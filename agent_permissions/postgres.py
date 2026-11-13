"""PostgreSQL 18 persistence, preserving the application's write serialization."""

from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg

from agent_permissions.database import RBAC_SCHEMA, SCHEMA, Database

# Fixed application namespace, shared across processes and schema revisions.
WRITE_LOCK = (0x41555052, 2)


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

    def execute(
        self, statement: str, parameters: Sequence[Any] | None = None
    ) -> Result:
        # Application SQL uses qmark parameters; values are always sent to the
        # driver separately and never interpolated into the SQL text.
        query = statement.replace("?", "%s") if parameters is not None else statement
        return Result(self.native.execute(query, parameters))

    def executemany(
        self, statement: str, parameters: Iterable[Sequence[Any]]
    ) -> Result:
        cursor = self.native.cursor()
        cursor.executemany(statement.replace("?", "%s"), parameters)
        return Result(cursor)


class PostgresDatabase:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    @contextmanager
    def connect(self) -> Iterator[PostgresConnection]:
        with psycopg.connect(
            self.database_url,
            autocommit=True,
            connect_timeout=10,
            application_name="agent-users",
        ) as native:
            yield PostgresConnection(native)

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[PostgresConnection]:
        with self.connect() as connection:
            connection.execute(
                "BEGIN" if write else "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            try:
                connection.execute("SET LOCAL lock_timeout='10s'")
                connection.execute("SET LOCAL statement_timeout='30s'")
                if write:
                    # Lock before the first authorization read. All mutators,
                    # login, rate limiting and migrations use this same lock.
                    connection.execute("SELECT pg_advisory_xact_lock(?,?)", WRITE_LOCK)
                yield connection
                connection.native.commit()
            except BaseException:
                connection.native.rollback()
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
