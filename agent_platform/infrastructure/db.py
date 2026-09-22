"""SQLAlchemy storage with serial transactions on SQLite and PostgreSQL."""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from threading import RLock

from sqlalchemy import MetaData, create_engine, event, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.pool import StaticPool

metadata = MetaData()


class Database:
    def __init__(self, url: str):
        parsed = make_url(url)
        if parsed.drivername == "postgresql":
            parsed = parsed.set(drivername="postgresql+psycopg")
        sqlite = parsed.get_backend_name() == "sqlite"
        if sqlite and parsed.database and parsed.database != ":memory:":
            Path(parsed.database).resolve().parent.mkdir(parents=True, exist_ok=True)
        options = {"pool_pre_ping": True}
        if sqlite:
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if parsed.database in (None, "", ":memory:"):
                options["poolclass"] = StaticPool
        self.engine = create_engine(parsed, **options)
        self.sqlite = sqlite
        # StaticPool shares a physical connection, including its transaction.
        self._memory_lock = RLock() if options.get("poolclass") is StaticPool else None
        if sqlite:

            @event.listens_for(self.engine, "connect")
            def pragmas(connection, _record):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.close()

    def create_schema(self) -> None:
        # Import table declarations before metadata.create_all; no business writes.
        from agent_platform.infrastructure import tables  # noqa: F401
        from agent_platform.modules.billing import service  # noqa: F401

        metadata.create_all(self.engine)
        if self.sqlite:
            with self.engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA journal_mode=WAL")
                connection.commit()

    @contextmanager
    def read(self) -> Iterator[Connection]:
        with (
            self._memory_lock if self._memory_lock else nullcontext(),
            self.engine.connect() as connection,
        ):
            yield connection

    @contextmanager
    def transaction(self, scope: str = "platform") -> Iterator[Connection]:
        with (
            self._memory_lock if self._memory_lock else nullcontext(),
            self.engine.connect() as connection,
        ):
            try:
                if self.sqlite:
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                else:
                    connection.begin()
                    lock_id = int.from_bytes(
                        hashlib.sha256(scope.encode()).digest()[:8], "big", signed=True
                    )
                    connection.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id}
                    )
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def close(self) -> None:
        self.engine.dispose()
