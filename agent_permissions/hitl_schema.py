"""Portable SQL for resumable Agent runs; migrated with the business database."""

HITL_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_chat_runs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    conversation_id TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    graph_version INTEGER NOT NULL DEFAULT 1,
    lease_token TEXT,
    lease_until REAL NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    output TEXT NOT NULL DEFAULT '',
    finish_reason TEXT NOT NULL DEFAULT 'stop',
    usage TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    purged INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id,client_request_id)
);
CREATE INDEX IF NOT EXISTS agent_runs_owner ON agent_chat_runs(user_id,conversation_id,created_at);
CREATE UNIQUE INDEX IF NOT EXISTS agent_runs_active ON agent_chat_runs(user_id,conversation_id)
    WHERE status IN ('running','waiting');
CREATE TABLE IF NOT EXISTS agent_interactions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_chat_runs(id) ON DELETE CASCADE,
    call_key TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    catalog_revision TEXT NOT NULL,
    document TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    submission_id TEXT,
    submission_digest TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_interactions_run ON agent_interactions(run_id,call_key,created_at);
CREATE TABLE IF NOT EXISTS agent_tool_executions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_chat_runs(id) ON DELETE CASCADE,
    interaction_id TEXT REFERENCES agent_interactions(id),
    call_key TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    arguments_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    service_result TEXT,
    result TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(run_id,call_key)
);
CREATE TABLE IF NOT EXISTS agent_checkpoints (
    thread_id TEXT NOT NULL REFERENCES agent_chat_runs(id) ON DELETE CASCADE,
    namespace TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    parent_id TEXT,
    kind TEXT NOT NULL,
    checkpoint TEXT NOT NULL,
    metadata TEXT NOT NULL,
    PRIMARY KEY(thread_id,namespace,checkpoint_id)
);
CREATE TABLE IF NOT EXISTS agent_checkpoint_writes (
    thread_id TEXT NOT NULL REFERENCES agent_chat_runs(id) ON DELETE CASCADE,
    namespace TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    channel TEXT NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY(thread_id,namespace,checkpoint_id,task_id,idx)
);
CREATE TABLE IF NOT EXISTS agent_workflow_entries (
    run_id TEXT NOT NULL REFERENCES agent_chat_runs(id) ON DELETE CASCADE,
    entry_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    document TEXT NOT NULL,
    PRIMARY KEY(run_id,entry_id)
);
"""
