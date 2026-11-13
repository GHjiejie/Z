-- Original user-management schema, for migration regression tests.

BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    email TEXT,
    password_hash TEXT NOT NULL,
    roles TEXT NOT NULL CHECK(json_valid(roles)),
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
PRAGMA user_version = 1;
COMMIT;
