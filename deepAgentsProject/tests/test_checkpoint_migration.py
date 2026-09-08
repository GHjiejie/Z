from __future__ import annotations

import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest

from packages.persistence.postgres import PostgresDatabase
from packages.runtime import checkpoint_saver
from packages.runtime.checkpoint_saver import FencedCheckpointSaver


@pytest.fixture
def migration_databases():
    url = os.getenv("DEEPAGENT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("DEEPAGENT_TEST_POSTGRES_URL is required")
    import psycopg
    from psycopg import sql

    schema = "migration_" + secrets.token_hex(10)
    databases = []
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            parts = urlsplit(url)
            query = dict(parse_qsl(parts.query))
            query["options"] = f"-csearch_path={schema},public"
            dsn = urlunsplit(parts._replace(query=urlencode(query)))
            for _ in range(2):
                databases.append(PostgresDatabase(dsn, min_pool_size=1, max_pool_size=1))
            yield databases
        finally:
            for database in databases:
                database.close()
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_concurrent_platform_and_checkpoint_migration_jobs(migration_databases):
    start = Barrier(2)

    def migrate(database):
        start.wait(timeout=5)
        database.initialize(auto_migrate=True)
        FencedCheckpointSaver(database).initialize(auto_migrate=True)
        database.assert_schema_current()
        FencedCheckpointSaver(database).initialize(auto_migrate=False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(migrate, database) for database in migration_databases]
        for job in jobs:
            job.result(timeout=20)


def test_concurrent_checkpoint_migrations_do_not_retain_waiter_snapshots(migration_databases, monkeypatch):
    from langgraph.checkpoint.postgres import PostgresSaver

    entered, contending = Event(), Event()
    original_setup, original_try = PostgresSaver.setup, checkpoint_saver._try_migration_lock

    def setup(saver):
        entered.set()
        assert contending.wait(5), "A second migration must really contend for the lock"
        return original_setup(saver)

    def attempt(connection):
        acquired = original_try(connection)
        if not acquired:
            contending.set()
        return acquired

    monkeypatch.setattr(PostgresSaver, "setup", setup)
    monkeypatch.setattr(checkpoint_saver, "_try_migration_lock", attempt)
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(FencedCheckpointSaver(migration_databases[0]).initialize, auto_migrate=True)
        assert entered.wait(5)
        waiter = pool.submit(FencedCheckpointSaver(migration_databases[1]).initialize, auto_migrate=True)
        owner.result(timeout=15)
        waiter.result(timeout=15)
    for database in migration_databases:
        FencedCheckpointSaver(database).initialize(auto_migrate=False)
        indices = database.fetch_all("""SELECT i.indisvalid FROM pg_index i
            JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=current_schema()""")
        assert indices and all(index["indisvalid"] for index in indices)


def test_checkpoint_migration_timeout_and_failure_release_locks(migration_databases):
    with migration_databases[0].pool.connection() as owner, migration_databases[1].pool.connection() as waiter:
        with checkpoint_saver._migration_lock(owner):
            with pytest.raises(TimeoutError, match="migration lock timed out"):
                with checkpoint_saver._migration_lock(waiter, timeout_seconds=0.05):
                    pytest.fail("The competing migration cannot enter")
        with pytest.raises(ValueError, match="injected"):
            with checkpoint_saver._migration_lock(owner):
                raise ValueError("injected setup failure")
        with checkpoint_saver._migration_lock(waiter, timeout_seconds=0.05):
            pass
