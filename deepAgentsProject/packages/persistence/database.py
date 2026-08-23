from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  draft_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'DRAFT',
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agents_scope ON agents(tenant_id, project_id);

CREATE TABLE IF NOT EXISTS agent_revisions (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL REFERENCES agents(id),
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  revision_number INTEGER NOT NULL,
  spec_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(agent_id, revision_number)
);

CREATE TABLE IF NOT EXISTS resolved_execution_plans (
  id TEXT PRIMARY KEY,
  agent_revision_id TEXT NOT NULL UNIQUE REFERENCES agent_revisions(id),
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  plan_hash TEXT NOT NULL UNIQUE,
  plan_json TEXT NOT NULL,
  runtime_image_digest TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_deployments (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  agent_id TEXT NOT NULL REFERENCES agents(id),
  agent_revision_id TEXT NOT NULL REFERENCES agent_revisions(id),
  resolved_plan_id TEXT NOT NULL REFERENCES resolved_execution_plans(id),
  name TEXT NOT NULL,
  environment TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS threads (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  agent_deployment_id TEXT NOT NULL REFERENCES agent_deployments(id),
  title TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  thread_id TEXT NOT NULL REFERENCES threads(id),
  agent_deployment_id TEXT NOT NULL REFERENCES agent_deployments(id),
  resolved_plan_id TEXT NOT NULL REFERENCES resolved_execution_plans(id),
  status TEXT NOT NULL,
  input TEXT NOT NULL,
  output TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  checkpoint_json TEXT NOT NULL DEFAULT '{}',
  current_attempt_id TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_scope ON runs(tenant_id, project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS run_attempts (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  attempt_number INTEGER NOT NULL,
  status TEXT NOT NULL,
  worker_id TEXT,
  lease_token TEXT,
  acquired_at TEXT,
  heartbeat_at TEXT,
  expires_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(run_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS run_events (
  event_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  sequence INTEGER NOT NULL,
  event_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(run_id, sequence)
);

CREATE TABLE IF NOT EXISTS interrupts (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  run_id TEXT NOT NULL REFERENCES runs(id),
  checkpoint_id TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  policy_reason TEXT NOT NULL,
  status TEXT NOT NULL,
  actions_json TEXT NOT NULL,
  decision_json TEXT,
  expires_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_ledger (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  run_id TEXT NOT NULL REFERENCES runs(id),
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  model_calls INTEGER NOT NULL,
  tool_calls INTEGER NOT NULL,
  subagent_calls INTEGER NOT NULL,
  cost REAL NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  run_id TEXT NOT NULL REFERENCES runs(id),
  name TEXT NOT NULL,
  media_type TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_records (
  tenant_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  key TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, scope, key)
);

CREATE TABLE IF NOT EXISTS model_deployments (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  name TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  endpoint_region TEXT NOT NULL,
  status TEXT NOT NULL,
  capabilities_json TEXT NOT NULL,
  pricing_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


JSON_COLUMNS = {
    "draft_json": "draft",
    "spec_json": "spec",
    "plan_json": "plan",
    "metadata_json": "metadata",
    "checkpoint_json": "checkpoint",
    "actions_json": "actions",
    "decision_json": "decision",
    "capabilities_json": "capabilities",
    "pricing_json": "pricing",
    "event_json": "event",
}


class Database:
    """Small SQLite repository used by the runnable reference implementation.

    SQL is deliberately kept behind this class so PostgreSQL repositories can replace
    it without leaking persistence details into services or the runtime adapter.
    """

    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()

    def initialize(self) -> None:
        with self.lock:
            self.connection.executescript(SCHEMA)
            self.connection.commit()

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self.lock:
            self.connection.execute(sql, tuple(params))
            self.connection.commit()

    def execute_many(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self.lock:
            self.connection.executemany(sql, rows)
            self.connection.commit()

    def fetch_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.connection.execute(sql, tuple(params)).fetchone()
        return self._decode(row) if row else None

    def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(sql, tuple(params)).fetchall()
        return [self._decode(row) for row in rows]

    def transaction(self):
        return self.connection

    @staticmethod
    def encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _decode(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        for column, target in JSON_COLUMNS.items():
            if column in result:
                raw = result.pop(column)
                if raw is not None:
                    try:
                        result[target] = json.loads(raw)
                    except json.JSONDecodeError:
                        result[target] = raw
                else:
                    result[target] = None
        return result
