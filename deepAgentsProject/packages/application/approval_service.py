from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from packages.application.services import ConflictError, NotFoundError, new_id
from packages.domain.models import DecisionCreate, TenantContext, utc_now
from packages.persistence import Database
from packages.runtime.event_emitter import EventEmitter


class ApprovalService:
    def __init__(self, db: Database, events: EventEmitter, orchestrator: Any):
        self.db = db
        self.events = events
        self.orchestrator = orchestrator

    def list_interrupts(
        self, context: TenantContext, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
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
        return self.db.fetch_all(sql, params)

    def get_interrupt(self, interrupt_id: str, context: TenantContext) -> Dict[str, Any]:
        interrupt = self.db.fetch_one(
            "SELECT * FROM interrupts WHERE id=? AND tenant_id=? AND project_id=?",
            (interrupt_id, context.tenant_id, context.project_id),
        )
        if not interrupt:
            raise NotFoundError("Interrupt not found")
        return interrupt

    async def decide(
        self,
        interrupt_id: str,
        payload: DecisionCreate,
        context: TenantContext,
        idempotency_key: Optional[str],
        expected_version: Optional[int],
    ) -> Dict[str, Any]:
        if idempotency_key:
            previous = self.db.fetch_one(
                """SELECT response_json FROM idempotency_records
                   WHERE tenant_id=? AND scope=? AND key=?""",
                (context.tenant_id, f"interrupt:{interrupt_id}", idempotency_key),
            )
            if previous:
                return json.loads(previous["response_json"])

        interrupt = self.get_interrupt(interrupt_id, context)
        if interrupt["status"] != "PENDING":
            raise ConflictError("Interrupt has already been resolved")
        if expected_version is not None and expected_version != interrupt["version"]:
            raise ConflictError(f"Interrupt version is {interrupt['version']}")
        allowed_actions = {action["action_id"] for action in interrupt["actions"]}
        for decision in payload.decisions:
            if decision.action_id not in allowed_actions:
                raise ConflictError(f"Unknown action {decision.action_id}")

        decision_data = payload.model_dump()
        now = utc_now()
        self.db.execute(
            """UPDATE interrupts SET status='RESOLVED', decision_json=?, version=version+1,
               updated_at=? WHERE id=?""",
            (self.db.encode(decision_data), now, interrupt_id),
        )
        run = self.db.fetch_one("SELECT * FROM runs WHERE id=?", (interrupt["run_id"],))
        checkpoint = run.get("checkpoint") or {}
        primary = payload.decisions[0].model_dump()
        decision_type = primary["type"]
        checkpoint["stage"] = {
            "reject": "approval_rejected",
            "respond": "waiting_for_input",
        }.get(decision_type, "approval_resolved")
        checkpoint["decision"] = primary
        self.events.append(
            run["id"],
            "interrupt.resolved",
            {
                "interrupt_id": interrupt_id,
                "decision": primary["type"],
                "actor": context.user_id,
            },
        )

        should_resume = decision_type in {"approve", "edit"}
        if should_resume:
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
                    self.db.encode(result),
                    now,
                ),
            )
        if should_resume:
            await self.orchestrator.enqueue(run["id"])
        return result
