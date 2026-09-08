from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from packages.persistence.fencing import current_write_fence, validate_write_fence


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL COLLATE NOCASE UNIQUE,
  display_name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
  roles_json TEXT NOT NULL DEFAULT '[]',
  is_super_admin INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  version INTEGER NOT NULL DEFAULT 1,
  last_login_at TEXT,
  password_changed_at TEXT,
  password_expires_at TEXT,
  must_change_password INTEGER NOT NULL DEFAULT 1,
  failed_login_count INTEGER NOT NULL DEFAULT 0,
  last_failed_login_at TEXT,
  locked_until TEXT,
  deleted_at TEXT,
  deleted_by TEXT,
  deletion_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_scope
  ON users(tenant_id, project_id, status, username);

CREATE TABLE IF NOT EXISTS auth_sessions (
  id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  user_id TEXT NOT NULL REFERENCES users(id),
  expires_at TEXT NOT NULL,
  revoked_at TEXT,
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  ip_address TEXT,
  user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
  ON auth_sessions(user_id, expires_at, revoked_at);

CREATE TABLE IF NOT EXISTS auth_audit_events (
  id TEXT PRIMARY KEY,
  actor_user_id TEXT,
  target_user_id TEXT,
  tenant_id TEXT,
  project_id TEXT,
  action TEXT NOT NULL,
  outcome TEXT NOT NULL,
  ip_address TEXT,
  user_agent TEXT,
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_audit_events_scope
  ON auth_audit_events(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_auth_audit_events_target
  ON auth_audit_events(target_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS auth_login_limits (
  key_hash TEXT PRIMARY KEY,
  attempts INTEGER NOT NULL,
  window_started_at TEXT NOT NULL,
  blocked_until TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_queue (
  id TEXT PRIMARY KEY,
  queue_name TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  active_key TEXT UNIQUE,
  status TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  available_at TEXT NOT NULL,
  lease_owner TEXT,
  lease_expires_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_queue_claim
  ON task_queue(queue_name, status, available_at, priority DESC, created_at);

CREATE TABLE IF NOT EXISTS worker_nodes (
  id TEXT PRIMARY KEY,
  worker_type TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  stopped_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_worker_nodes_health
  ON worker_nodes(status, heartbeat_at DESC, worker_type);

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
    "limits_json": "limits",
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
    "roles_json": "roles",
    "details_json": "details",
    "cases_json": "cases",
    "evidence_json": "evidence",
    "model_identity_json": "model_identity",
    "limits_json": "limits",
    "requested_roles_json": "requested_roles",
    "runtime_binding_json": "runtime_binding",
}

LATEST_SCHEMA_VERSION = 21


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
        self._transaction_state = threading.local()
        self.dialect = "sqlite"

    def initialize(self, *, auto_migrate: bool = True) -> None:
        if not auto_migrate:
            self.assert_schema_current()
            return
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
            self._run_migrations()
            self.connection.commit()

    def schema_versions(self) -> list[int]:
        try:
            rows = self.fetch_all("SELECT version FROM schema_migrations ORDER BY version")
        except sqlite3.DatabaseError:
            return []
        return [int(row["version"]) for row in rows]

    def assert_schema_current(self) -> None:
        versions = self.schema_versions()
        if not versions or versions[-1] != LATEST_SCHEMA_VERSION:
            current = versions[-1] if versions else 0
            raise RuntimeError(
                f"Database schema is at version {current}; run the migration job for version {LATEST_SCHEMA_VERSION}"
            )

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        if current_write_fence() is not None:
            with self.transaction() as connection:
                connection.execute(sql, tuple(params))
            return
        with self.lock:
            self.connection.execute(sql, tuple(params))
            self._commit_if_outside_transaction()

    def execute_count(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Execute one write and return its affected-row count."""
        if current_write_fence() is not None:
            with self.transaction() as connection:
                return connection.execute(sql, tuple(params)).rowcount
        with self.lock:
            cursor = self.connection.execute(sql, tuple(params))
            self._commit_if_outside_transaction()
            return cursor.rowcount

    def execute_many(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        if current_write_fence() is not None:
            with self.transaction() as connection:
                connection.executemany(sql, rows)
            return
        with self.lock:
            self.connection.executemany(sql, rows)
            self._commit_if_outside_transaction()

    def fetch_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.connection.execute(sql, tuple(params)).fetchone()
        return self._decode(row) if row else None

    def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(sql, tuple(params)).fetchall()
        return [self._decode(row) for row in rows]

    @property
    def in_transaction(self):
        return bool(getattr(self._transaction_state, "depth", 0))

    @contextmanager
    def transaction(self):
        """Run a unit of work atomically, nesting safely within one thread."""

        with self.lock:
            depth = int(getattr(self._transaction_state, "depth", 0))
            if depth:
                self._transaction_state.depth = depth + 1
                try:
                    yield self.connection
                finally:
                    self._transaction_state.depth = depth
                return
            self.connection.execute("BEGIN IMMEDIATE")
            self._transaction_state.depth = 1
            try:
                validate_write_fence(self.connection, self.dialect)
                yield self.connection
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            finally:
                self._transaction_state.depth = 0

    def current_time(self) -> datetime:
        return datetime.now(timezone.utc)

    def assert_execution_fence(self) -> None:
        with self.transaction():
            pass

    def _commit_if_outside_transaction(self) -> None:
        if not int(getattr(self._transaction_state, "depth", 0)):
            self.connection.commit()

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _run_migrations(self) -> None:
        """Apply additive, versioned migrations for databases created by older builds."""
        applied = {
            int(row["version"])
            for row in self.connection.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        }
        migrations = (
            (1, "record-existing-platform-schema", self._migration_platform_baseline),
            (2, "auth-user-governance", self._migration_auth_user_governance),
            (3, "durable-task-queue", self._migration_durable_task_queue),
            (4, "worker-heartbeats", self._migration_worker_heartbeats),
            (5, "sandbox-execution-lease-authority", self._migration_sandbox_lease_authority),
            (6, "durable-model-budget", self._migration_model_budget),
            (7, "evaluation-release-gates", self._migration_evaluation_gates),
            (8, "coding-consistent-recovery", self._migration_coding_recovery),
            (9, "unified-metering-and-quotas", self._migration_billing),
            (10, "thread-access-and-source-provenance", self._migration_thread_access),
            (11, "routing-ownership-and-atomic-review", self._migration_atomic_review),
            (12, "complete-metering-attribution", self._migration_metering_attribution),
            (13, "immutable-model-bindings", self._migration_model_bindings),
            (14, "knowledge-metadata-access", self._migration_knowledge_metadata_access),
            (15, "governed-production-releases", self._migration_production_releases),
            (16, "outstanding-work-admission-indexes", self._migration_admission_indexes),
            (17, "governed-production-routing", self._migration_production_routing),
            (18, "durable-cancellation-finalization", self._migration_cancellation_finalization),
            (19, "durable-trace-origins", self._migration_trace_origins),
            (20, "bounded-knowledge-upload-pipeline", self._migration_upload_pipeline),
            (21, "repository-object-materializations", self._migration_repository_objects),
        )
        for version, name, migration in migrations:
            if version in applied:
                continue
            migration()
            self.connection.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES(?,?,datetime('now'))",
                (version, name),
            )

    def _migration_repository_objects(self) -> None:
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS repository_objects (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                archive_sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('UPLOADING','READY','UNCERTAIN')),
                archive_path TEXT, owner_token TEXT NOT NULL, created_by TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(tenant_id,project_id,archive_sha256)
            );
            CREATE INDEX IF NOT EXISTS idx_repository_objects_scope
                ON repository_objects(tenant_id,project_id,status);
        """)

    def _migration_upload_pipeline(self) -> None:
        self._ensure_column('knowledge_document_versions', 'upload_expires_at', 'TEXT')
        self._ensure_column('knowledge_document_versions', 'upload_request_hash', 'TEXT')
        # Give legacy pending grants one bounded migration window, never renew it
        # on application startup. Already accepted jobs retain their pinned source.
        self.connection.execute("""UPDATE knowledge_document_versions SET upload_expires_at=?
            WHERE upload_expires_at IS NULL AND status='PENDING_UPLOAD'""",
            ((datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),))
        self.connection.executescript("""
            CREATE INDEX IF NOT EXISTS idx_upload_intent_expiry
                ON knowledge_document_versions(upload_expires_at,id) WHERE status='PENDING_UPLOAD';
            CREATE INDEX IF NOT EXISTS idx_upload_retained_scope
                ON knowledge_document_versions(tenant_id,project_id,document_id);
            CREATE INDEX IF NOT EXISTS idx_ingestion_running_capacity
                ON knowledge_ingestion_jobs(tenant_id,project_id,requested_by) WHERE status='RUNNING';
        """)

    def _migration_production_routing(self) -> None:
        # Existing production profiles retain their data, not inferred approval.
        self._ensure_column("intent_router_revisions", "approval_state", "TEXT NOT NULL DEFAULT 'LEGACY'")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS routing_change_requests (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                environment TEXT NOT NULL CHECK(environment='production'), requested_by TEXT NOT NULL,
                snapshot_json TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('PENDING','APPLIED','REJECTED','CANCELLED')),
                version INTEGER NOT NULL DEFAULT 1, router_revision_id TEXT,
                decided_by TEXT, decision_reason TEXT, decided_at TEXT,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_routing_changes_scope
                ON routing_change_requests(tenant_id,project_id,created_at,id);
        """)

    def _migration_admission_indexes(self) -> None:
        # Keep quota and probe scans bounded by outstanding work, not all
        # retained conversations or historical ingestion jobs.
        self.connection.executescript("""
            CREATE INDEX IF NOT EXISTS idx_runs_outstanding_admission
                ON runs(tenant_id,project_id,principal_user_id,status)
                WHERE status NOT IN ('CANCELLED','TIMED_OUT','FAILED','FAILED_BUDGET','SUCCEEDED');
            CREATE INDEX IF NOT EXISTS idx_runs_pending_health
                ON runs(current_attempt_id)
                WHERE status IN ('CREATED','QUEUED','ORPHANED');
            CREATE INDEX IF NOT EXISTS idx_ingestion_outstanding_admission
                ON knowledge_ingestion_jobs(tenant_id,project_id,requested_by,status,updated_at)
                WHERE status IN ('QUEUED','RUNNING');
            CREATE INDEX IF NOT EXISTS idx_ingestion_pending_health
                ON knowledge_ingestion_jobs(updated_at) WHERE status='QUEUED';
        """)

    def _migration_platform_baseline(self) -> None:
        # Version 1 records the original direct-schema baseline so later changes are
        # independently traceable on both fresh and upgraded installations.
        return None

    def _migration_auth_user_governance(self) -> None:
        user_columns = {
            "version": "INTEGER NOT NULL DEFAULT 1",
            "password_changed_at": "TEXT",
            "password_expires_at": "TEXT",
            "must_change_password": "INTEGER NOT NULL DEFAULT 0",
            "failed_login_count": "INTEGER NOT NULL DEFAULT 0",
            "last_failed_login_at": "TEXT",
            "locked_until": "TEXT",
            "deleted_at": "TEXT",
            "deleted_by": "TEXT",
            "deletion_reason": "TEXT",
        }
        for column, declaration in user_columns.items():
            self._ensure_column("users", column, declaration)
        self._ensure_column("auth_sessions", "ip_address", "TEXT")
        self._ensure_column("auth_sessions", "user_agent", "TEXT")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS auth_audit_events (
              id TEXT PRIMARY KEY,
              actor_user_id TEXT,
              target_user_id TEXT,
              tenant_id TEXT,
              project_id TEXT,
              action TEXT NOT NULL,
              outcome TEXT NOT NULL,
              ip_address TEXT,
              user_agent TEXT,
              details_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_auth_audit_events_scope
              ON auth_audit_events(tenant_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_auth_audit_events_target
              ON auth_audit_events(target_user_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS auth_login_limits (
              key_hash TEXT PRIMARY KEY,
              attempts INTEGER NOT NULL,
              window_started_at TEXT NOT NULL,
              blocked_until TEXT,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_users_lock_state
              ON users(status, locked_until, username);
            """
        )

    def _migration_durable_task_queue(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS task_queue (
              id TEXT PRIMARY KEY,
              queue_name TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              dedupe_key TEXT NOT NULL,
              active_key TEXT UNIQUE,
              status TEXT NOT NULL,
              priority INTEGER NOT NULL DEFAULT 0,
              attempts INTEGER NOT NULL DEFAULT 0,
              max_attempts INTEGER NOT NULL DEFAULT 3,
              available_at TEXT NOT NULL,
              lease_owner TEXT,
              lease_expires_at TEXT,
              last_error TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_task_queue_claim
              ON task_queue(queue_name, status, available_at, priority DESC, created_at);
            """
        )

    def _migration_trace_origins(self) -> None:
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS run_trace_origins (
                entity_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
                trace_id TEXT NOT NULL, parent_span_id TEXT NOT NULL,
                sampled INTEGER NOT NULL CHECK(sampled IN (0,1)),
                request_id TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ingestion_trace_origins (
                entity_id TEXT PRIMARY KEY REFERENCES knowledge_ingestion_jobs(id) ON DELETE CASCADE,
                trace_id TEXT NOT NULL, parent_span_id TEXT NOT NULL,
                sampled INTEGER NOT NULL CHECK(sampled IN (0,1)),
                request_id TEXT NOT NULL, created_at TEXT NOT NULL
            );
        """)

    def _migration_cancellation_finalization(self) -> None:
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS run_cancellations (
                run_id TEXT PRIMARY KEY REFERENCES runs(id),
                attempt_id TEXT NOT NULL REFERENCES run_attempts(id),
                workspace_id TEXT REFERENCES coding_workspaces(id),
                sandbox_instance_id TEXT REFERENCES sandbox_instances(id),
                workspace_generation INTEGER,
                status TEXT NOT NULL CHECK(status IN ('PENDING','RUNNING','COMPLETED')),
                worker_id TEXT, lease_token TEXT, expires_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL,
                last_error TEXT, workspace_snapshot_id TEXT REFERENCES workspace_snapshots(id),
                recovery_point_id TEXT REFERENCES coding_recovery_points(id),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_run_cancellations_pending
                ON run_cancellations(status, available_at) WHERE status!='COMPLETED';
        """)
        self.connection.execute(
            ("CREATE OR REPLACE VIEW" if self.dialect == "postgresql" else "CREATE VIEW IF NOT EXISTS")
            + """ sandbox_cancellation_leases AS
                SELECT c.sandbox_instance_id AS sandbox_request_id, c.workspace_id,
                       c.run_id, r.status AS run_status, c.attempt_id,
                       c.lease_token, c.expires_at, c.status AS finalization_status,
                       c.workspace_generation
                FROM run_cancellations c JOIN runs r ON r.id=c.run_id
                JOIN coding_workspaces w ON w.id=c.workspace_id
                JOIN run_attempts a ON a.id=c.attempt_id
                WHERE r.current_attempt_id=c.attempt_id AND r.status='CANCELLING'
                  AND r.coding_workspace_id=w.id AND a.lease_token IS NULL
                  AND w.sandbox_instance_id=c.sandbox_instance_id
                  AND w.workspace_generation=c.workspace_generation"""
        )

    def _migration_worker_heartbeats(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS worker_nodes (
              id TEXT PRIMARY KEY,
              worker_type TEXT NOT NULL,
              status TEXT NOT NULL,
              started_at TEXT NOT NULL,
              heartbeat_at TEXT NOT NULL,
              stopped_at TEXT,
              metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_worker_nodes_health
              ON worker_nodes(status, heartbeat_at DESC, worker_type);
            """
        )

    def _migration_sandbox_lease_authority(self) -> None:
        self.connection.execute(
            ("CREATE OR REPLACE VIEW" if self.dialect == "postgresql" else "CREATE VIEW IF NOT EXISTS")
            + """ sandbox_execution_leases AS
               SELECT w.sandbox_instance_id AS sandbox_request_id, w.id AS workspace_id,
                      r.id AS run_id, r.status AS run_status,
                      a.id AS attempt_id, a.lease_token, a.expires_at
               FROM coding_workspaces w JOIN runs r ON r.coding_workspace_id=w.id
               JOIN run_attempts a ON a.id=r.current_attempt_id
               WHERE r.status IN ('CREATED','QUEUED','ORPHANED','PREPARING','RUNNING',
                                  'RESUMING','CANCELLING','WAITING_FOR_APPROVAL','WAITING_FOR_INPUT')"""
        )

    def _migration_model_budget(self) -> None:
        columns = {
            "metering_version": "INTEGER NOT NULL DEFAULT 0",
            "attempt_id": "TEXT",
            "billing_status": "TEXT NOT NULL DEFAULT 'ACTUAL'",
            "reserved_micro_usd": "BIGINT NOT NULL DEFAULT 0",
            "charged_micro_usd": "BIGINT NOT NULL DEFAULT 0",
            "pricing_json": "TEXT NOT NULL DEFAULT '{}'",
            "model_identity_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for column, declaration in columns.items():
            self._ensure_column("usage_ledger", column, declaration)
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_ledger_budget ON usage_ledger(run_id, billing_status)"
        )

    def _migration_evaluation_gates(self) -> None:
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS evaluation_suites (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                name TEXT NOT NULL, cases_json TEXT NOT NULL, suite_hash TEXT NOT NULL,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_evaluation_suites_scope
                ON evaluation_suites(tenant_id, project_id, created_at);
            CREATE TABLE IF NOT EXISTS evaluation_policies (
                tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                suite_id TEXT NOT NULL REFERENCES evaluation_suites(id),
                version INTEGER NOT NULL, max_age_seconds INTEGER NOT NULL,
                updated_by TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(tenant_id, project_id)
            );
            CREATE TABLE IF NOT EXISTS evaluation_results (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                agent_revision_id TEXT NOT NULL REFERENCES agent_revisions(id),
                sequence INTEGER NOT NULL, plan_hash TEXT NOT NULL,
                suite_id TEXT NOT NULL REFERENCES evaluation_suites(id), suite_hash TEXT NOT NULL,
                status TEXT NOT NULL, score DOUBLE PRECISION NOT NULL, production_eligible INTEGER NOT NULL,
                checks_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
                result_hash TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(agent_revision_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS idx_evaluation_results_gate
                ON evaluation_results(tenant_id, project_id, agent_revision_id, suite_id, sequence);
            CREATE TABLE IF NOT EXISTS governance_audit_events (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                actor_user_id TEXT NOT NULL, action TEXT NOT NULL, resource_id TEXT NOT NULL,
                details_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_governance_audit_scope
                ON governance_audit_events(tenant_id, project_id, created_at);
        """)
        self._ensure_column("agent_deployments", "evaluation_id", "TEXT REFERENCES evaluation_results(id)")

    def _migration_coding_recovery(self) -> None:
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS coding_graph_sessions (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                thread_id TEXT NOT NULL REFERENCES threads(id),
                run_id TEXT NOT NULL REFERENCES runs(id),
                attempt_id TEXT NOT NULL UNIQUE REFERENCES run_attempts(id),
                workspace_id TEXT NOT NULL REFERENCES coding_workspaces(id),
                graph_thread_id TEXT NOT NULL UNIQUE, plan_hash TEXT NOT NULL,
                source_point_id TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS coding_recovery_points (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES runs(id),
                session_id TEXT NOT NULL REFERENCES coding_graph_sessions(id),
                workspace_id TEXT NOT NULL REFERENCES coding_workspaces(id),
                sequence INTEGER NOT NULL, plan_hash TEXT NOT NULL,
                workspace_generation INTEGER NOT NULL, base_commit_sha TEXT NOT NULL,
                workspace_snapshot_id TEXT NOT NULL REFERENCES workspace_snapshots(id),
                checkpoint_id TEXT NOT NULL, phase TEXT NOT NULL,
                graph_state TEXT NOT NULL, graph_sha256 TEXT NOT NULL,
                manifest_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(workspace_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS idx_coding_recovery_run
                ON coding_recovery_points(run_id, sequence DESC);
        """)

    def _migration_billing(self) -> None:
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS billing_tenants (tenant_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS billing_quota_policies (
                tenant_id TEXT NOT NULL, scope_type TEXT NOT NULL, subject_id TEXT NOT NULL,
                period TEXT NOT NULL, version INTEGER NOT NULL, enabled INTEGER NOT NULL,
                limits_json TEXT NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(tenant_id, scope_type, subject_id, period)
            );
            CREATE TABLE IF NOT EXISTS billing_price_policies (
                tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, model_key TEXT NOT NULL,
                version INTEGER NOT NULL, enabled INTEGER NOT NULL,
                model_identity_json TEXT NOT NULL, pricing_json TEXT NOT NULL,
                updated_by TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(tenant_id, project_id, model_key)
            );
            CREATE TABLE IF NOT EXISTS metered_calls (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                user_id TEXT NOT NULL, run_id TEXT REFERENCES runs(id),
                purpose TEXT NOT NULL, resource_id TEXT NOT NULL, model_key TEXT NOT NULL,
                model_identity_json TEXT NOT NULL, pricing_json TEXT NOT NULL,
                billing_status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, owner_token_hash TEXT NOT NULL,
                reserved_input_tokens BIGINT NOT NULL, reserved_output_tokens BIGINT NOT NULL,
                reserved_micro_usd BIGINT NOT NULL, charged_input_tokens BIGINT NOT NULL,
                charged_output_tokens BIGINT NOT NULL, charged_micro_usd BIGINT NOT NULL,
                input_tokens BIGINT NOT NULL DEFAULT 0, output_tokens BIGINT NOT NULL DEFAULT 0,
                call_count INTEGER NOT NULL DEFAULT 1, request_fingerprint TEXT NOT NULL,
                admitted_at TEXT NOT NULL, day_key TEXT NOT NULL, month_key TEXT NOT NULL,
                active_until TEXT, settled_at TEXT, provider_receipt TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_metered_scope_month ON metered_calls(tenant_id, month_key, project_id, user_id, model_key);
            CREATE INDEX IF NOT EXISTS idx_metered_scope_day ON metered_calls(tenant_id, day_key);
            CREATE INDEX IF NOT EXISTS idx_metered_run ON metered_calls(run_id);
        """)
        self._ensure_column("knowledge_ingestion_jobs", "requested_by", "TEXT")
        self.connection.execute("""UPDATE knowledge_ingestion_jobs SET requested_by=(
            SELECT d.created_by FROM knowledge_document_versions v
            JOIN knowledge_documents d ON d.id=v.document_id
            WHERE v.id=knowledge_ingestion_jobs.document_version_id) WHERE requested_by IS NULL""")
        # Existing spend continues to count. Never reset a tenant's allowance by
        # introducing the unified ledger. Missing attribution remains explicit.
        from decimal import Decimal, ROUND_CEILING
        from packages.billing.models import model_key

        rows = self.connection.execute("""SELECT u.*, r.principal_user_id FROM usage_ledger u
            JOIN runs r ON r.id=u.run_id WHERE u.model_calls>0 OR u.cost>0""").fetchall()
        for source in rows:
            row = dict(source)
            identity = json.loads(row.get("model_identity_json") or "{}")
            at = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
            amount = int(row["charged_micro_usd"]) if row["metering_version"] else int(
                (Decimal(str(row["cost"])) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
            self.connection.execute("""INSERT INTO metered_calls
                (id, tenant_id, project_id, user_id, run_id, purpose, resource_id, model_key,
                 model_identity_json, pricing_json, billing_status, owner_kind, owner_id, owner_token_hash,
                 reserved_input_tokens, reserved_output_tokens, reserved_micro_usd,
                 charged_input_tokens, charged_output_tokens, charged_micro_usd, input_tokens, output_tokens,
                 call_count, request_fingerprint, admitted_at, day_key, month_key)
                VALUES (?,?,?,?,?,'legacy_run',?,?,?,?,'LEGACY','legacy',?,'',?,?,?,?,?,?,?,?,?,'',?,?,?)""",
                (row["id"], row["tenant_id"], row["project_id"], row.get("principal_user_id") or "legacy_unattributed",
                 row["run_id"], row["run_id"], model_key(identity), self.encode(identity), row.get("pricing_json") or "{}",
                 row.get("attempt_id") or row["run_id"], row["input_tokens"], row["output_tokens"], amount,
                 row["input_tokens"], row["output_tokens"], amount, row["input_tokens"], row["output_tokens"],
                 row["model_calls"], at.isoformat(), at.strftime("%Y-%m-%d"), at.strftime("%Y-%m")))

    def _migration_metering_attribution(self) -> None:
        self._ensure_column("usage_ledger", "purpose", "TEXT NOT NULL DEFAULT 'run_model'")
        self._ensure_column("knowledge_ingestion_jobs", "requested_environment_id", "TEXT")
        self._ensure_column("knowledge_ingestion_jobs", "requested_roles_json", "TEXT")
        # Restore only already verified historical measurements. Unknown legacy
        # spend is retained and must be explicitly reconciled, never zeroed.
        self.connection.execute("""UPDATE metered_calls SET billing_status='ACTUAL',version=version+1
            WHERE owner_kind='legacy' AND billing_status='LEGACY' AND id IN (
                SELECT id FROM usage_ledger WHERE metering_version>0 AND billing_status='ACTUAL')""")

    def _migration_knowledge_metadata_access(self) -> None:
        self._ensure_column("knowledge_events", "actor_user_id", "TEXT")

    def _migration_production_releases(self) -> None:
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS release_projects (
                tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                PRIMARY KEY(tenant_id,project_id)
            );
            CREATE TABLE IF NOT EXISTS deployment_environment_grants (
                tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, environment TEXT NOT NULL,
                user_id TEXT NOT NULL, can_deploy INTEGER NOT NULL, can_approve INTEGER NOT NULL,
                version INTEGER NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(tenant_id,project_id,environment,user_id)
            );
            CREATE TABLE IF NOT EXISTS release_channels (
                tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                environment TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
                active_deployment_id TEXT, updated_at TEXT NOT NULL,
                PRIMARY KEY(tenant_id,project_id,agent_id,environment)
            );
            CREATE TABLE IF NOT EXISTS release_requests (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                agent_id TEXT NOT NULL, environment TEXT NOT NULL, requested_by TEXT NOT NULL,
                snapshot_json TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
                status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                deployment_id TEXT, decided_by TEXT, decision_reason TEXT, decided_at TEXT,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_release_requests_scope
                ON release_requests(tenant_id,project_id,created_at,id);
        """)
        self._ensure_column("agent_deployments", "release_request_id", "TEXT")

    def _migration_model_bindings(self) -> None:
        self._ensure_column("model_deployments","runtime_binding_json","TEXT")
        self._ensure_column("model_deployments","version","INTEGER NOT NULL DEFAULT 1")

    def _migration_thread_access(self) -> None:
        self._ensure_column("threads", "owner_user_id", "TEXT")
        self._ensure_column("threads", "visibility", "TEXT NOT NULL DEFAULT 'private'")
        self._ensure_column("threads", "access_version", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("threads", "access_state", "TEXT NOT NULL DEFAULT 'ACTIVE'")
        self._ensure_column("threads", "legacy_access", "INTEGER NOT NULL DEFAULT 1")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS thread_members (
              thread_id TEXT NOT NULL REFERENCES threads(id), user_id TEXT NOT NULL,
              access TEXT NOT NULL, PRIMARY KEY(thread_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS thread_knowledge_sources (
              thread_id TEXT NOT NULL REFERENCES threads(id), document_id TEXT NOT NULL,
              document_version_id TEXT NOT NULL, policy_hash TEXT NOT NULL,
              policy_json TEXT NOT NULL, acquired_by TEXT NOT NULL, created_at TEXT NOT NULL,
              PRIMARY KEY(thread_id,document_version_id,policy_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_thread_access_owner ON threads(tenant_id,project_id,owner_user_id);
            CREATE INDEX IF NOT EXISTS idx_thread_member_user ON thread_members(user_id,thread_id);
        """)
        # Never infer that old project-wide visibility was explicit consent.
        # Threads without a verified creator remain quarantined, not assigned
        # to an administrator. Retain source restrictions on historical answers.
        self.connection.execute("""UPDATE threads SET owner_user_id=(
            SELECT principal_user_id FROM runs WHERE runs.thread_id=threads.id
              AND principal_verified=1 AND principal_user_id IS NOT NULL
            ORDER BY created_at,id LIMIT 1) WHERE owner_user_id IS NULL""")
        self.connection.execute("UPDATE threads SET access_state='QUARANTINED' WHERE owner_user_id IS NULL")
        from packages.auth.resource_access import document_policy, policy_digest

        audits = self.connection.execute("""SELECT k.*,r.thread_id FROM knowledge_retrieval_audits k
            JOIN runs r ON r.id=k.run_id""").fetchall()
        for source in audits:
            audit = dict(source)
            for hit in json.loads(audit["hits_json"]):
                source_row = self.connection.execute("""SELECT d.*,c.document_version_id FROM knowledge_chunks c
                    JOIN knowledge_documents d ON d.id=c.document_id WHERE c.id=?""", (hit["chunk_id"],)).fetchone()
                if not source_row:
                    self.connection.execute("UPDATE threads SET access_state='QUARANTINED' WHERE id=?", (audit["thread_id"],))
                    continue
                document = dict(source_row)
                document["allowed_roles"] = json.loads(document["allowed_roles_json"])
                policy = document_policy(document)
                self.connection.execute("""INSERT INTO thread_knowledge_sources
                    (thread_id,document_id,document_version_id,policy_hash,policy_json,acquired_by,created_at)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""", (
                    audit["thread_id"], document["id"], document["document_version_id"], policy_digest(policy),
                    self.encode(policy), audit["user_id"], audit["created_at"],
                ))

    def _migration_atomic_review(self) -> None:
        self._ensure_column("intent_routing_decisions", "owner_user_id", "TEXT")
        self.connection.execute("""UPDATE intent_routing_decisions SET owner_user_id=(
            SELECT owner_user_id FROM threads WHERE threads.id=intent_routing_decisions.thread_id)
            WHERE owner_user_id IS NULL""")
        self._ensure_column("change_sets", "version", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("change_sets", "decision_hash", "TEXT")

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
