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

CREATE TABLE IF NOT EXISTS intent_router_revisions (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
  revision_number INTEGER NOT NULL,
  taxonomy_version TEXT NOT NULL,
  mode TEXT NOT NULL,
  config_json TEXT NOT NULL,
  model_snapshot_json TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(tenant_id, project_id, environment_id, revision_number)
);
CREATE INDEX IF NOT EXISTS idx_intent_router_revisions_scope
  ON intent_router_revisions(tenant_id, project_id, environment_id, status);

CREATE TABLE IF NOT EXISTS intent_routing_decisions (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
  router_revision_id TEXT NOT NULL REFERENCES intent_router_revisions(id),
  input_hash TEXT NOT NULL,
  classification_json TEXT NOT NULL,
  status TEXT NOT NULL,
  selected_deployment_id TEXT REFERENCES agent_deployments(id),
  predicted_deployment_id TEXT REFERENCES agent_deployments(id),
  candidate_deployments_json TEXT NOT NULL DEFAULT '[]',
  reason TEXT NOT NULL,
  requirements_json TEXT NOT NULL DEFAULT '{}',
  override_deployment_id TEXT REFERENCES agent_deployments(id),
  thread_id TEXT,
  run_id TEXT,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  committed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_intent_routing_decisions_scope
  ON intent_routing_decisions(tenant_id, project_id, environment_id, created_at DESC);

CREATE TABLE IF NOT EXISTS repositories (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  name TEXT NOT NULL,
  provider TEXT NOT NULL,
  canonical_uri TEXT NOT NULL,
  default_branch TEXT NOT NULL,
  credential_ref TEXT,
  access_policy_revision_id TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_repositories_scope
  ON repositories(tenant_id, project_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS repository_snapshots (
  id TEXT PRIMARY KEY,
  repository_id TEXT NOT NULL REFERENCES repositories(id),
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  requested_ref TEXT NOT NULL,
  resolved_commit_sha TEXT NOT NULL,
  source_mode TEXT NOT NULL,
  manifest_hash TEXT NOT NULL,
  archive_path TEXT NOT NULL,
  archive_sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  file_count INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(repository_id, resolved_commit_sha, source_mode, manifest_hash)
);
CREATE INDEX IF NOT EXISTS idx_repository_snapshots_scope
  ON repository_snapshots(tenant_id, project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS threads (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  agent_deployment_id TEXT NOT NULL REFERENCES agent_deployments(id),
  repository_id TEXT REFERENCES repositories(id),
  repository_snapshot_id TEXT REFERENCES repository_snapshots(id),
  routing_decision_id TEXT REFERENCES intent_routing_decisions(id),
  title TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sandbox_instances (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  external_id TEXT,
  profile_json TEXT NOT NULL,
  provider_metadata_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sandbox_instances_scope
  ON sandbox_instances(tenant_id, project_id, status, expires_at);

CREATE TABLE IF NOT EXISTS coding_workspaces (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  thread_id TEXT NOT NULL UNIQUE REFERENCES threads(id),
  repository_snapshot_id TEXT NOT NULL REFERENCES repository_snapshots(id),
  sandbox_instance_id TEXT REFERENCES sandbox_instances(id),
  lifecycle TEXT NOT NULL,
  workspace_generation INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  last_checkpoint_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coding_workspaces_scope
  ON coding_workspaces(tenant_id, project_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS workspace_snapshots (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  run_id TEXT NOT NULL REFERENCES runs(id),
  workspace_id TEXT NOT NULL REFERENCES coding_workspaces(id),
  base_commit_sha TEXT NOT NULL,
  workspace_generation INTEGER NOT NULL,
  plan_hash TEXT NOT NULL,
  reason TEXT NOT NULL,
  archive_path TEXT NOT NULL,
  archive_sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workspace_snapshots_workspace
  ON workspace_snapshots(workspace_id, workspace_generation DESC, created_at DESC);

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
  principal_user_id TEXT NOT NULL DEFAULT 'user_demo',
  principal_roles_json TEXT NOT NULL DEFAULT '[]',
  principal_environment_id TEXT NOT NULL DEFAULT 'env_development',
  principal_verified INTEGER NOT NULL DEFAULT 0,
  coding_workspace_id TEXT REFERENCES coding_workspaces(id),
  routing_decision_id TEXT REFERENCES intent_routing_decisions(id),
  workspace_generation INTEGER,
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
  plan_hash TEXT,
  base_commit_sha TEXT,
  workspace_generation INTEGER,
  artifact_metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sandbox_commands (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  run_id TEXT NOT NULL REFERENCES runs(id),
  workspace_id TEXT NOT NULL REFERENCES coding_workspaces(id),
  command_hash TEXT NOT NULL,
  command_preview TEXT NOT NULL,
  working_directory TEXT NOT NULL,
  status TEXT NOT NULL,
  exit_code INTEGER,
  duration_ms INTEGER,
  output_artifact_id TEXT REFERENCES artifacts(id),
  resource_usage_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sandbox_commands_run
  ON sandbox_commands(run_id, created_at);

CREATE TABLE IF NOT EXISTS workspace_file_changes (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  run_id TEXT NOT NULL REFERENCES runs(id),
  workspace_id TEXT NOT NULL REFERENCES coding_workspaces(id),
  path TEXT NOT NULL,
  operation TEXT NOT NULL,
  before_hash TEXT,
  after_hash TEXT,
  workspace_generation INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workspace_file_changes_run
  ON workspace_file_changes(run_id, workspace_generation);

CREATE TABLE IF NOT EXISTS verification_reports (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
  workspace_id TEXT NOT NULL REFERENCES coding_workspaces(id),
  status TEXT NOT NULL,
  checks_json TEXT NOT NULL,
  summary_json TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_sets (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  run_id TEXT NOT NULL REFERENCES runs(id),
  workspace_id TEXT NOT NULL REFERENCES coding_workspaces(id),
  base_commit_sha TEXT NOT NULL,
  workspace_generation INTEGER NOT NULL,
  patch_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
  diff_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
  verification_report_id TEXT REFERENCES verification_reports(id),
  diff_stat_json TEXT NOT NULL,
  changed_files_json TEXT NOT NULL,
  status TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  plan_hash TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_change_sets_run ON change_sets(run_id, created_at DESC);

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
  context_window_tokens INTEGER NOT NULL DEFAULT 131072,
  capabilities_json TEXT NOT NULL,
  pricing_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plugins (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  version TEXT NOT NULL,
  description TEXT NOT NULL,
  source_path TEXT NOT NULL,
  manifest_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  loaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skills (
  id TEXT PRIMARY KEY,
  plugin_id TEXT NOT NULL REFERENCES plugins(id),
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  current_version_id TEXT NOT NULL,
  tags_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  builtin INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skills_plugin ON skills(plugin_id, status, name);

CREATE TABLE IF NOT EXISTS skill_versions (
  id TEXT PRIMARY KEY,
  skill_id TEXT NOT NULL REFERENCES skills(id),
  version TEXT NOT NULL,
  artifact_hash TEXT NOT NULL,
  content TEXT NOT NULL,
  source_path TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(skill_id, version)
);
CREATE INDEX IF NOT EXISTS idx_skill_versions_skill ON skill_versions(skill_id, created_at DESC);

CREATE TABLE IF NOT EXISTS knowledge_bases (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  current_revision_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_bases_scope
  ON knowledge_bases(tenant_id, project_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS knowledge_documents (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id),
  display_name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  source_type TEXT NOT NULL DEFAULT 'upload',
  current_version_id TEXT,
  status TEXT NOT NULL DEFAULT 'PENDING_UPLOAD',
  visibility TEXT NOT NULL DEFAULT 'project',
  allowed_roles_json TEXT NOT NULL DEFAULT '[]',
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_documents_scope
  ON knowledge_documents(tenant_id, project_id, knowledge_base_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS knowledge_document_versions (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  revision_number INTEGER NOT NULL,
  storage_provider TEXT NOT NULL,
  bucket TEXT NOT NULL,
  region TEXT NOT NULL,
  object_key TEXT NOT NULL,
  object_version_id TEXT,
  canonical_uri TEXT NOT NULL,
  etag TEXT,
  content_sha256 TEXT,
  expected_sha256 TEXT,
  content_type TEXT NOT NULL,
  size_bytes INTEGER,
  expected_size_bytes INTEGER NOT NULL,
  storage_class TEXT,
  parser_version TEXT,
  chunker_version TEXT,
  embedding_revision_id TEXT,
  status TEXT NOT NULL DEFAULT 'PENDING_UPLOAD',
  error_code TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  uploaded_at TEXT,
  indexed_at TEXT,
  UNIQUE(document_id, revision_number),
  UNIQUE(storage_provider, bucket, object_key)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_document_versions_scope
  ON knowledge_document_versions(tenant_id, project_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS knowledge_base_revisions (
  id TEXT PRIMARY KEY,
  knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id),
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  revision_number INTEGER NOT NULL,
  status TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  retrieval_profile_json TEXT NOT NULL,
  embedding_model TEXT NOT NULL,
  embedding_dimensions INTEGER NOT NULL,
  index_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  activated_at TEXT,
  deprecated_at TEXT,
  UNIQUE(knowledge_base_id, revision_number)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_base_revisions_scope
  ON knowledge_base_revisions(tenant_id, project_id, knowledge_base_id, revision_number DESC);

CREATE TABLE IF NOT EXISTS knowledge_revision_documents (
  revision_id TEXT NOT NULL REFERENCES knowledge_base_revisions(id),
  document_version_id TEXT NOT NULL REFERENCES knowledge_document_versions(id),
  PRIMARY KEY(revision_id, document_version_id)
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id),
  document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
  document_version_id TEXT NOT NULL REFERENCES knowledge_document_versions(id),
  position INTEGER NOT NULL,
  text TEXT NOT NULL,
  token_count INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  locator_json TEXT NOT NULL,
  embedding_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(document_version_id, position)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_scope
  ON knowledge_chunks(tenant_id, project_id, knowledge_base_id, document_version_id);

CREATE TABLE IF NOT EXISTS knowledge_ingestion_jobs (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(id),
  document_version_id TEXT NOT NULL UNIQUE REFERENCES knowledge_document_versions(id),
  status TEXT NOT NULL,
  stage TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  worker_id TEXT,
  lease_token TEXT,
  heartbeat_at TEXT,
  error_code TEXT,
  error_message TEXT,
  chunk_count INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_ingestion_jobs_scope
  ON knowledge_ingestion_jobs(tenant_id, project_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS knowledge_retrieval_audits (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  run_id TEXT,
  query_hash TEXT NOT NULL,
  revision_ids_json TEXT NOT NULL,
  result_count INTEGER NOT NULL,
  latency_ms INTEGER NOT NULL,
  hits_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_retrieval_audits_scope
  ON knowledge_retrieval_audits(tenant_id, project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS knowledge_events (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  knowledge_base_id TEXT,
  document_version_id TEXT,
  ingestion_job_id TEXT,
  type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_events_scope
  ON knowledge_events(tenant_id, project_id, created_at DESC);
"""


JSON_COLUMNS = {
    "draft_json": "draft",
    "spec_json": "spec",
    "plan_json": "plan",
    "metadata_json": "metadata",
    "artifact_metadata_json": "artifact_metadata",
    "principal_roles_json": "principal_roles",
    "profile_json": "profile",
    "provider_metadata_json": "provider_metadata",
    "resource_usage_json": "resource_usage",
    "checks_json": "checks",
    "summary_json": "summary",
    "diff_stat_json": "diff_stat",
    "changed_files_json": "changed_files",
    "checkpoint_json": "checkpoint",
    "actions_json": "actions",
    "decision_json": "decision",
    "capabilities_json": "capabilities",
    "pricing_json": "pricing",
    "event_json": "event",
    "tags_json": "tags",
    "allowed_roles_json": "allowed_roles",
    "manifest_json": "manifest",
    "retrieval_profile_json": "retrieval_profile",
    "locator_json": "locator",
    "embedding_json": "embedding",
    "revision_ids_json": "revision_ids",
    "hits_json": "hits",
    "payload_json": "payload",
    "config_json": "config",
    "model_snapshot_json": "model_snapshot",
    "classification_json": "classification",
    "candidate_deployments_json": "candidate_deployments",
    "requirements_json": "requirements",
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
            self._ensure_column("knowledge_base_revisions", "deprecated_at", "TEXT")
            self._ensure_column("knowledge_ingestion_jobs", "chunk_count", "INTEGER")
            self._ensure_column(
                "runs", "principal_user_id", "TEXT NOT NULL DEFAULT 'user_demo'"
            )
            self._ensure_column(
                "runs", "principal_roles_json", "TEXT NOT NULL DEFAULT '[]'"
            )
            self._ensure_column(
                "runs",
                "principal_environment_id",
                "TEXT NOT NULL DEFAULT 'env_development'",
            )
            self._ensure_column(
                "runs", "principal_verified", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column("threads", "repository_id", "TEXT")
            self._ensure_column("threads", "repository_snapshot_id", "TEXT")
            self._ensure_column("threads", "routing_decision_id", "TEXT")
            self._ensure_column("runs", "coding_workspace_id", "TEXT")
            self._ensure_column("runs", "routing_decision_id", "TEXT")
            self._ensure_column("runs", "workspace_generation", "INTEGER")
            self._ensure_column("artifacts", "plan_hash", "TEXT")
            self._ensure_column("artifacts", "base_commit_sha", "TEXT")
            self._ensure_column("artifacts", "workspace_generation", "INTEGER")
            self._ensure_column(
                "artifacts", "artifact_metadata_json", "TEXT NOT NULL DEFAULT '{}'"
            )
            self._ensure_column(
                "sandbox_instances", "provider_metadata_json", "TEXT NOT NULL DEFAULT '{}'"
            )
            self._ensure_column(
                "model_deployments",
                "context_window_tokens",
                "INTEGER NOT NULL DEFAULT 131072",
            )
            self._ensure_column("change_sets", "plan_hash", "TEXT NOT NULL DEFAULT ''")
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

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

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
