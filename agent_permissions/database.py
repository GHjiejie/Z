"""SQLite persistence with database-wide serialization of authority changes."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from agent_permissions.permissions import (
    ADMIN_PERMISSIONS,
    MEMBER_PERMISSIONS,
    PERMISSION_CATALOG,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    email TEXT,
    password_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'disabled')),
    must_change_password INTEGER NOT NULL CHECK(must_change_password IN (0, 1)),
    version INTEGER NOT NULL CHECK(version >= 1),
    last_login_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token_digest TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    scope TEXT NOT NULL CHECK(scope IN ('full', 'password_change')),
    expires_at REAL NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    actor_user_id TEXT,
    target_user_id TEXT,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason_code TEXT,
    request_id TEXT NOT NULL,
    source TEXT NOT NULL,
    changes TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_time ON audit_events(created_at);
CREATE TABLE IF NOT EXISTS rate_limits (
    bucket_key TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_expiry ON rate_limits(expires_at);
"""

RBAC_SCHEMA = """
CREATE TABLE IF NOT EXISTS roles (
    code TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL,
    built_in INTEGER NOT NULL CHECK(built_in IN (0,1)),
    version INTEGER NOT NULL CHECK(version>=1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS permissions (
    code TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    resource TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_roles (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_code TEXT NOT NULL REFERENCES roles(code) ON DELETE RESTRICT,
    PRIMARY KEY(user_id,role_code)
);
CREATE INDEX IF NOT EXISTS user_roles_role ON user_roles(role_code);
CREATE TABLE IF NOT EXISTS role_permissions (
    role_code TEXT NOT NULL REFERENCES roles(code) ON DELETE CASCADE,
    permission_code TEXT NOT NULL REFERENCES permissions(code) ON DELETE RESTRICT,
    PRIMARY KEY(role_code,permission_code)
);
CREATE INDEX IF NOT EXISTS role_permissions_permission ON role_permissions(permission_code);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.touch(mode=0o600, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2):
                    raise RuntimeError("Unsupported user database schema version")
                if version == 0:
                    self._execute_schema(connection, SCHEMA)
                self._execute_schema(connection, RBAC_SCHEMA)
                self._seed_catalog(connection)
                if version == 1:
                    for user in connection.execute(
                        "SELECT id,roles FROM users WHERE deleted_at IS NULL"
                    ):
                        if not json.loads(user["roles"]):
                            raise RuntimeError("Legacy user has no roles")
                        connection.executemany(
                            "INSERT INTO user_roles(user_id,role_code) VALUES(?,?)",
                            [
                                (user["id"], code)
                                for code in set(json.loads(user["roles"]))
                            ],
                        )
                    connection.execute("ALTER TABLE users DROP COLUMN roles")
                connection.execute("PRAGMA user_version=2")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _execute_schema(connection: sqlite3.Connection, schema: str) -> None:
        # executescript commits an open transaction; execute DDL individually so
        # the role backfill, old-column removal and version change are atomic.
        for statement in schema.split(";"):
            if statement.strip():
                connection.execute(statement)

    @staticmethod
    def _seed_catalog(connection: sqlite3.Connection) -> None:
        connection.executemany(
            """INSERT INTO permissions(code,display_name,resource) VALUES(?,?,?)
            ON CONFLICT(code) DO UPDATE SET
            display_name=excluded.display_name,resource=excluded.resource""",
            PERMISSION_CATALOG,
        )
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        for code, name, description, permissions in [
            ("admin", "管理员", "管理系统用户、角色与权限", ADMIN_PERMISSIONS),
            ("member", "普通用户", "管理自己的基本资料和密码", MEMBER_PERMISSIONS),
        ]:
            inserted = connection.execute(
                """INSERT OR IGNORE INTO roles
                (code,display_name,description,built_in,version,created_at,updated_at)
                VALUES(?,?,?,1,1,?,?)""",
                (code, name, description, now, now),
            ).rowcount
            if inserted or code == "admin":
                connection.executemany(
                    """INSERT OR IGNORE INTO role_permissions(role_code,permission_code)
                    VALUES(?,?)""",
                    [(code, permission) for permission in permissions],
                )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.create_function(
                "casefold",
                1,
                lambda value: (value or "").casefold(),
                deterministic=True,
            )
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            # This lock also works across processes sharing the same local DB.
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
