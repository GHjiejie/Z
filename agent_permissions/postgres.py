"""PostgreSQL 18 persistence, preserving the application's write serialization."""

import logging
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from threading import Lock, RLock
from time import monotonic
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool, PoolClosed

from agent_permissions.config import Settings
from agent_permissions.database import RBAC_SCHEMA, SCHEMA, Database

# Fixed application namespace, shared across processes and schema revisions.
WRITE_LOCK = (0x41555052, 2)
logger = logging.getLogger(__name__)
_LOG_FILTER_LOCK = Lock()
_STAT_KEYS = (
    "pool_min",
    "pool_max",
    "pool_size",
    "pool_available",
    "requests_waiting",
    "requests_num",
    "requests_queued",
    "requests_wait_ms",
    "requests_errors",
    "connections_num",
    "connections_errors",
    "returns_bad",
    "usage_ms",
)


class DatabaseDriverLogFilter(logging.Filter):
    """Driver diagnostics may contain DSNs or SQL; retain only safe categories."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.msg if isinstance(record.msg, str) else ""
        reason = "DB_DRIVER_EVENT"
        for marker, category in (
            ("error connecting", "DB_POOL_CONNECT_FAILED"),
            ("reconnection attempt", "DB_POOL_RECONNECT_FAILED"),
            ("resetting", "DB_POOL_RESET_FAILED"),
            ("rollback", "DB_CONNECTION_ROLLBACK"),
            ("rolling back", "DB_CONNECTION_ROLLBACK"),
            ("broken connection", "DB_CONNECTION_LOST"),
        ):
            if marker in message:
                reason = category
                break
        record.msg = "PostgreSQL driver event reason=%s"
        record.args = (reason,)
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        record.db_reason = reason
        record.worker_pid = os.getpid()
        for key in ("conninfo", "database_url", "password", "token", "dsn"):
            if key in record.__dict__:
                record.__dict__[key] = "[redacted]"
        return True


def _protect_driver_logs() -> None:
    with _LOG_FILTER_LOCK:
        for name in ("psycopg", "psycopg.pool"):
            target = logging.getLogger(name)
            if not any(
                isinstance(item, DatabaseDriverLogFilter) for item in target.filters
            ):
                target.addFilter(DatabaseDriverLogFilter())


def _stats(pool: ConnectionPool) -> dict[str, int]:
    values = pool.get_stats()
    return {key: values.get(key, 0) for key in _STAT_KEYS}


class Record(Mapping[str, Any]):
    def __init__(self, names: tuple[str, ...], values: Sequence[Any]) -> None:
        self._values = tuple(values)
        self._fields = dict(zip(names, values, strict=True))

    def __getitem__(self, key: str | int) -> Any:
        return self._values[key] if isinstance(key, int) else self._fields[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._fields)

    def __len__(self) -> int:
        return len(self._fields)


class Result:
    def __init__(self, cursor: psycopg.Cursor) -> None:
        self.cursor = cursor
        self.names = tuple(column.name for column in cursor.description or ())

    @property
    def rowcount(self) -> int:
        return self.cursor.rowcount

    def fetchone(self) -> Record | None:
        value = self.cursor.fetchone()
        return None if value is None else Record(self.names, value)

    def fetchall(self) -> list[Record]:
        return [Record(self.names, value) for value in self.cursor.fetchall()]

    def __iter__(self) -> Iterator[Record]:
        for value in self.cursor:
            yield Record(self.names, value)


class PostgresConnection:
    def __init__(self, native: psycopg.Connection) -> None:
        self.native = native
        self._cursors: list[psycopg.Cursor] = []
        self._released = False

    def _cursor(self) -> psycopg.Cursor:
        if self._released:
            raise psycopg.InterfaceError("Database connection borrow has ended")
        cursor = self.native.cursor()
        self._cursors.append(cursor)
        return cursor

    def close_cursors(self) -> None:
        self._released = True
        failed = False
        for cursor in self._cursors:
            try:
                cursor.close()
            except Exception:  # noqa: BLE001 -- release all cursors before discarding the connection
                failed = True
        self._cursors.clear()
        if failed:
            with suppress(Exception):
                self.native.close()
            raise psycopg.OperationalError("Database cursor cleanup failed")

    def execute(
        self, statement: str, parameters: Sequence[Any] | None = None
    ) -> Result:
        # Application SQL uses qmark parameters; values are always sent to the
        # driver separately and never interpolated into the SQL text.
        query = statement.replace("?", "%s") if parameters is not None else statement
        cursor = self._cursor()
        cursor.execute(query, parameters)
        return Result(cursor)

    def executemany(
        self, statement: str, parameters: Iterable[Sequence[Any]]
    ) -> Result:
        cursor = self._cursor()
        cursor.executemany(statement.replace("?", "%s"), parameters)
        return Result(cursor)


class PostgresDatabase:
    def __init__(self, database_url: str, *, settings: Settings | None = None) -> None:
        self.database_url = database_url
        self.settings = settings or Settings(database_url=database_url)
        self._pool: ConnectionPool | None = None
        self._pool_pid: int | None = None
        self._lifecycle_lock = RLock()
        self._reconnect_log_lock = Lock()
        self._last_reconnect_log = float("-inf")

    def open(self) -> None:
        with self._lifecycle_lock:
            if self._pool is not None:
                if self._pool_pid != os.getpid():
                    raise PoolClosed("Database pool belongs to another process")
                if not self._pool.closed:
                    return
            _protect_driver_logs()
            options = self.settings
            pool = ConnectionPool(
                self.database_url,
                kwargs={
                    "autocommit": True,
                    "connect_timeout": 10,
                    "application_name": "agent-users",
                },
                min_size=options.db_pool_min_size,
                max_size=options.db_pool_max_size,
                timeout=options.db_pool_timeout_seconds,
                max_waiting=options.db_pool_max_waiting,
                max_idle=options.db_pool_max_idle_seconds,
                max_lifetime=options.db_pool_max_lifetime_seconds,
                reconnect_timeout=options.db_pool_reconnect_timeout_seconds,
                reconnect_failed=self._reconnect_failed,
                check=ConnectionPool.check_connection,
                name=f"agent-users-{os.getpid()}-{id(self):x}",
                open=False,
            )
            self._pool = pool
            self._pool_pid = os.getpid()
            try:
                pool.open(wait=True, timeout=options.db_pool_startup_timeout_seconds)
            except BaseException:
                with suppress(Exception):
                    pool.close(timeout=options.db_pool_close_timeout_seconds)
                self._pool = None
                self._pool_pid = None
                raise

    def close(self) -> None:
        with self._lifecycle_lock:
            pool = self._pool
            if pool is None:
                return
            if self._pool_pid != os.getpid():
                raise PoolClosed("Database pool belongs to another process")
            try:
                pool.close(timeout=self.settings.db_pool_close_timeout_seconds)
            finally:
                self._pool = None
                self._pool_pid = None

    def pool_stats(self) -> dict[str, int]:
        with self._lifecycle_lock:
            return (
                _stats(self._pool)
                if self._pool is not None and self._pool_pid == os.getpid()
                else {}
            )

    def _reconnect_failed(self, pool: ConnectionPool) -> None:
        with self._reconnect_log_lock:
            now = monotonic()
            if (
                now - self._last_reconnect_log
                < self.settings.db_pool_reconnect_timeout_seconds
            ):
                return
            self._last_reconnect_log = now
        logger.error(
            "Database pool reconnect failed",
            extra={
                "db_reason": "DB_POOL_RECONNECT_FAILED",
                "worker_pid": os.getpid(),
                "pool_name": pool.name,
                "pool_stats": _stats(pool),
            },
        )

    @contextmanager
    def connect(self) -> Iterator[PostgresConnection]:
        with self._lifecycle_lock:
            pool = self._pool
            if pool is None or self._pool_pid != os.getpid():
                raise PoolClosed("Database pool is not available in this process")
        with pool.connection(timeout=self.settings.db_pool_timeout_seconds) as native:
            connection = PostgresConnection(native)
            try:
                yield connection
            except BaseException:
                try:
                    connection.close_cursors()
                except Exception:  # noqa: BLE001 -- cleanup must preserve the original exception
                    logger.error(
                        "Database cursor cleanup failed",
                        extra={"db_reason": "DB_CURSOR_CLEANUP_FAILED"},
                    )
                raise
            else:
                connection.close_cursors()

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[PostgresConnection]:
        with self.connect() as connection:
            try:
                connection.execute(
                    "BEGIN"
                    if write
                    else "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                connection.execute("SET LOCAL lock_timeout='10s'")
                connection.execute("SET LOCAL statement_timeout='30s'")
                if write:
                    # Lock before the first authorization read. All mutators,
                    # login, rate limiting and migrations use this same lock.
                    connection.execute("SELECT pg_advisory_xact_lock(?,?)", WRITE_LOCK)
                yield connection
                connection.native.commit()
            except BaseException:
                try:
                    connection.native.rollback()
                except Exception:  # noqa: BLE001 -- rollback cleanup must preserve the original exception
                    with suppress(Exception):
                        connection.native.close()
                    logger.error(
                        "Database rollback failed",
                        extra={"db_reason": "DB_ROLLBACK_FAILED"},
                    )
                raise

    def initialize(self, *, seed: bool = True) -> None:
        with self.transaction(write=True) as connection:
            if connection.native.info.server_version < 180000:
                raise RuntimeError("Agent users requires PostgreSQL 18 or newer")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_metadata (key TEXT PRIMARY KEY,version INTEGER NOT NULL)"
            )
            version = connection.execute(
                "SELECT version FROM schema_metadata WHERE key='schema_version'"
            ).fetchone()
            if version is not None and version[0] != 2:
                raise RuntimeError("Unsupported user database schema version")
            # BIGINT and DOUBLE PRECISION retain SQLite's integer/float range.
            schema = (
                (SCHEMA + RBAC_SCHEMA)
                .replace(" INTEGER", " BIGINT")
                .replace(" REAL", " DOUBLE PRECISION")
            )
            Database._execute_schema(connection, schema)
            connection.execute("""CREATE OR REPLACE FUNCTION app_contains(value TEXT,needle TEXT)
                RETURNS INTEGER LANGUAGE SQL IMMUTABLE PARALLEL SAFE AS $$
                SELECT pg_catalog.strpos(
                    pg_catalog.casefold(COALESCE(value,'') COLLATE pg_catalog.pg_unicode_fast),
                    pg_catalog.casefold(needle COLLATE pg_catalog.pg_unicode_fast)
                ) $$""")
            if seed:
                Database._seed_catalog(connection)
            connection.execute(
                "INSERT INTO schema_metadata(key,version) VALUES('schema_version',2) ON CONFLICT(key) DO NOTHING"
            )
