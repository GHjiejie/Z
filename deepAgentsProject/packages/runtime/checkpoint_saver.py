from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import contextmanager
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver

from packages.persistence import Database
from packages.persistence.fencing import LeaseLostError, RunWriteFence, current_write_fence


def _try_migration_lock(connection) -> bool:
    with connection.execute("SELECT pg_try_advisory_lock(726593927602) AS acquired") as cursor:
        return bool(cursor.fetchone()["acquired"])


@contextmanager
def _migration_lock(connection, timeout_seconds: float = 30):
    if not connection.autocommit:
        raise RuntimeError("Checkpoint migrations require an autocommit connection")
    deadline = time.monotonic() + timeout_seconds
    # A blocking advisory-lock SELECT retains a statement snapshot while it
    # waits. CREATE INDEX CONCURRENTLY then waits for that snapshot, producing
    # a deadlock with the lock holder. Each failed try must finish before sleep.
    while not _try_migration_lock(connection):
        if time.monotonic() >= deadline:
            raise TimeoutError("Checkpoint migration lock timed out; retry the migration job")
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    try:
        yield
    finally:
        try:
            with connection.execute("SELECT pg_advisory_unlock(726593927602)"):
                pass
        except BaseException:
            # Never return a possibly locked session to the connection pool.
            connection.close()
            raise


class FencedCheckpointSaver(BaseCheckpointSaver):
    """Run lease and checkpoint writes share a synchronous transaction boundary.

    PostgreSQL uses the platform transaction's exact connection, including
    checkpoint blobs and pending writes. SQLite retains the existing checkpoint
    file, protected by the platform write lock for local single-process use.
    Async callers offload the entire operation; no DB lock is held across an
    await that needs the event loop to make progress.
    """

    def __init__(self, db: Database, sqlite_path: str | None = None):
        super().__init__()
        self.db = db
        self.sqlite = (
            SqliteSaver(sqlite3.connect(sqlite_path, check_same_thread=False))
            if db.dialect == "sqlite" and sqlite_path else None
        )
        if db.dialect == "sqlite" and self.sqlite is None:
            raise ValueError("SQLite checkpoint path is required")

    def close(self) -> None:
        if self.sqlite:
            self.sqlite.conn.close()

    def initialize(self, *, auto_migrate: bool) -> None:
        if self.sqlite:
            if auto_migrate:
                self.sqlite.setup()
            else:
                names = {
                    row[0] for row in self.sqlite.conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if not {"checkpoints", "writes"}.issubset(names):
                    raise RuntimeError("Checkpoint schema is missing; run the migration job")
                self.sqlite.is_setup = True
            return
        from langgraph.checkpoint.postgres import PostgresSaver

        if auto_migrate:
            with self.db.pool.connection() as connection:
                with _migration_lock(connection):
                    PostgresSaver(connection).setup()
        else:
            try:
                row = self.db.fetch_one("SELECT MAX(v) AS version FROM checkpoint_migrations")
                if not row or row["version"] != len(PostgresSaver.MIGRATIONS) - 1:
                    raise RuntimeError("Checkpoint schema is not current; run the migration job")
            except Exception as exc:
                raise RuntimeError("Checkpoint schema is not current; run the migration job") from exc

    def _call(self, method: str, *args: Any, write: bool = False, **kwargs: Any):
        with self.db.transaction() as connection:
            if write:
                fence = current_write_fence()
                if not isinstance(fence, RunWriteFence):
                    raise LeaseLostError("Checkpoint writes require an owned Run lease")
                run = self.db.fetch_one("SELECT * FROM runs WHERE id=?", (fence.run_id,))
                expected = f"{run['tenant_id']}:{run['project_id']}:{run['thread_id']}"
                actual = args[0].get("configurable", {}).get("thread_id")
                if actual != expected:
                    session = self.db.fetch_one(
                        """SELECT id FROM coding_graph_sessions WHERE graph_thread_id=?
                           AND run_id=? AND attempt_id=? AND tenant_id=? AND project_id=?""",
                        (actual, run["id"], fence.attempt_id, run["tenant_id"], run["project_id"]),
                    )
                    if not session:
                        raise LeaseLostError("Checkpoint thread does not match the leased Run")
            if self.sqlite:
                saver = self.sqlite
            else:
                from langgraph.checkpoint.postgres import PostgresSaver

                saver = PostgresSaver(connection.raw, serde=self.serde)
            result = getattr(saver, method)(*args, **kwargs)
            return list(result) if method == "list" else result

    def get_tuple(self, config):
        return self._call("get_tuple", config)

    async def aget_tuple(self, config):
        return await asyncio.to_thread(self.get_tuple, config)

    def list(self, config, *, filter=None, before=None, limit=None):
        yield from self._call("list", config, filter=filter, before=before, limit=limit)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        values = await asyncio.to_thread(self._call, "list", config, filter=filter, before=before, limit=limit)
        for value in values:
            yield value

    def put(self, config, checkpoint, metadata, new_versions):
        return self._call("put", config, checkpoint, metadata, new_versions, write=True)

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id, task_path=""):
        return self._call("put_writes", config, writes, task_id, task_path, write=True)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        return await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    def get_next_version(self, current, channel):
        return SqliteSaver.get_next_version(self, current, channel)

    def get_delta_channel_history(self, *, config, channels):
        return self._call("get_delta_channel_history", config=config, channels=channels)

    async def aget_delta_channel_history(self, *, config, channels):
        return await asyncio.to_thread(self.get_delta_channel_history, config=config, channels=channels)
