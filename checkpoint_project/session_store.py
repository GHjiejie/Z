"""Small SQLite catalog for named chat sessions and branch provenance."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class Session:
    thread_id: str
    created_at: str
    source_thread_id: str | None
    source_checkpoint_id: str | None


class SessionStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._setup()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _setup(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    thread_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    source_thread_id TEXT,
                    source_checkpoint_id TEXT
                )
                """
            )

    def ensure(
        self,
        thread_id: str,
        *,
        source_thread_id: str | None = None,
        source_checkpoint_id: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO chat_sessions (
                    thread_id, created_at, source_thread_id, source_checkpoint_id
                ) VALUES (?, ?, ?, ?)
                """,
                (thread_id, now, source_thread_id, source_checkpoint_id),
            )

    def list(self) -> list[Session]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT thread_id, created_at, source_thread_id, source_checkpoint_id
                FROM chat_sessions
                ORDER BY created_at, thread_id
                """
            ).fetchall()
        return [Session(**dict(row)) for row in rows]

    def exists(self, thread_id: str) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM chat_sessions WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return row is not None
