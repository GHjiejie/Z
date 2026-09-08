from __future__ import annotations

import contextvars
import os
import re
import threading
from datetime import timezone
from contextlib import contextmanager
from typing import Any, Dict, Iterable, Iterator, List, Optional

from packages.persistence.database import Database, SCHEMA
from packages.persistence.fencing import current_write_fence, validate_write_fence


def _postgres_sql(sql: str) -> str:
    statement = sql.strip()
    ignore_conflicts = bool(re.match(r"(?is)^INSERT\s+OR\s+IGNORE\s+INTO\s+", statement))
    if ignore_conflicts:
        statement = re.sub(
            r"(?is)^INSERT\s+OR\s+IGNORE\s+INTO\s+", "INSERT INTO ", statement
        )
    statement = re.sub(r"(?i)\s+COLLATE\s+NOCASE", "", statement)
    statement = statement.replace("?", "%s").replace(
        "datetime('now')", "CURRENT_TIMESTAMP"
    )
    if ignore_conflicts:
        statement = statement.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return statement


def _postgres_schema() -> str:
    statements = [
        "CREATE EXTENSION IF NOT EXISTS citext",
        SCHEMA.replace("PRAGMA journal_mode=WAL;", "")
        .replace("PRAGMA foreign_keys=ON;", "")
        .replace("username TEXT NOT NULL COLLATE NOCASE UNIQUE", "username CITEXT NOT NULL UNIQUE"),
    ]
    return ";\n".join(statements)


def _split_script(script: str) -> Iterator[str]:
    for statement in script.split(";"):
        if statement.strip():
            yield statement.strip()


class _ConnectionAdapter:
    def __init__(self, connection: Any):
        self.raw = connection

    def execute(self, sql: str, params: Iterable[Any] = ()):
        return self.raw.execute(_postgres_sql(sql), tuple(params))

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]):
        return self.raw.cursor().executemany(
            _postgres_sql(sql), [tuple(row) for row in rows]
        )

    def executescript(self, script: str) -> None:
        for statement in _split_script(script):
            self.raw.execute(_postgres_sql(statement))

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()


class PostgresDatabase(Database):
    """Pooled PostgreSQL repository implementing the platform Database contract."""

    def __init__(self, dsn: str, *, min_pool_size: int = 2, max_pool_size: int = 20):
        try:
            from psycopg.rows import dict_row
            from psycopg.conninfo import conninfo_to_dict
            from psycopg_pool import ConnectionPool
        except ImportError as error:  # pragma: no cover - exercised in deployment
            raise RuntimeError(
                "PostgreSQL requires the psycopg binary and pool dependencies"
            ) from error
        self.dsn = dsn
        self.dialect = "postgresql"
        self.lock = threading.RLock()
        self._active_connection: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
            "deepagent_postgres_connection", default=None
        )
        self._transaction_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
            "deepagent_postgres_transaction_depth", default=0
        )
        supplied_options = conninfo_to_dict(dsn).get("options", "")
        statement_timeout = max(1000, int(os.getenv("DEEPAGENT_DB_STATEMENT_TIMEOUT_MS", "15000")))
        lock_timeout = max(100, int(os.getenv("DEEPAGENT_DB_LOCK_TIMEOUT_MS", "5000")))
        session_options = (
            f"-c statement_timeout={statement_timeout} -c lock_timeout={lock_timeout} "
            f"-c idle_in_transaction_session_timeout=60000 {supplied_options}"
        ).strip()
        self.pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_pool_size,
            max_size=max_pool_size,
            kwargs={
                "autocommit": True, "row_factory": dict_row,
                "connect_timeout": max(1, int(os.getenv("DEEPAGENT_DB_CONNECT_TIMEOUT_SECONDS", "5"))),
                "options": session_options,
            },
            open=True,
        )
        self.pool.wait()

    @property
    def connection(self) -> _ConnectionAdapter:
        active = self._active_connection.get()
        if active is None:
            raise RuntimeError(
                "Direct database connection access requires Database.transaction()"
            )
        return _ConnectionAdapter(active)

    def initialize(self, *, auto_migrate: bool = True) -> None:
        if not auto_migrate:
            self.assert_schema_current()
            return
        with self.transaction() as connection:
            # Serialize the ordinary transactional schema before creating any
            # catalog objects. IF NOT EXISTS alone is not concurrency control.
            # This transaction contains no CREATE INDEX CONCURRENTLY.
            connection.execute("SELECT pg_advisory_xact_lock(726593927601)")
            connection.executescript(_postgres_schema())
            self._run_migrations()

    def close(self) -> None:
        self.pool.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        if current_write_fence() is not None:
            with self.transaction() as connection:
                connection.execute(sql, params)
            return
        with self._connection() as connection:
            connection.execute(sql, params)

    def execute_count(self, sql: str, params: Iterable[Any] = ()) -> int:
        if current_write_fence() is not None:
            with self.transaction() as connection:
                return connection.execute(sql, params).rowcount
        with self._connection() as connection:
            cursor = connection.execute(sql, params)
            return cursor.rowcount

    def execute_many(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        if current_write_fence() is not None:
            with self.transaction() as connection:
                connection.executemany(sql, rows)
            return
        with self._connection() as connection:
            connection.executemany(sql, rows)

    def fetch_one(
        self, sql: str, params: Iterable[Any] = ()
    ) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(sql, params).fetchone()
            return self._decode(row) if row else None

    def fetch_all(
        self, sql: str, params: Iterable[Any] = ()
    ) -> List[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(sql, params).fetchall()
            return [self._decode(row) for row in rows]

    @property
    def in_transaction(self):
        return self._active_connection.get() is not None

    @contextmanager
    def transaction(self):
        active = self._active_connection.get()
        if active is not None:
            depth_token = self._transaction_depth.set(self._transaction_depth.get() + 1)
            try:
                yield _ConnectionAdapter(active)
            finally:
                self._transaction_depth.reset(depth_token)
            return
        with self.pool.connection() as raw:
            connection_token = self._active_connection.set(raw)
            depth_token = self._transaction_depth.set(1)
            try:
                with raw.transaction():
                    connection = _ConnectionAdapter(raw)
                    validate_write_fence(connection, self.dialect)
                    yield connection
            finally:
                self._transaction_depth.reset(depth_token)
                self._active_connection.reset(connection_token)

    @contextmanager
    def _connection(self):
        active = self._active_connection.get()
        if active is not None:
            yield _ConnectionAdapter(active)
            return
        with self.pool.connection() as raw:
            yield _ConnectionAdapter(raw)

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        exists = self.fetch_one(
            """SELECT 1 AS value FROM information_schema.columns
               WHERE table_schema=current_schema() AND table_name=? AND column_name=?""",
            (table, column),
        )
        if not exists:
            self.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    @staticmethod
    def _decode(row: Any) -> Dict[str, Any]:
        return Database._decode(row)

    def ping(self) -> bool:
        return bool(self.fetch_one("SELECT 1 AS value"))

    def current_time(self):
        return self.fetch_one("SELECT clock_timestamp() AS now")["now"].astimezone(timezone.utc)

    def schema_versions(self) -> list[int]:
        try:
            return super().schema_versions()
        except Exception as error:
            if error.__class__.__module__.startswith("psycopg"):
                return []
            raise
