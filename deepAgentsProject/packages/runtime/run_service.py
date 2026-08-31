from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any, Dict, List, Optional

from packages.application.services import ConflictError, NotFoundError, new_id
from packages.auth.permissions import Permission, authorize
from packages.auth.resource_access import ResourceAccess, refresh_context
from packages.auth.transactions import authorized_write, current_authority
from packages.domain.models import (
    TERMINAL_RUN_STATUSES,
    RunCreate,
    RunStatus,
    TenantContext,
    ThreadCreate,
    ThreadAccessUpdate,
    utc_now,
)
from packages.persistence import Database
from packages.persistence.pagination import authorized_page, PageAccessChanged
from packages.runtime.event_emitter import EventEmitter
from packages.runtime.run_lease import finalize_cancellation
from packages.runtime.admission import TaskAdmission


_UNPREPARED = object()

RESERVED_RUN_METADATA = {
    "tenant_id",
    "project_id",
    "environment_id",
    "user_id",
    "roles",
    "principal",
    "routing_decision_id",
    "resume_input",
}


class RunService:
    def __init__(
        self,
        db: Database,
        events: EventEmitter,
        orchestrator: Any = None,
        coding: Any = None,
    ):
        self.db = db
        self.events = events
        self.orchestrator = orchestrator
        self.coding = coding
        self.access = ResourceAccess(db)
        self.admission = TaskAdmission(db)

    def attach_orchestrator(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    def create_thread(
        self,
        payload: ThreadCreate,
        context: TenantContext,
        *,
        routing_decision_id: Optional[str] = None,
        prepared_workspace: Any = _UNPREPARED,
    ) -> Dict[str, Any]:
        context = current_authority(self.db, context, Permission.RUNTIME_USE)
        if prepared_workspace is _UNPREPARED:
            prepared_workspace = self.prepare_thread_workspace(payload, context)
        with self.db.transaction():
            if context.environment_id == 'env_production':
                from packages.releases.service import ReleaseService
                ReleaseService(self.db).lock_project(context)
            with authorized_write(self.db, context, Permission.RUNTIME_USE) as context:
                return self._commit_thread(payload, context, routing_decision_id=routing_decision_id,
                                           prepared_workspace=prepared_workspace)

    def prepare_thread_workspace(self, payload, context):
        context = current_authority(self.db, context, Permission.RUNTIME_USE)
        deployment = self.access.require_deployment(payload.agent_deployment_id, context, active=True)
        row = self.db.fetch_one('SELECT * FROM resolved_execution_plans WHERE id=?', (deployment['resolved_plan_id'],))
        enabled = bool(row and (row['plan'].get('coding_profile') or {}).get('enabled'))
        if enabled != (payload.workspace is not None):
            raise ConflictError('Coding deployments require a workspace; other deployments cannot bind one')
        if payload.workspace is None:
            return None
        if not self.coding:
            raise ConflictError('Coding workspace service is unavailable')
        return self.coding.prepare_workspace(payload.workspace, context, deployment_id=deployment['id'], plan_id=row['id'])

    def _commit_thread(self, payload, context, *, routing_decision_id, prepared_workspace):
        context = refresh_context(self.db, context)
        authorize(context, Permission.RUNTIME_USE)
        deployment = self.access.require_deployment(payload.agent_deployment_id, context, active=True)
        plan_row = self.db.fetch_one(
            "SELECT * FROM resolved_execution_plans WHERE id=?",
            (deployment["resolved_plan_id"],),
        )
        coding_enabled = bool(
            plan_row and (plan_row.get("plan", {}).get("coding_profile") or {}).get("enabled")
        )
        if coding_enabled and payload.workspace is None:
            raise ConflictError("Coding Agent threads require an explicit repository workspace")
        if not coding_enabled and payload.workspace is not None:
            raise ConflictError("Workspace binding is supported only by Coding Agent deployments")
        thread_id = new_id("thr")
        now = utc_now()
        with self.db.transaction():
            self.db.execute(
                """INSERT INTO threads
                   (id, tenant_id, project_id, agent_deployment_id, routing_decision_id,
                    title, created_at, updated_at, owner_user_id, legacy_access)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    thread_id,
                    context.tenant_id,
                    context.project_id,
                    payload.agent_deployment_id,
                    routing_decision_id,
                    payload.title,
                    now,
                    now,
                    context.user_id,
                ),
            )
            if payload.workspace is not None:
                if not self.coding:
                    raise ConflictError("Coding workspace service is unavailable")
                sandbox_profile = plan_row["plan"]["coding_profile"]["sandbox"]
                self.coding.bind_thread(
                    thread_id,
                    payload.workspace,
                    context,
                    prepared=prepared_workspace,
                    lifecycle=sandbox_profile.get("lifecycle", "thread_scoped"),
                    ttl_seconds=int(sandbox_profile.get("ttl_seconds", 86400)),
                )
            self.db.execute("""INSERT INTO governance_audit_events
                (id,tenant_id,project_id,actor_user_id,action,resource_id,details_json,created_at)
                VALUES (?,?,?,?,'thread.created',?,?,?)""", (new_id('audit'), context.tenant_id, context.project_id,
                context.user_id, thread_id, self.db.encode({'deployment_id': deployment['id']}), utc_now()))
            return self.get_thread(thread_id, context)

    def list_threads(self, context: TenantContext) -> List[Dict[str, Any]]:
        return self.thread_page(context)["items"]

    def thread_page(self, context: TenantContext, limit=100, cursor=None, query=""):
        context = refresh_context(self.db, context)
        authorize(context, Permission.RUNTIME_READ)
        scope, params = self.access.thread_scope(context)
        if query:
            scope += " AND (LOWER(t.title) LIKE ? OR LOWER(a.name) LIKE ?)"
            params += (f"%{query.lower()}%", f"%{query.lower()}%")
        page = authorized_page(self.db, query=f"""SELECT t.*, d.name AS deployment_name, a.name AS agent_name
               FROM threads t JOIN agent_deployments d ON d.id=t.agent_deployment_id
               JOIN agents a ON a.id=d.agent_id
               WHERE {scope}""", params=params, alias="t", resource=f"threads:{query}", context=context,
               visible=lambda row: self.access.can_thread(row["id"], context), limit=limit, cursor=cursor)
        for thread in page["items"]:
            last_run = self.db.fetch_one(
                "SELECT id, status, updated_at FROM runs WHERE thread_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                (thread["id"],),
            )
            thread["last_run"] = last_run
        if any(not self.access.can_thread(row["id"], context) for row in page["items"]):
            raise PageAccessChanged("Access changed while loading threads; reload the page")
        return page

    def get_thread(self, thread_id: str, context: TenantContext) -> Dict[str, Any]:
        thread = self.db.fetch_one(
            "SELECT * FROM threads WHERE id=? AND tenant_id=? AND project_id=?",
            (thread_id, context.tenant_id, context.project_id),
        )
        if not thread:
            raise NotFoundError("Thread not found")
        thread["runs"] = self.db.fetch_all(
            "SELECT * FROM runs WHERE thread_id=? ORDER BY created_at DESC", (thread_id,)
        )
        thread["workspace"] = self.db.fetch_one(
            "SELECT * FROM coding_workspaces WHERE thread_id=?", (thread_id,)
        )
        self.access.require_thread(thread_id, context)
        return thread

    def thread_access(self, thread_id: str, context: TenantContext) -> Dict[str, Any]:
        thread = self.get_thread(thread_id, context)
        return {"thread_id": thread_id, "owner_user_id": thread["owner_user_id"],
                "visibility": thread["visibility"], "version": thread["access_version"],
                "legacy_access": bool(thread["legacy_access"]),
                "can_manage": thread["owner_user_id"] == context.user_id,
                "members": self.db.fetch_all("SELECT user_id,access FROM thread_members WHERE thread_id=? ORDER BY user_id", (thread_id,))
                if thread["owner_user_id"] == context.user_id else [],
                "source_restricted": bool(self.db.fetch_one("SELECT 1 AS value FROM thread_knowledge_sources WHERE thread_id=? LIMIT 1", (thread_id,)))}

    def update_thread_access(self, thread_id: str, payload: ThreadAccessUpdate, context: TenantContext):
        context = refresh_context(self.db, context)
        authorize(context, Permission.RUNTIME_USE)
        with self.db.transaction():
            thread = self.get_thread(thread_id, context)
            if thread["owner_user_id"] != context.user_id:
                from packages.auth.service import AuthAuthorizationError
                raise AuthAuthorizationError("Only the thread creator may change sharing")
            if thread["legacy_access"] and payload.visibility != "private":
                raise ConflictError("Legacy conversation consent is unknown; start a new conversation to share")
            if payload.visibility != "members" and payload.members:
                raise ConflictError("Members are only valid for members-only sharing")
            if len({member.user_id for member in payload.members}) != len(payload.members):
                raise ConflictError("Duplicate thread member")
            for member in payload.members:
                account = self.db.fetch_one("""SELECT id FROM users WHERE id=? AND tenant_id=?
                    AND project_id=? AND environment_id=? AND status='ACTIVE'""",
                    (member.user_id, context.tenant_id, context.project_id, context.environment_id))
                if not account:
                    raise NotFoundError("An active project member was not found")
            changed = self.db.execute_count("""UPDATE threads SET visibility=?,access_version=access_version+1
                WHERE id=? AND access_version=?""", (payload.visibility, thread_id, payload.version))
            if changed != 1:
                raise ConflictError("Thread sharing changed; reload before updating")
            self.db.execute("DELETE FROM thread_members WHERE thread_id=?", (thread_id,))
            for member in payload.members:
                self.db.execute("INSERT INTO thread_members(thread_id,user_id,access) VALUES(?,?,?)", (thread_id, member.user_id, member.access))
            self.db.execute("""INSERT INTO governance_audit_events
                (id,tenant_id,project_id,actor_user_id,action,resource_id,details_json,created_at)
                VALUES(?,?,?,?,?,?,?,?)""", (new_id("audit"), context.tenant_id, context.project_id, context.user_id,
                "thread.sharing.updated", thread_id, self.db.encode(payload.model_dump()), utc_now()))
            return self.thread_access(thread_id, context)

    def sharing_candidates(self, thread_id: str, query: str, context: TenantContext):
        access = self.thread_access(thread_id, context)
        if not access["can_manage"]:
            from packages.auth.service import AuthAuthorizationError
            raise AuthAuthorizationError("Only the thread creator can look up sharing recipients")
        return self.db.fetch_all("""SELECT id,username,display_name FROM users
            WHERE tenant_id=? AND project_id=? AND environment_id=? AND status='ACTIVE' AND
            (LOWER(username) LIKE ? OR LOWER(display_name) LIKE ?)
            ORDER BY username LIMIT 20""", (context.tenant_id, context.project_id, context.environment_id,
                                         f"%{query.lower()}%", f"%{query.lower()}%"))

    async def create_run(
        self,
        thread_id: str,
        payload: RunCreate,
        context: TenantContext,
        idempotency_key: Optional[str] = None,
        enqueue: bool = True,
    ) -> Dict[str, Any]:
        context = refresh_context(self.db, context)
        authorize(context, Permission.RUNTIME_USE)
        self._validate_metadata(payload.metadata)
        request_hash = hashlib.sha256(json.dumps({
            "payload": payload.model_dump(mode="json"), "user_id": context.user_id,
            "project_id": context.project_id, "environment_id": context.environment_id,
        }, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        with self.db.transaction():
            if context.environment_id == "env_production":
                from packages.releases.service import ReleaseService
                ReleaseService(self.db).lock_project(context)
            if self.db.dialect == "postgresql":
                self.db.fetch_one(
                    "SELECT id FROM threads WHERE id=? AND tenant_id=? AND project_id=? FOR UPDATE",
                    (thread_id, context.tenant_id, context.project_id),
                )
            self.access.require_thread(thread_id, context, write=True)
            if idempotency_key:
                previous = self.db.fetch_one(
                    """SELECT response_json FROM idempotency_records
                       WHERE tenant_id=? AND scope=? AND key=?""",
                    (context.tenant_id, f"thread:{thread_id}:run", idempotency_key),
                )
                if previous:
                    saved = json.loads(previous["response_json"])
                    if saved.get("request_hash") != request_hash or "response" not in saved:
                        # Legacy responses have no trustworthy original request
                        # fingerprint. Never guess from mutable Run metadata.
                        raise ConflictError("Run idempotency key is legacy or was used for different content or principal")
                    self.access.require_run(saved["response"]["id"], context, write=True)
                    return saved["response"]

            thread = self.get_thread(thread_id, context)
            self._assert_thread_available(thread_id)
            deployment = self.access.require_deployment(thread["agent_deployment_id"], context, active=True)
            registry = getattr(self,"model_registry",None)
            if registry is not None:
                model_plan = self.db.fetch_one("SELECT plan_json FROM resolved_execution_plans WHERE id=?",
                    (deployment["resolved_plan_id"],))
                registry.validate_plan(model_plan["plan"])
            if deployment["environment"] == "production":
                from packages.evaluations.service import EvaluationService
                EvaluationService(self.db).require_production_result(deployment["agent_revision_id"], context)
            self.admission.run(context)
            run_id = new_id("run")
            attempt_id = new_id("att")
            now = utc_now()
            self._validate_metadata(payload.metadata)
            metadata = dict(payload.metadata)
            # Client metadata is not tracing provenance. Trusted origins are
            # stored independently in the same transaction as this Run.
            workspace = self.db.fetch_one(
                "SELECT * FROM coding_workspaces WHERE thread_id=?", (thread_id,)
            )
            self.db.execute(
                """INSERT INTO runs
                   (id, tenant_id, project_id, thread_id, agent_deployment_id, resolved_plan_id,
                    status, input, metadata_json, principal_user_id, principal_roles_json,
                    principal_environment_id, principal_verified, coding_workspace_id,
                    routing_decision_id, workspace_generation, checkpoint_json,
                    current_attempt_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'CREATED', ?, ?, ?, ?, ?, 1, ?, ?, ?, '{}', ?, ?, ?)""",
                (
                    run_id,
                    context.tenant_id,
                    context.project_id,
                    thread_id,
                    deployment["id"],
                    deployment["resolved_plan_id"],
                    payload.input,
                    self.db.encode(metadata),
                    context.user_id,
                    self.db.encode(context.roles),
                    context.environment_id,
                    workspace["id"] if workspace else None,
                    thread.get("routing_decision_id"),
                    workspace["workspace_generation"] if workspace else None,
                    attempt_id,
                    now,
                    now,
                ),
            )
            from packages.operations.telemetry import persist_origin
            persist_origin(self.db, 'run', run_id)
            self.db.execute(
                """INSERT INTO run_attempts
                   (id, run_id, attempt_number, status, created_at, updated_at)
                   VALUES (?, ?, 1, 'PENDING', ?, ?)""",
                (attempt_id, run_id, now, now),
            )
            self.events.append(
                run_id,
                "run.created",
                {"input": payload.input, "plan_id": deployment["resolved_plan_id"]},
            )
            self._set_status(run_id, RunStatus.QUEUED.value)
            self.db.execute("UPDATE threads SET updated_at=? WHERE id=?", (now, thread_id))
            self.events.append(run_id, "run.queued", {"queue": "runtime-worker-standard"})
            result = self.get_run(run_id, context)
            if idempotency_key:
                self.db.execute(
                    """INSERT OR IGNORE INTO idempotency_records
                       (tenant_id, scope, key, response_json, created_at) VALUES (?, ?, ?, ?, ?)""",
                    (
                        context.tenant_id,
                        f"thread:{thread_id}:run",
                        idempotency_key,
                        self.db.encode({"request_hash": request_hash, "response": result}),
                        now,
                    ),
                )
            enqueued = bool(enqueue and self.orchestrator and self.orchestrator.enqueue_in_transaction(run_id))
        if enqueue and not enqueued:
            await self.enqueue_run(run_id)
        return result

    async def enqueue_run(self, run_id: str) -> None:
        if self.orchestrator:
            await self.orchestrator.enqueue(run_id)

    def _locked_run(self, run_id: str, context: TenantContext) -> Dict[str, Any]:
        self.access.require_run(run_id, context, write=True)
        run = self.get_run(run_id, context)
        if self.db.dialect == "postgresql":
            self.db.fetch_one("SELECT id FROM threads WHERE id=? FOR UPDATE", (run["thread_id"],))
            self.db.fetch_one("SELECT id FROM runs WHERE id=? FOR UPDATE", (run_id,))
        return self.get_run(run_id, context)

    def _assert_thread_available(self, thread_id: str, *, ignore_run_id: str = "") -> None:
        active = self.db.fetch_one(
            """SELECT id FROM runs WHERE thread_id=? AND id<>?
               AND status NOT IN ('CANCELLED','TIMED_OUT','FAILED','FAILED_BUDGET','SUCCEEDED')
               LIMIT 1""",
            (thread_id, ignore_run_id),
        )
        if active:
            raise ConflictError("Thread already has an active run; complete or cancel it first")

    async def provide_input(
        self, run_id: str, payload: RunCreate, context: TenantContext
    ) -> Dict[str, Any]:
        authorize(context, Permission.RUNTIME_USE)
        with self.db.transaction():
            run = self._locked_run(run_id, context)
            if run["principal_user_id"] != context.user_id:
                from packages.auth.service import AuthAuthorizationError
                raise AuthAuthorizationError("Only the original Run principal can supply continuation input")
            self.access.require_execution(run_id)
            if run["status"] != RunStatus.WAITING_FOR_INPUT.value:
                raise ConflictError("Only runs waiting for input can be resumed this way")
            self.admission.run(context, ignore_run_id=run_id, principal_user_id=run["principal_user_id"])
            now = utc_now()
            number = self.db.fetch_one(
                "SELECT COALESCE(MAX(attempt_number), 0) AS value FROM run_attempts WHERE run_id=?",
                (run_id,),
            )["value"] + 1
            attempt_id = new_id("att")
            checkpoint = run.get("checkpoint") or {}
            checkpoint["stage"] = "input_received"
            checkpoint.setdefault("responses", []).append(
                {"input": payload.input, "actor": context.user_id, "received_at": now}
            )
            self._validate_metadata(payload.metadata)
            metadata = {**(run.get("metadata") or {}), **payload.metadata, "resume_input": payload.input}
            self.db.execute(
                """INSERT INTO run_attempts
                   (id, run_id, attempt_number, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'PENDING', ?, ?)""",
                (attempt_id, run_id, number, now, now),
            )
            self.db.execute(
                """UPDATE runs SET status='QUEUED', checkpoint_json=?, metadata_json=?,
                   current_attempt_id=?, version=version+1, updated_at=? WHERE id=?""",
                (self.db.encode(checkpoint), self.db.encode(metadata), attempt_id, now, run_id),
            )
            self.db.execute("UPDATE threads SET updated_at=? WHERE id=?", (now, run["thread_id"]))
            self.events.append(run_id, "run.input_received", {"actor": context.user_id, "attempt": number})
            self.events.append(run_id, "run.queued", {"reason": "input_received", "attempt": number})
            enqueued = bool(self.orchestrator and self.orchestrator.enqueue_in_transaction(run_id))
        if not enqueued:
            await self.enqueue_run(run_id)
        return self.get_run(run_id, context)

    def list_runs(self, context: TenantContext, limit: int = 100) -> List[Dict[str, Any]]:
        return self.run_page(context, limit=limit)["items"]

    def run_page(self, context: TenantContext, limit=100, cursor=None, query="", status=None):
        context = refresh_context(self.db, context)
        authorize(context, Permission.RUNTIME_READ)
        scope, params = self.access.thread_scope(context)
        where = scope
        if query:
            where += " AND (LOWER(r.id) LIKE ? OR LOWER(r.thread_id) LIKE ? OR LOWER(a.name) LIKE ? OR LOWER(r.input) LIKE ? OR LOWER(r.status) LIKE ?)"
            params += tuple(f"%{query.lower()}%" for _ in range(5))
        if status:
            where += " AND r.status=?"
            params += (status,)
        page = authorized_page(self.db, query=f"""SELECT r.*, t.title AS thread_title, a.name AS agent_name,
                      d.environment AS environment
               FROM runs r JOIN threads t ON t.id=r.thread_id AND t.tenant_id=r.tenant_id AND t.project_id=r.project_id
               JOIN agent_deployments d ON d.id=r.agent_deployment_id
               JOIN agents a ON a.id=d.agent_id
               WHERE {where}""", params=params, alias="r", resource=f"runs:{query}:{status}", context=context,
               visible=lambda row: self.access.can_thread(row["thread_id"], context), limit=limit, cursor=cursor)
        for run in page["items"]:
            run["attempt_count"] = self.db.fetch_one(
                "SELECT COUNT(*) AS count FROM run_attempts WHERE run_id=?", (run["id"],)
            )["count"]
            run["usage"] = self.db.fetch_one(
                """SELECT COALESCE(SUM(input_tokens),0) AS input_tokens,
                          COALESCE(SUM(output_tokens),0) AS output_tokens,
                          COALESCE(SUM(model_calls),0) AS model_calls,
                          COALESCE(SUM(tool_calls),0) AS tool_calls,
                          COALESCE(SUM(subagent_calls),0) AS subagent_calls,
                          COALESCE(SUM(cost),0) AS cost,
                          COALESCE(SUM(CASE WHEN billing_status!='ACTUAL' THEN model_calls ELSE 0 END),0) AS unsettled_model_calls
                   FROM usage_ledger WHERE run_id=?""",
                (run["id"],),
            )
        if any(not self.access.can_thread(row["thread_id"], context) for row in page["items"]):
            raise PageAccessChanged("Access changed while loading runs; reload the page")
        return page

    def get_run(self, run_id: str, context: TenantContext) -> Dict[str, Any]:
        run = self.db.fetch_one(
            "SELECT * FROM runs WHERE id=? AND tenant_id=? AND project_id=?",
            (run_id, context.tenant_id, context.project_id),
        )
        if not run:
            raise NotFoundError("Run not found")
        run["attempts"] = self.db.fetch_all(
            "SELECT * FROM run_attempts WHERE run_id=? ORDER BY attempt_number", (run_id,)
        )
        for attempt in run["attempts"]:
            attempt.pop("lease_token", None)
        run['cancellation'] = self.db.fetch_one("""SELECT status, attempts, last_error, available_at,
            workspace_snapshot_id, recovery_point_id FROM run_cancellations WHERE run_id=?""", (run_id,))
        run['observability'] = self.db.fetch_one('SELECT trace_id, request_id FROM run_trace_origins WHERE entity_id=?', (run_id,))
        run["usage"] = self.db.fetch_one(
            """SELECT COALESCE(SUM(input_tokens),0) AS input_tokens,
                      COALESCE(SUM(output_tokens),0) AS output_tokens,
                      COALESCE(SUM(model_calls),0) AS model_calls,
                      COALESCE(SUM(tool_calls),0) AS tool_calls,
                      COALESCE(SUM(subagent_calls),0) AS subagent_calls,
                      COALESCE(SUM(cost),0) AS cost,
                      COALESCE(SUM(CASE WHEN billing_status!='ACTUAL' THEN model_calls ELSE 0 END),0) AS unsettled_model_calls
               FROM usage_ledger WHERE run_id=?""",
            (run_id,),
        )
        self.access.require_thread(run["thread_id"], context)
        return run

    async def cancel(self, run_id: str, context: TenantContext) -> Dict[str, Any]:
        authorize(context, Permission.RUNTIME_CONTROL)
        with self.db.transaction():
            run = self._locked_run(run_id, context)
            if run["status"] in TERMINAL_RUN_STATUSES:
                return run
            if run["status"] != RunStatus.CANCELLING.value:
                self._set_status(run_id, RunStatus.CANCELLING.value)
                self.db.execute(
                    "UPDATE run_attempts SET lease_token=NULL, expires_at=NULL, updated_at=? WHERE id=?",
                    (utc_now(), run["current_attempt_id"]),
                )
                self.events.append(run_id, "run.cancelling", {"requested_by": context.user_id})
        if self.orchestrator:
            await self.orchestrator.cancel_execution(run_id)
        finalize_cancellation(self.db, self.events, run_id)
        return self.get_run(run_id, context)

    async def retry(self, run_id: str, context: TenantContext) -> Dict[str, Any]:
        authorize(context, Permission.RUNTIME_CONTROL)
        with self.db.transaction():
            run = self._locked_run(run_id, context)
            if run["status"] not in {"FAILED", "ORPHANED", "TIMED_OUT"}:
                raise ConflictError("Only failed, orphaned, or timed-out runs can be retried")
            self._assert_thread_available(run["thread_id"], ignore_run_id=run_id)
            current = self.db.fetch_one("SELECT status FROM run_attempts WHERE id=?", (run["current_attempt_id"],))
            if current and current["status"] == "RUNNING":
                raise ConflictError("The current attempt is still running")
            self.admission.run(context, ignore_run_id=run_id, principal_user_id=run["principal_user_id"])
            number = self.db.fetch_one(
                "SELECT COALESCE(MAX(attempt_number), 0) AS value FROM run_attempts WHERE run_id=?",
                (run_id,),
            )["value"] + 1
            attempt_id = new_id("att")
            now = utc_now()
            self.db.execute(
                "UPDATE run_attempts SET status='SUPERSEDED', lease_token=NULL WHERE id=? AND status='PENDING'",
                (run["current_attempt_id"],),
            )
            self.db.execute(
                """INSERT INTO run_attempts
                   (id, run_id, attempt_number, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'PENDING', ?, ?)""",
                (attempt_id, run_id, number, now, now),
            )
            self.db.execute(
                "UPDATE runs SET status='QUEUED', current_attempt_id=?, version=version+1, updated_at=? WHERE id=?",
                (attempt_id, now, run_id),
            )
            self.events.append(run_id, "run.queued", {"reason": "retry", "attempt": number})
            enqueued = bool(self.orchestrator and self.orchestrator.enqueue_in_transaction(run_id))
        if not enqueued:
            await self.enqueue_run(run_id)
        return self.get_run(run_id, context)

    def artifacts(self, run_id: str, context: TenantContext) -> List[Dict[str, Any]]:
        items = self.db.fetch_all("SELECT * FROM artifacts WHERE run_id=? ORDER BY created_at", (run_id,))
        self.access.require_run(run_id, context)
        return items

    def visible_events(self, run_id: str, context: TenantContext, after_sequence: int = 0):
        return self.event_page(run_id, context, after_sequence)["items"]

    def event_page(self, run_id: str, context: TenantContext, after_sequence: int = 0):
        # Authorize after loading a batch: provenance is committed before any
        # derived content is emitted, closing the check-then-read streaming race.
        events = self.events.list(run_id, after_sequence)
        self.access.require_run(run_id, context)
        return {"items": [event for event in events if event.get("visibility", "user") == "user"],
                "next_sequence": events[-1]["sequence"] if events else after_sequence,
                "has_more": len(events) == 500}

    def spans(self, run_id: str, context: TenantContext) -> List[Dict[str, Any]]:
        self.get_run(run_id, context)
        events = self.visible_events(run_id, context)
        spans: Dict[str, Dict[str, Any]] = {}
        for event in events:
            if not event.get("span_id"):
                continue
            span = spans.setdefault(
                event["span_id"],
                {
                    "span_id": event["span_id"],
                    "parent_span_id": event.get("parent_span_id"),
                    "execution_path": event.get("execution_path", ["main"]),
                    "events": [],
                },
            )
            span["events"].append(event)
        return list(spans.values())

    def _set_status(self, run_id: str, status: str, output: Optional[str] = None) -> None:
        if output is None:
            self.db.execute(
                "UPDATE runs SET status=?, version=version+1, updated_at=? WHERE id=?",
                (status, utc_now(), run_id),
            )
        else:
            self.db.execute(
                "UPDATE runs SET status=?, output=?, version=version+1, updated_at=? WHERE id=?",
                (status, output, utc_now(), run_id),
            )

    @staticmethod
    def _validate_metadata(metadata: Dict[str, Any]) -> None:
        reserved = sorted(RESERVED_RUN_METADATA.intersection(metadata))
        if reserved:
            raise ConflictError(
                "Run metadata contains reserved identity fields: " + ", ".join(reserved)
            )
