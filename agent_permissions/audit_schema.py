"""Source outbox and independently owned audit database schemas."""

SOURCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_audit_call_refs (
    call_id TEXT PRIMARY KEY, run_id TEXT, call_key TEXT, entrypoint TEXT NOT NULL,
    sequence INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'requested', UNIQUE(run_id,call_key)
);
CREATE TABLE IF NOT EXISTS agent_audit_outbox (
    event_id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES agent_audit_call_refs(call_id),
    fact_key TEXT UNIQUE, payload TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
    available_at DOUBLE PRECISION NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    lease TEXT, state TEXT NOT NULL DEFAULT 'pending', delivered_at DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS agent_audit_ready ON agent_audit_outbox(state,available_at);
CREATE INDEX IF NOT EXISTS agent_audit_call_events ON agent_audit_outbox(call_id);
"""
SOURCE_COLUMNS = {
    "agent_audit_call_refs": (
        "call_id",
        "run_id",
        "call_key",
        "entrypoint",
        "sequence",
        "state",
    ),
    "agent_audit_outbox": (
        "event_id",
        "call_id",
        "fact_key",
        "payload",
        "created_at",
        "available_at",
        "attempts",
        "lease",
        "state",
        "delivered_at",
    ),
}

STORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_audit_events (
    producer_id TEXT NOT NULL, event_id TEXT NOT NULL, call_id TEXT NOT NULL, sequence INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1, received_at TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(producer_id,event_id), UNIQUE(call_id,sequence)
);
CREATE TABLE IF NOT EXISTS agent_audit_retired_events (
    producer_id TEXT NOT NULL, event_id TEXT NOT NULL, call_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
    PRIMARY KEY(producer_id,event_id)
);
CREATE INDEX IF NOT EXISTS audit_retired_calls ON agent_audit_retired_events(call_id);
CREATE TABLE IF NOT EXISTS agent_audit_maintenance (
    id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, deleted_events INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_audit_operations (
    call_id TEXT PRIMARY KEY, parent_call_id TEXT, started_at TEXT NOT NULL, finished_at TEXT,
    actor_user_id TEXT, subject_user_id TEXT, agent_id TEXT, operation_id TEXT, target_id TEXT, run_id TEXT,
    business_outcome TEXT NOT NULL, document TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_op_time ON agent_audit_operations(started_at,call_id);
CREATE INDEX IF NOT EXISTS audit_op_actor ON agent_audit_operations(actor_user_id,started_at,call_id);
CREATE INDEX IF NOT EXISTS audit_op_subject ON agent_audit_operations(subject_user_id,started_at,call_id);
CREATE INDEX IF NOT EXISTS audit_op_operation ON agent_audit_operations(operation_id,started_at,call_id);
CREATE INDEX IF NOT EXISTS audit_op_target ON agent_audit_operations(target_id,started_at,call_id);
CREATE INDEX IF NOT EXISTS audit_op_outcome ON agent_audit_operations(business_outcome,started_at,call_id);
CREATE INDEX IF NOT EXISTS audit_op_run ON agent_audit_operations(run_id);
CREATE INDEX IF NOT EXISTS audit_children ON agent_audit_operations(parent_call_id);
CREATE TABLE IF NOT EXISTS agent_audit_grant_changes (
    call_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, actor_user_id TEXT NOT NULL, subject_user_id TEXT NOT NULL,
    operation_id TEXT NOT NULL, grant_version INTEGER NOT NULL, document TEXT NOT NULL,
    UNIQUE(subject_user_id,operation_id,grant_version)
);
CREATE INDEX IF NOT EXISTS audit_grant_actor ON agent_audit_grant_changes(actor_user_id,started_at,call_id);
CREATE INDEX IF NOT EXISTS audit_grant_time ON agent_audit_grant_changes(started_at,call_id);
"""
