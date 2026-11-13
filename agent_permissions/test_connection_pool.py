"""Connection ownership, backpressure and recovery against real PostgreSQL."""

import io
import logging
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from time import monotonic, sleep
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit, urlunsplit

import psycopg
from fastapi.testclient import TestClient
from psycopg.pq import TransactionStatus
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from agent_permissions.__main__ import main as cli_main
from agent_permissions.api import create_app
from agent_permissions.config import Settings
from agent_permissions.database import Database
from agent_permissions.database_errors import connection_failure_reason
from agent_permissions.errors import ApiError
from agent_permissions.models import UserCreate
from agent_permissions.postgres import DatabaseDriverLogFilter, PostgresDatabase
from agent_permissions.test_postgres import TEST_URL, PostgresFixture


class PoolConfigurationTests(unittest.TestCase):
    def test_defaults_and_invalid_pool_configuration(self) -> None:
        settings = Settings()
        self.assertEqual(
            (settings.db_pool_min_size, settings.db_pool_max_size), (2, 10)
        )
        self.assertEqual(settings.db_pool_timeout_seconds, 3.0)
        self.assertEqual(settings.db_pool_max_waiting, 20)
        for field in ("db_pool_min_size", "db_pool_max_size", "db_pool_max_waiting"):
            for value in (0, -1, 1.5, True, None, "2"):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(ValueError),
                ):
                    replace(settings, **{field: value})
        for field in settings.__dataclass_fields__:
            if field.startswith("db_pool_") and field.endswith("_seconds"):
                for value in (0, -0.1, float("nan"), float("inf"), True, "1"):
                    with (
                        self.subTest(field=field, value=value),
                        self.assertRaises(ValueError),
                    ):
                        replace(settings, **{field: value})
        with self.assertRaises(ValueError):
            Settings(db_pool_min_size=3, db_pool_max_size=2)

    def test_float_environment_values_and_environment_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pool.env"
            path.write_text(
                "AGENT_USERS_DB_POOL_MIN_SIZE=1\nAGENT_USERS_DB_POOL_MAX_SIZE=3\nAGENT_USERS_DB_POOL_TIMEOUT_SECONDS=0.25\n"
            )
            with patch.dict(
                os.environ, {"AGENT_USERS_DB_POOL_TIMEOUT_SECONDS": "0.5"}, clear=True
            ):
                settings = Settings.from_env(path)
            self.assertEqual(settings.db_pool_timeout_seconds, 0.5)
            self.assertEqual(settings.db_pool_max_size, 3)
            for value in ("nan", "inf", "invalid"):
                with (
                    patch.dict(
                        os.environ,
                        {"AGENT_USERS_DB_POOL_TIMEOUT_SECONDS": value},
                        clear=True,
                    ),
                    self.assertRaises(ValueError),
                ):
                    Settings.from_env()

    def test_connection_failure_classification_preserves_sql_error_auditing(
        self,
    ) -> None:
        for error, expected in (
            (PoolTimeout(), "DB_POOL_TIMEOUT"),
            (PoolClosed(), "DB_POOL_CLOSED"),
            (TooManyRequests(), "DB_POOL_QUEUE_FULL"),
            (psycopg.OperationalError(), "DB_CONNECTION_UNAVAILABLE"),
            (psycopg.errors.AdminShutdown(), "DB_CONNECTION_UNAVAILABLE"),
            (psycopg.errors.LockNotAvailable(), None),
            (psycopg.errors.QueryCanceled(), None),
            (psycopg.errors.DeadlockDetected(), None),
        ):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(connection_failure_reason(error), expected)

    def test_driver_diagnostics_are_redacted_before_formatting(self) -> None:
        secret = "postgresql://name:private-password@server/database"
        error = RuntimeError(secret)
        record = logging.LogRecord(
            "psycopg.pool",
            logging.ERROR,
            __file__,
            1,
            "error connecting %s",
            (secret,),
            (RuntimeError, error, None),
        )
        record.conninfo = secret
        record.stack_info = secret
        DatabaseDriverLogFilter().filter(record)
        self.assertNotIn(secret, record.getMessage())
        self.assertEqual(record.conninfo, "[redacted]")
        self.assertIsNone(record.exc_info)
        self.assertIsNone(record.stack_info)


