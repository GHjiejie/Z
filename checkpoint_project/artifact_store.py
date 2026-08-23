"""Persistent immutable artifacts produced by conversation tools."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MAX_HTML_BYTES = 256 * 1024
MAX_TITLE_CHARACTERS = 120


class ArtifactValidationError(ValueError):
    """Raised when a requested artifact does not satisfy the public contract."""


class ArtifactConflictError(RuntimeError):
    """Raised when replaying one tool call with different artifact content."""


@dataclass(frozen=True, slots=True)
class Artifact:
    artifact_id: str
    owner_thread_id: str
    tool_call_id: str
    kind: str
    mime_type: str
    title: str
    content: str
    content_sha256: str
    byte_size: int
    parent_artifact_id: str | None
    created_at: str

    def public_ref(self) -> dict[str, object]:
        """Return the small, safe reference stored in graph messages."""
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "mime_type": self.mime_type,
            "title": self.title,
            "byte_size": self.byte_size,
            "parent_artifact_id": self.parent_artifact_id,
            "created_at": self.created_at,
        }

    def public_record(self) -> dict[str, object]:
        """Return the API representation including the untrusted source text."""
        return {
            **self.public_ref(),
            "content": self.content,
            "content_sha256": self.content_sha256,
        }


class ArtifactStore:
    """Store immutable artifacts and per-session access grants in SQLite."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.expanduser().resolve()
        self._setup()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _setup(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    owner_thread_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    parent_artifact_id TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(owner_thread_id, tool_call_id)
                );

                CREATE INDEX IF NOT EXISTS idx_artifacts_owner
                ON artifacts(owner_thread_id, created_at);

                CREATE TABLE IF NOT EXISTS session_artifacts (
                    thread_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(thread_id, artifact_id)
                );
                """
            )

    def create_or_get_html(
        self,
        *,
        thread_id: str,
        tool_call_id: str,
        title: str,
        html: str,
        parent_artifact_id: str | None = None,
    ) -> Artifact:
        """Create an HTML artifact, or return the identical replayed call result."""
        normalized_title = title.strip()
        normalized_parent = (parent_artifact_id or "").strip() or None
        if not normalized_title:
            raise ArtifactValidationError("页面标题不能为空")
        if len(normalized_title) > MAX_TITLE_CHARACTERS:
            raise ArtifactValidationError(
                f"页面标题不能超过 {MAX_TITLE_CHARACTERS} 个字符"
            )
        if not html.strip():
            raise ArtifactValidationError("HTML 内容不能为空")
        if "\0" in html:
            raise ArtifactValidationError("HTML 内容不能包含空字符")
        encoded = html.encode("utf-8")
        if len(encoded) > MAX_HTML_BYTES:
            raise ArtifactValidationError(
                f"HTML 不能超过 {MAX_HTML_BYTES // 1024} KiB"
            )
        if not tool_call_id or len(tool_call_id) > 256:
            raise ArtifactValidationError("工具调用 ID 无效")

        content_sha256 = hashlib.sha256(encoded).hexdigest()
        now = datetime.now(UTC).isoformat(timespec="microseconds")

        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing_row = connection.execute(
                    """
                    SELECT * FROM artifacts
                    WHERE owner_thread_id = ? AND tool_call_id = ?
                    """,
                    (thread_id, tool_call_id),
                ).fetchone()
                if existing_row is not None:
                    existing = _artifact_from_row(existing_row)
                    if (
                        existing.content_sha256 != content_sha256
                        or existing.title != normalized_title
                        or existing.parent_artifact_id != normalized_parent
                    ):
                        raise ArtifactConflictError(
                            "同一工具调用产生了不同的 HTML 内容"
                        )
                    self._grant(connection, thread_id, existing.artifact_id, now)
                    connection.commit()
                    return existing

                if normalized_parent:
                    parent = connection.execute(
                        """
                        SELECT 1
                        FROM artifacts AS artifact
                        JOIN session_artifacts AS access
                          ON access.artifact_id = artifact.artifact_id
                        WHERE access.thread_id = ?
                          AND artifact.artifact_id = ?
                          AND artifact.kind = 'html'
                        """,
                        (thread_id, normalized_parent),
                    ).fetchone()
                    if parent is None:
                        raise ArtifactValidationError(
                            "父 artifact 不存在、不可访问或不是 HTML"
                        )

                artifact = Artifact(
                    artifact_id=f"art_{uuid.uuid4().hex}",
                    owner_thread_id=thread_id,
                    tool_call_id=tool_call_id,
                    kind="html",
                    mime_type="text/html",
                    title=normalized_title,
                    content=html,
                    content_sha256=content_sha256,
                    byte_size=len(encoded),
                    parent_artifact_id=normalized_parent,
                    created_at=now,
                )
                connection.execute(
                    """
                    INSERT INTO artifacts (
                        artifact_id, owner_thread_id, tool_call_id, kind,
                        mime_type, title, content, content_sha256, byte_size,
                        parent_artifact_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.artifact_id,
                        artifact.owner_thread_id,
                        artifact.tool_call_id,
                        artifact.kind,
                        artifact.mime_type,
                        artifact.title,
                        artifact.content,
                        artifact.content_sha256,
                        artifact.byte_size,
                        artifact.parent_artifact_id,
                        artifact.created_at,
                    ),
                )
                self._grant(connection, thread_id, artifact.artifact_id, now)
                connection.commit()
                return artifact
            except BaseException:
                connection.rollback()
                raise

    def get(self, thread_id: str, artifact_id: str) -> Artifact | None:
        """Return an artifact only when the session has an explicit grant."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT artifact.*
                FROM artifacts AS artifact
                JOIN session_artifacts AS access
                  ON access.artifact_id = artifact.artifact_id
                WHERE access.thread_id = ? AND artifact.artifact_id = ?
                """,
                (thread_id, artifact_id),
            ).fetchone()
        return _artifact_from_row(row) if row is not None else None

    def list_for_session(self, thread_id: str) -> list[Artifact]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT artifact.*
                FROM artifacts AS artifact
                JOIN session_artifacts AS access
                  ON access.artifact_id = artifact.artifact_id
                WHERE access.thread_id = ?
                ORDER BY artifact.created_at, artifact.artifact_id
                """,
                (thread_id,),
            ).fetchall()
        return [_artifact_from_row(row) for row in rows]

    def grant_to_session(self, thread_id: str, artifact_ids: list[str]) -> None:
        """Allow a forked session to resolve artifacts present in copied state."""
        if not artifact_ids:
            return
        now = datetime.now(UTC).isoformat(timespec="microseconds")
        with closing(self._connect()) as connection, connection:
            for artifact_id in dict.fromkeys(artifact_ids):
                self._grant(connection, thread_id, artifact_id, now)

    @staticmethod
    def _grant(
        connection: sqlite3.Connection,
        thread_id: str,
        artifact_id: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO session_artifacts (
                thread_id, artifact_id, created_at
            )
            SELECT ?, artifact_id, ?
            FROM artifacts
            WHERE artifact_id = ?
            """,
            (thread_id, created_at, artifact_id),
        )


def _artifact_from_row(row: sqlite3.Row) -> Artifact:
    return Artifact(**dict(row))
