from __future__ import annotations

import secrets
from typing import Any, Dict, List, Optional

from packages.application.services import ConflictError, NotFoundError, new_id
from packages.domain.models import RunCreate, RunStatus, TenantContext, ThreadCreate, utc_now
from packages.persistence import Database
from packages.runtime.event_emitter import EventEmitter


class RunService:
    def __init__(self, db: Database, events: EventEmitter, orchestrator: Any = None):
        self.db = db
        self.events = events
        self.orchestrator = orchestrator

    def attach_orchestrator(self, orchestrator: Any) -> None:
        self.orchestrator = orchestrator

    def create_thread(self, payload: ThreadCreate, context: TenantContext) -> Dict[str, Any]:
        deployment = self.db.fetch_one(
            """SELECT * FROM agent_deployments WHERE id=? AND tenant_id=? AND project_id=?
               AND status='ACTIVE'""",
            (payload.agent_deployment_id, context.tenant_id, context.project_id),
        )
        if not deployment:
            raise NotFoundError("Active deployment not found")
        thread_id = new_id("thr")
        now = utc_now()
        self.db.execute(
            """INSERT INTO threads
               (id, tenant_id, project_id, agent_deployment_id, title, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                thread_id,
                context.tenant_id,
                context.project_id,
                payload.agent_deployment_id,
                payload.title,
                now,
                now,
            ),
        )
        return self.get_thread(thread_id, context)

    def list_threads(self, context: TenantContext) -> List[Dict[str, Any]]:
        threads = self.db.fetch_all(
            """SELECT t.*, d.name AS deployment_name, a.name AS agent_name
               FROM threads t JOIN agent_deployments d ON d.id=t.agent_deployment_id
               JOIN agents a ON a.id=d.agent_id
               WHERE t.tenant_id=? AND t.project_id=? ORDER BY t.updated_at DESC""",
            (context.tenant_id, context.project_id),
        )
        for thread in threads:
            last_run = self.db.fetch_one(
                "SELECT id, status, updated_at FROM runs WHERE thread_id=? ORDER BY created_at DESC LIMIT 1",
                (thread["id"],),
            )
            thread["last_run"] = last_run
        return threads

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
        return thread

    async def create_run(
        self,
        thread_id: str,
        payload: RunCreate,
        context: TenantContext,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        if idempotency_key:
            previous = self.db.fetch_one(
                """SELECT response_json FROM idempotency_records
                   WHERE tenant_id=? AND scope=? AND key=?""",
                (context.tenant_id, f"thread:{thread_id}:run", idempotency_key),
            )
            if previous:
                import json

                return json.loads(previous["response_json"])

        thread = self.get_thread(thread_id, context)
        deployment = self.db.fetch_one(
            "SELECT * FROM agent_deployments WHERE id=? AND status='ACTIVE'",
            (thread["agent_deployment_id"],),
        )
        if not deployment:
            raise ConflictError("Thread deployment is not active")
        run_id = new_id("run")
        attempt_id = new_id("att")
        now = utc_now()
        metadata = dict(payload.metadata)
        metadata["request_id"] = metadata.get("request_id", new_id("req"))
        metadata["trace_id"] = metadata.get("trace_id", new_id("trace"))
        self.db.execute(
            """INSERT INTO runs
               (id, tenant_id, project_id, thread_id, agent_deployment_id, resolved_plan_id,
                status, input, metadata_json, checkpoint_json, current_attempt_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'CREATED', ?, ?, '{}', ?, ?, ?)""",
            (
                run_id,
                context.tenant_id,
                context.project_id,
                thread_id,
                deployment["id"],
                deployment["resolved_plan_id"],
                payload.input,
                self.db.encode(metadata),
                attempt_id,
                now,
                now,
            ),
        )
        self.db.execute(
            """INSERT INTO run_attempts
               (id, run_id, attempt_number, status, created_at, updated_at)
               VALUES (?, ?, 1, 'PENDING', ?, ?)""",
            (attempt_id, run_id, now, now),
        )
        self.events.append(run_id, "run.created", {"input": payload.input, "plan_id": deployment["resolved_plan_id"]})
        self._set_status(run_id, RunStatus.QUEUED.value)
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
                    self.db.encode(result),
                    now,
                ),
            )
        if self.orchestrator:
            await self.orchestrator.enqueue(run_id)
        return result

    def list_runs(self, context: TenantContext, limit: int = 100) -> List[Dict[str, Any]]:
        return self.db.fetch_all(
            """SELECT r.*, t.title AS thread_title, a.name AS agent_name,
                      d.environment AS environment
               FROM runs r JOIN threads t ON t.id=r.thread_id
               JOIN agent_deployments d ON d.id=r.agent_deployment_id
               JOIN agents a ON a.id=d.agent_id
               WHERE r.tenant_id=? AND r.project_id=?
               ORDER BY r.created_at DESC LIMIT ?""",
            (context.tenant_id, context.project_id, limit),
        )

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
        run["usage"] = self.db.fetch_one(
            """SELECT COALESCE(SUM(input_tokens),0) AS input_tokens,
                      COALESCE(SUM(output_tokens),0) AS output_tokens,
                      COALESCE(SUM(model_calls),0) AS model_calls,
                      COALESCE(SUM(tool_calls),0) AS tool_calls,
                      COALESCE(SUM(subagent_calls),0) AS subagent_calls,
                      COALESCE(SUM(cost),0) AS cost
               FROM usage_ledger WHERE run_id=?""",
            (run_id,),
        )
        return run

    async def cancel(self, run_id: str, context: TenantContext) -> Dict[str, Any]:
        run = self.get_run(run_id, context)
        if run["status"] in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"}:
            return run
        self._set_status(run_id, RunStatus.CANCELLING.value)
        self.events.append(run_id, "run.cancelling", {"requested_by": context.user_id})
        self._set_status(run_id, RunStatus.CANCELLED.value)
        self.db.execute(
            "UPDATE run_attempts SET status='CANCELLED', updated_at=? WHERE id=?",
            (utc_now(), run["current_attempt_id"]),
        )
        self.events.append(run_id, "run.cancelled", {})
        return self.get_run(run_id, context)

    async def retry(self, run_id: str, context: TenantContext) -> Dict[str, Any]:
        run = self.get_run(run_id, context)
        if run["status"] not in {"FAILED", "ORPHANED", "TIMED_OUT"}:
            raise ConflictError("Only failed, orphaned, or timed-out runs can be retried")
        row = self.db.fetch_one(
            "SELECT COALESCE(MAX(attempt_number), 0) AS value FROM run_attempts WHERE run_id=?", (run_id,)
        )
        attempt_id = new_id("att")
        now = utc_now()
        self.db.execute(
            """INSERT INTO run_attempts
               (id, run_id, attempt_number, status, created_at, updated_at)
               VALUES (?, ?, ?, 'PENDING', ?, ?)""",
            (attempt_id, run_id, row["value"] + 1, now, now),
        )
        self.db.execute(
            "UPDATE runs SET status='QUEUED', current_attempt_id=?, version=version+1, updated_at=? WHERE id=?",
            (attempt_id, now, run_id),
        )
        self.events.append(run_id, "run.queued", {"reason": "retry", "attempt": row["value"] + 1})
        if self.orchestrator:
            await self.orchestrator.enqueue(run_id)
        return self.get_run(run_id, context)

    def artifacts(self, run_id: str, context: TenantContext) -> List[Dict[str, Any]]:
        self.get_run(run_id, context)
        return self.db.fetch_all("SELECT * FROM artifacts WHERE run_id=? ORDER BY created_at", (run_id,))

    def spans(self, run_id: str, context: TenantContext) -> List[Dict[str, Any]]:
        self.get_run(run_id, context)
        events = self.events.list(run_id)
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

