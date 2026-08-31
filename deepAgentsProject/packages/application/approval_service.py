from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from packages.application.services import ConflictError, NotFoundError, new_id
from packages.domain.models import DecisionCreate, TenantContext, utc_now
from packages.persistence import Database
from packages.auth.permissions import Permission, authorize
from packages.auth.resource_access import ResourceAccess, refresh_context
from packages.runtime.event_emitter import EventEmitter
from packages.runtime.admission import TaskAdmission


class ApprovalService:
    def __init__(self, db: Database, events: EventEmitter, orchestrator: Any):
        self.db = db
        self.events = events
        self.orchestrator = orchestrator
        self.admission = TaskAdmission(db)

    def list_interrupts(
        self, context: TenantContext, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        context = refresh_context(self.db, context)
        authorize(context, Permission.APPROVAL_READ)
        sql = """SELECT i.*, r.input AS run_input, a.name AS agent_name
                 FROM interrupts i JOIN runs r ON r.id=i.run_id
                 JOIN agent_deployments d ON d.id=r.agent_deployment_id
                 JOIN agents a ON a.id=d.agent_id
                 WHERE i.tenant_id=? AND i.project_id=?"""
        params: List[Any] = [context.tenant_id, context.project_id]
        if status:
            sql += " AND i.status=?"
            params.append(status.upper())
        sql += " ORDER BY i.created_at DESC"
        rows = self.db.fetch_all(sql, params)
        visible = []
        for row in rows:
            try:
                ResourceAccess(self.db).require_run(row["run_id"], context)
                visible.append(row)
            except NotFoundError:
                continue
        return visible

    def get_interrupt(self, interrupt_id: str, context: TenantContext) -> Dict[str, Any]:
        interrupt = self.db.fetch_one(
            "SELECT * FROM interrupts WHERE id=? AND tenant_id=? AND project_id=?",
            (interrupt_id, context.tenant_id, context.project_id),
        )
        if not interrupt:
            raise NotFoundError("Interrupt not found")
        ResourceAccess(self.db).require_run(interrupt["run_id"], context)
        return interrupt

    async def decide(
        self,
        interrupt_id: str,
        payload: DecisionCreate,
        context: TenantContext,
        idempotency_key: Optional[str],
        expected_version: Optional[int],
    ) -> Dict[str, Any]:
        context = refresh_context(self.db, context)
        authorize(context, Permission.APPROVAL_DECIDE)
        with self.db.transaction():
            interrupt = self.get_interrupt(interrupt_id, context)
            if any(decision.type in {"approve", "edit"} for decision in payload.decisions):
                ResourceAccess(self.db).require_execution(interrupt["run_id"])
            if self.db.dialect == "postgresql":
                self.db.fetch_one("SELECT id FROM runs WHERE id=? FOR UPDATE", (interrupt["run_id"],))
                self.db.fetch_one("SELECT id FROM interrupts WHERE id=? FOR UPDATE", (interrupt_id,))
            result, should_resume, run_id = self._decide(
                interrupt_id,
                payload,
                context,
                idempotency_key,
                expected_version,
            )
            enqueued = bool(should_resume and self.orchestrator.enqueue_in_transaction(run_id))
        if should_resume and not enqueued:
            await self.orchestrator.enqueue(run_id)
        return result

    def _decide(
        self,
        interrupt_id: str,
        payload: DecisionCreate,
        context: TenantContext,
        idempotency_key: Optional[str],
        expected_version: Optional[int],
    ) -> tuple[Dict[str, Any], bool, str]:
        request_hash = hashlib.sha256(json.dumps({
            "payload": payload.model_dump(mode="json"), "expected_version": expected_version,
            "user_id": context.user_id, "project_id": context.project_id,
            "environment_id": context.environment_id,
        }, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        if idempotency_key:
            previous = self.db.fetch_one(
                """SELECT response_json FROM idempotency_records
                   WHERE tenant_id=? AND scope=? AND key=?""",
                (context.tenant_id, f"interrupt:{interrupt_id}", idempotency_key),
            )
            if previous:
                saved = json.loads(previous["response_json"])
                if saved.get("request_hash") != request_hash or "response" not in saved:
                    raise ConflictError("Approval idempotency key is legacy or was used for different content or principal")
                existing = saved["response"]
                return existing, False, existing["run_id"]

        interrupt = self.get_interrupt(interrupt_id, context)
        run = self.db.fetch_one("SELECT * FROM runs WHERE id=?", (interrupt["run_id"],))
        if not run or run["status"] != "WAITING_FOR_APPROVAL":
            raise ConflictError("Run is no longer waiting for this approval")
        if interrupt["status"] != "PENDING":
            raise ConflictError("Interrupt has already been resolved")
        if expected_version is not None and expected_version != interrupt["version"]:
            raise ConflictError(f"Interrupt version is {interrupt['version']}")
        action_configs = {
            action["action_id"]: action for action in interrupt["actions"]
        }
        allowed_actions = set(action_configs)
        submitted_actions = {decision.action_id for decision in payload.decisions}
        if submitted_actions != allowed_actions or len(payload.decisions) != len(
            allowed_actions
        ):
            raise ConflictError("A decision is required for every interrupted action")
        for decision in payload.decisions:
            if decision.action_id not in allowed_actions:
                raise ConflictError(f"Unknown action {decision.action_id}")
            if decision.type not in action_configs[decision.action_id].get(
                "allowed_decisions", []
            ):
                raise ConflictError(
                    f"Decision {decision.type} is not allowed for action {decision.action_id}"
                )

        by_action = {
            decision.action_id: decision.model_dump() for decision in payload.decisions
        }
        ordered_decisions = [
            by_action[action["action_id"]] for action in interrupt["actions"]
        ]
        decision_data = {"decisions": ordered_decisions}
        now = utc_now()
        self.db.execute(
            """UPDATE interrupts SET status='RESOLVED', decision_json=?, version=version+1,
               updated_at=? WHERE id=?""",
            (self.db.encode(decision_data), now, interrupt_id),
        )
        run = self.db.fetch_one("SELECT * FROM runs WHERE id=?", (interrupt["run_id"],))
        checkpoint = run.get("checkpoint") or {}
        primary = ordered_decisions[0]
        decision_types = {decision["type"] for decision in ordered_decisions}
        decision_type = (
            "reject"
            if "reject" in decision_types
            else "respond"
            if "respond" in decision_types
            else primary["type"]
        )
        checkpoint["stage"] = {
            "reject": "approval_rejected",
            "respond": "waiting_for_input",
        }.get(decision_type, "approval_resolved")
        checkpoint["decision"] = primary
        checkpoint["decisions"] = ordered_decisions
        self.events.append(
            run["id"],
            "interrupt.resolved",
            {
                "interrupt_id": interrupt_id,
                "decision": decision_type,
                "decision_count": len(ordered_decisions),
                "actor": context.user_id,
            },
        )

        should_resume = decision_type in {"approve", "edit"}
        if should_resume:
            self.admission.run(context, ignore_run_id=run["id"], principal_user_id=run["principal_user_id"])
            attempt_count = self.db.fetch_one(
                "SELECT COALESCE(MAX(attempt_number), 0) AS value FROM run_attempts WHERE run_id=?",
                (run["id"],),
            )["value"]
            attempt_id = new_id("att")
            self.db.execute(
                """INSERT INTO run_attempts
                   (id, run_id, attempt_number, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'PENDING', ?, ?)""",
                (attempt_id, run["id"], attempt_count + 1, now, now),
            )
            self.db.execute(
                """UPDATE runs SET status='QUEUED', checkpoint_json=?, current_attempt_id=?,
                   version=version+1, updated_at=? WHERE id=?""",
                (self.db.encode(checkpoint), attempt_id, now, run["id"]),
            )
            self.events.append(
                run["id"],
                "run.queued",
                {"reason": "approval_resolved", "attempt": attempt_count + 1},
            )
        elif decision_type == "reject":
            self.db.execute(
                """UPDATE runs SET status='CANCELLED', checkpoint_json=?,
                   version=version+1, updated_at=? WHERE id=?""",
                (self.db.encode(checkpoint), now, run["id"]),
            )
            self.events.append(
                run["id"],
                "tool.failed",
                {
                    "tool_name": interrupt["actions"][0]["tool_name"],
                    "reason": primary.get("message") or "Rejected by reviewer",
                },
            )
            self.events.append(
                run["id"],
                "graph.cancelled",
                {
                    "graph_id": run["current_attempt_id"],
                    "status": "cancelled",
                    "reason": "approval_rejected",
                    "actor": context.user_id,
                },
                span_id="span_main",
                execution_path=["main"],
            )
            self.events.append(
                run["id"], "run.cancelled", {"reason": "approval_rejected"}
            )
        else:
            self.db.execute(
                """UPDATE runs SET status='WAITING_FOR_INPUT', checkpoint_json=?,
                   version=version+1, updated_at=? WHERE id=?""",
                (self.db.encode(checkpoint), now, run["id"]),
            )
            self.events.append(
                run["id"],
                "graph.paused",
                {
                    "graph_id": run["current_attempt_id"],
                    "status": "waiting_for_input",
                    "checkpoint_id": interrupt["checkpoint_id"],
                    "reason": "reviewer_requested_changes",
                    "actor": context.user_id,
                },
                span_id="span_main",
                execution_path=["main", "write_artifact"],
            )
            self.events.append(
                run["id"],
                "run.waiting_for_input",
                {
                    "reason": "reviewer_requested_changes",
                    "message": primary.get("message"),
                },
            )
        result = self.get_interrupt(interrupt_id, context)
        if idempotency_key:
            self.db.execute(
                """INSERT INTO idempotency_records
                   (tenant_id, scope, key, response_json, created_at) VALUES (?, ?, ?, ?, ?)""",
                (
                    context.tenant_id,
                    f"interrupt:{interrupt_id}",
                    idempotency_key,
                    self.db.encode({"request_hash": request_hash, "response": result}),
                    now,
                ),
            )
        return result, should_resume, run["id"]