class DatabaseLifecycleTests(unittest.TestCase):
    def test_sqlite_lifecycle_does_not_create_persistent_connections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sqlite.db"
            database = Database(path)
            database.open()
            database.close()
            self.assertFalse(path.exists())
            self.assertEqual(database.pool_stats(), {})
            database.initialize()
            with database.transaction() as connection:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 2
                )

    def test_app_cleanup_covers_open_and_initialize_failures(self) -> None:
        for phase in ("open", "initialize"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                app = create_app(Settings(database_path=Path(directory) / "db"))
                database = app.state.database
                with (
                    patch.object(
                        database, phase, side_effect=RuntimeError("startup failed")
                    ),
                    patch.object(database, "close") as close,
                ):
                    with (
                        self.assertRaisesRegex(RuntimeError, "startup failed"),
                        TestClient(app),
                    ):
                        self.fail("启动失败时不能接受请求")
                    close.assert_called_once()

    def test_cli_closes_database_after_success_rejection_and_cancellation(self) -> None:
        for failure, expected in (
            (None, 0),
            (ApiError(409, "BOOTSTRAP_ALREADY_COMPLETED", "已有用户"), 1),
            (KeyboardInterrupt(), 1),
            (PoolTimeout(), 1),
        ):
            with (
                self.subTest(failure=type(failure).__name__),
                redirect_stdout(io.StringIO()),
            ):
                database = MagicMock()
                service = MagicMock()
                service.bootstrap_admin.return_value = {"username": "admin"}
                service.bootstrap_admin.side_effect = failure
                if isinstance(failure, PoolTimeout):
                    database.open.side_effect = failure
                with (
                    patch(
                        "agent_permissions.__main__.create_database",
                        return_value=database,
                    ),
                    patch(
                        "agent_permissions.__main__.UserService", return_value=service
                    ),
                ):
                    self.assertEqual(
                        cli_main(["init-admin", "--username", "admin"]), expected
                    )
                database.close.assert_called_once()


@unittest.skipUnless(TEST_URL, "Set AGENT_USERS_TEST_DATABASE_URL for real pool tests")
class PostgresPoolTests(PostgresFixture, unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.settings = replace(
            self.settings_for(Path(directory) / "unused.sqlite3"),
            db_pool_min_size=1,
            db_pool_max_size=1,
            db_pool_max_waiting=1,
            db_pool_timeout_seconds=0.25,
            db_pool_startup_timeout_seconds=3.0,
            db_pool_close_timeout_seconds=2.0,
        )
        self.database = self.make_database(self.settings)
        self.database.initialize()

    def make_database(self, settings: Settings) -> PostgresDatabase:
        database = PostgresDatabase(settings.database_url, settings=settings)
        self.addCleanup(database.close)
        database.open()
        return database

    def pid(self, database: PostgresDatabase | None = None) -> int:
        with (database or self.database).transaction() as connection:
            return connection.execute("SELECT pg_backend_pid()").fetchone()[0]

    def wait_for(self, predicate, timeout: float = 2.0) -> None:
        deadline = monotonic() + timeout
        while not predicate():
            if monotonic() >= deadline:
                self.fail("条件未在预算内成立")
            sleep(0.005)

    def test_reuses_physical_connection_and_preserves_schema_and_row_format(
        self,
    ) -> None:
        first = self.pid()
        before = self.database.pool_stats()
        for _ in range(20):
            self.assertEqual(self.pid(), first)
        after = self.database.pool_stats()
        self.assertEqual(after["connections_num"], before["connections_num"])
        self.assertEqual(after["requests_num"] - before["requests_num"], 20)
        self.assertEqual(self.database.pool_stats(), after)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT current_schema() AS schema, 7 AS value"
            ).fetchone()
            self.assertTrue(row["schema"].startswith("test_agent_users_"))
            self.assertEqual((row[1], row["value"]), (7, 7))

    def test_borrow_timeout_recovers_after_release(self) -> None:
        with self.database.connect():
            started = monotonic()
            with self.assertRaises(PoolTimeout), self.database.connect():
                self.fail("池已满时不应借到连接")
            elapsed = monotonic() - started
            self.assertGreaterEqual(elapsed, 0.15)
            self.assertLess(elapsed, 1.5)
        self.assertIsInstance(self.pid(), int)
        self.assertEqual(self.database.pool_stats()["requests_waiting"], 0)

    def test_wait_queue_is_bounded_and_waiter_can_finish(self) -> None:
        database = self.make_database(
            replace(self.settings, db_pool_timeout_seconds=1.0)
        )
        executor = self.enterContext(ThreadPoolExecutor(max_workers=1))
        with database.connect() as held:
            pid = held.native.info.backend_pid
            waiting = executor.submit(self.pid, database)
            self.wait_for(lambda: database.pool_stats()["requests_waiting"] == 1)
            with self.assertRaises(TooManyRequests), database.connect():
                self.fail("等待队列已满时不能继续排队")
        self.assertEqual(waiting.result(timeout=2), pid)
        self.assertEqual(database.pool_stats()["pool_size"], 1)

    def test_rollback_read_only_and_local_settings_do_not_leak(self) -> None:
        with self.database.connect() as connection:
            default_timeout = connection.execute("SHOW statement_timeout").fetchone()[0]
            default_read_only = connection.execute(
                "SHOW transaction_read_only"
            ).fetchone()[0]
        with self.database.transaction(write=True) as connection:
            connection.execute("CREATE TABLE pool_probe(value INTEGER)")
        with (
            self.assertRaisesRegex(ValueError, "business failure"),
            self.database.transaction(write=True) as connection,
        ):
            connection.execute("INSERT INTO pool_probe VALUES(1)")
            self.assertEqual(
                connection.execute("SHOW statement_timeout").fetchone()[0], "30s"
            )
            raise ValueError("business failure")
        with self.database.transaction() as connection:
            self.assertEqual(
                connection.execute("SHOW transaction_read_only").fetchone()[0], "on"
            )
            self.assertEqual(
                connection.execute("SELECT count(*) FROM pool_probe").fetchone()[0], 0
            )
        with (
            self.assertRaises(psycopg.errors.ReadOnlySqlTransaction),
            self.database.transaction() as connection,
        ):
            connection.execute("INSERT INTO pool_probe VALUES(2)")
        with self.database.connect() as connection:
            self.assertEqual(
                connection.native.info.transaction_status, TransactionStatus.IDLE
            )
            self.assertTrue(connection.native.autocommit)
            self.assertEqual(
                connection.execute("SHOW statement_timeout").fetchone()[0],
                default_timeout,
            )
            self.assertEqual(
                connection.execute("SHOW transaction_read_only").fetchone()[0],
                default_read_only,
            )

    def test_cursor_and_adapter_cannot_escape_borrow(self) -> None:
        with self.database.connect() as connection:
            result = connection.execute("SELECT 1 AS value")
            row = result.fetchone()
        self.assertTrue(result.cursor.closed)
        self.assertEqual(row["value"], 1)
        with self.assertRaises(psycopg.InterfaceError):
            connection.execute("SELECT 2")
        with self.assertRaises(psycopg.InterfaceError):
            result.fetchone()

    def test_disconnected_idle_connection_is_replaced(self) -> None:
        previous = self.pid()
        attempts = self.database.pool_stats()["connections_num"]
        with psycopg.connect(TEST_URL, autocommit=True) as control:
            self.assertTrue(
                control.execute(
                    "SELECT pg_terminate_backend(%s)", (previous,)
                ).fetchone()[0]
            )
        self.assertNotEqual(self.pid(), previous)
        self.assertGreater(self.database.pool_stats()["connections_num"], attempts)

    def test_concurrent_borrowing_grows_only_to_configured_capacity(self) -> None:
        database = self.make_database(
            replace(
                self.settings,
                db_pool_max_size=3,
                db_pool_max_waiting=3,
                db_pool_timeout_seconds=0.5,
            )
        )
        barrier = threading.Barrier(4)
        release = threading.Event()

        def hold_connection():
            with database.connect() as connection:
                pid = connection.native.info.backend_pid
                barrier.wait(timeout=3)
                release.wait(timeout=3)
                return pid

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(hold_connection) for _ in range(3)]
            try:
                barrier.wait(timeout=3)
                self.assertEqual(database.pool_stats()["pool_size"], 3)
                with self.assertRaises(PoolTimeout), database.connect():
                    self.fail("达到容量上限后不能再创建连接")
            finally:
                release.set()
            self.assertEqual(len({future.result(timeout=2) for future in futures}), 3)
        self.assertEqual(database.pool_stats()["pool_available"], 3)

    def test_concurrent_open_only_creates_one_process_pool(self) -> None:
        database = PostgresDatabase(self.settings.database_url, settings=self.settings)
        self.addCleanup(database.close)
        with psycopg.connect(TEST_URL, autocommit=True) as control:
            before = {
                row[0]
                for row in control.execute(
                    "SELECT pid FROM pg_stat_activity WHERE datname=current_database()"
                )
            }
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(lambda _: database.open(), range(4)))
            after = {
                row[0]
                for row in control.execute(
                    "SELECT pid FROM pg_stat_activity WHERE datname=current_database()"
                )
            }
        self.assertEqual(len(after - before), 1)
        first = self.pid(database)
        database.open()
        self.assertEqual(self.pid(database), first)

    def test_close_is_idempotent_waits_for_return_and_allows_explicit_reopen(
        self,
    ) -> None:
        pool_name = self.database._pool.name
        with self.database.connect() as connection:
            native = connection.native
            previous = native.info.backend_pid
            self.database.close()
            self.database.close()
            self.assertFalse(native.closed)
            self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
            with self.assertRaises(PoolClosed), self.database.connect():
                self.fail("关闭后不应自动重新开池")
        self.assertTrue(native.closed)
        self.wait_for(
            lambda: (
                not any(
                    thread.name.startswith(pool_name)
                    for thread in threading.enumerate()
                )
            )
        )
        self.database.open()
        self.assertNotEqual(self.pid(), previous)

    def test_separate_instances_and_process_owner_have_separate_pools(self) -> None:
        second = self.make_database(self.settings)
        self.assertNotEqual(self.pid(), self.pid(second))
        second.close()
        self.assertIsInstance(self.pid(), int)
        owner = self.database._pool_pid
        self.database._pool_pid = os.getpid() + 1
        try:
            with self.assertRaises(PoolClosed), self.database.connect():
                self.fail("不能使用其他进程的池")
        finally:
            self.database._pool_pid = owner

    def test_failed_startup_closes_pool_without_leaking_driver_diagnostics(
        self,
    ) -> None:
        parts = urlsplit(self.settings.database_url)
        bad_url = urlunsplit(
            parts._replace(
                netloc=f"{parts.username}:private-connect-password@127.0.0.1:1"
            )
        )
        settings = replace(
            self.settings, database_url=bad_url, db_pool_startup_timeout_seconds=0.2
        )
        database = PostgresDatabase(bad_url, settings=settings)
        self.addCleanup(database.close)
        prefix = f"agent-users-{os.getpid()}-{id(database):x}"
        with (
            self.assertLogs("psycopg.pool", level="WARNING") as logs,
            self.assertRaises(PoolTimeout),
        ):
            database.open()
        self.assertNotIn("private-connect-password", " ".join(logs.output))
        self.assertNotIn(bad_url, " ".join(logs.output))
        self.assertEqual(database.pool_stats(), {})
        self.wait_for(
            lambda: (
                not any(
                    thread.name.startswith(prefix) for thread in threading.enumerate()
                )
            )
        )

    def test_rollback_failure_preserves_original_error_and_discards_connection(
        self,
    ) -> None:
        previous = self.pid()
        patcher = None
        try:
            with (
                self.assertLogs("agent_permissions.postgres", level="ERROR") as logs,
                self.assertRaisesRegex(ValueError, "original failure"),
                self.database.transaction(write=True) as connection,
            ):
                patcher = patch.object(
                    connection.native,
                    "rollback",
                    side_effect=psycopg.OperationalError("private-rollback-diagnostic"),
                )
                patcher.start()
                raise ValueError("original failure")
        finally:
            if patcher is not None:
                patcher.stop()
        self.assertNotIn("private-rollback-diagnostic", " ".join(logs.output))
        self.assertNotEqual(self.pid(), previous)

    def test_cleanup_failure_does_not_replace_business_error(self) -> None:
        previous = self.pid()
        broken_cursor = MagicMock()
        broken_cursor.close.side_effect = RuntimeError("private-cursor-diagnostic")
        with (
            self.assertLogs("agent_permissions.postgres", level="ERROR") as logs,
            self.assertRaisesRegex(ValueError, "original failure"),
            self.database.connect() as connection,
        ):
            connection._cursors.append(broken_cursor)
            raise ValueError("original failure")
        self.assertNotIn("private-cursor-diagnostic", " ".join(logs.output))
        self.assertNotEqual(self.pid(), previous)

    def test_commit_result_is_not_replayed_after_error(self) -> None:
        with self.database.transaction(write=True) as connection:
            connection.execute("CREATE TABLE pool_probe(value INTEGER)")
        patcher = None
        try:
            with (
                self.assertRaises(psycopg.OperationalError),
                self.database.transaction(write=True) as connection,
            ):
                connection.execute("INSERT INTO pool_probe VALUES(1)")
                commit = connection.native.commit

                def ambiguous_commit():
                    commit()
                    raise psycopg.OperationalError("commit response lost")

                patcher = patch.object(
                    connection.native, "commit", side_effect=ambiguous_commit
                )
                mocked_commit = patcher.start()
            mocked_commit.assert_called_once()
        finally:
            if patcher is not None:
                patcher.stop()
        with self.database.transaction() as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM pool_probe").fetchone()[0], 1
            )

    def test_api_pool_timeout_does_not_queue_failure_audit_or_revoke_session(
        self,
    ) -> None:
        app = create_app(self.settings)
        with TestClient(app) as client:
            app.state.service.bootstrap_admin(
                UserCreate(username="admin", display_name="管理员", password="12345")
            )
            login = client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "12345"}
            )
            self.assertEqual(login.status_code, 200)
            headers = {
                "Authorization": f"Bearer {login.json()['data']['session_token']}"
            }
            with app.state.database.connect():
                before = app.state.database.pool_stats()["requests_num"]
                with (
                    patch.object(app.state.service, "record_failure") as audit,
                    self.assertLogs("agent_permissions.api", level="ERROR"),
                ):
                    response = client.get("/api/v1/users/me", headers=headers)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(
                    response.json()["error"]["code"], "SERVICE_UNAVAILABLE"
                )
                self.assertEqual(
                    app.state.database.pool_stats()["requests_num"] - before, 1
                )
                audit.assert_not_called()
            self.assertEqual(
                client.get("/api/v1/users/me", headers=headers).status_code, 200
            )
            rejected = client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "wrong-password"},
            )
            self.assertEqual(rejected.status_code, 401)
            with self.database.transaction() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM audit_events WHERE action='auth.login' AND outcome='failure'"
                    ).fetchone()[0],
                    1,
                )
            for error in (
                TooManyRequests("private-error"),
                PoolClosed("private-error"),
            ):
                with (
                    patch.object(app.state.database, "transaction", side_effect=error),
                    patch.object(app.state.service, "record_failure") as audit,
                    self.assertLogs("agent_permissions.api", level="ERROR") as logs,
                ):
                    response = client.get("/api/v1/users/me", headers=headers)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("private-error", response.text + " ".join(logs.output))
                audit.assert_not_called()

    def test_failed_schema_initialization_and_repeated_app_lifespan_release_pools(
        self,
    ) -> None:
        app = create_app(self.settings)
        with TestClient(app):
            first = self.pid(app.state.database)
        self.assertEqual(app.state.database.pool_stats(), {})
        with TestClient(app):
            self.assertNotEqual(self.pid(app.state.database), first)
        with self.database.transaction(write=True) as connection:
            connection.execute("UPDATE schema_metadata SET version=99")
        with self.assertRaisesRegex(RuntimeError, "schema version"), TestClient(app):
            self.fail("不支持的 schema 不应启动")
        self.assertEqual(app.state.database.pool_stats(), {})


if __name__ == "__main__":
    unittest.main()
