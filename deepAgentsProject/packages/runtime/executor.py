from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from packages.adapters.harness.deepagents import DeepAgentsHarnessAdapter
from packages.application.services import new_id
from packages.domain.models import RunStatus, utc_now
from packages.persistence import Database
from packages.runtime.binder import RuntimeBinder
from packages.runtime.event_emitter import EventEmitter


HIGH_RISK_TERMS = {
    "production",
    "deploy",
    "delete",
    "shell",
    "execute",
    "数据库",
    "生产",
    "部署",
    "删除",
    "写入",
}


class ReferenceRuntimeExecutor:
    """Deterministic runtime that exercises the platform contract without API keys.

    It is a functional Phase 1 harness, not a fake HTTP response: runs are leased,
    checkpointed, evented, paused for HITL, resumed with a new attempt, audited,
    metered, and tied to an immutable plan. Swap only this adapter to invoke the
    Deep Agents/LangGraph SDK in a production worker image.
    """

    def __init__(self, db: Database, events: EventEmitter, worker_id: str):
        self.db = db
        self.events = events
        self.worker_id = worker_id
        self.binder = RuntimeBinder()
        self.harness = DeepAgentsHarnessAdapter()

    async def execute(self, run_id: str) -> None:
        run = self.db.fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run or run["status"] == RunStatus.CANCELLED.value:
            return
        plan_row = self.db.fetch_one(
            "SELECT * FROM resolved_execution_plans WHERE id=?", (run["resolved_plan_id"],)
        )
        if not plan_row:
            raise RuntimeError("Resolved execution plan is missing")
        plan = plan_row["plan"]
        self._acquire_lease(run)
        run = self.db.fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
        runtime_context = self.binder.bind(run, plan)
        factory = await self.harness.build_factory(plan)
        executable = await factory(runtime_context)

        checkpoint = run.get("checkpoint") or {}
        if checkpoint.get("stage") == "approval_resolved":
            await self._resume_after_approval(run, plan, executable, checkpoint)
            return

        self._set_status(run_id, RunStatus.PREPARING.value)
        self.events.append(
            run_id,
            "run.preparing",
            {
                "worker_id": self.worker_id,
                "runtime_image_digest": plan["runtime_image_digest"],
                "plan_hash": plan["plan_hash"],
            },
        )
        await asyncio.sleep(0.08)
        if self._is_cancelled(run_id):
            return

        self._set_status(run_id, RunStatus.RUNNING.value)
        self.events.append(
            run_id,
            "run.started",
            {
                "worker_id": self.worker_id,
                "attempt_id": run["current_attempt_id"],
                "model_endpoint_id": runtime_context["model_endpoint_id"],
                "harness_adapter_version": executable["adapter_version"],
            },
            span_id="span_main",
        )
        self.events.append(
            run_id,
            "todo.updated",
            {
                "items": [
                    {"id": "todo_1", "title": "Understand request and constraints", "status": "completed"},
                    {"id": "todo_2", "title": "Inspect relevant project context", "status": "in_progress"},
                    {"id": "todo_3", "title": "Prepare an auditable recommendation", "status": "pending"},
                ]
            },
            span_id="span_main",
        )
        self.events.append(
            run_id,
            "model.started",
            {"model": plan["model_snapshot"]["model"], "route": plan["model_snapshot"]["endpoint_region"]},
            span_id="span_model_1",
            parent_span_id="span_main",
        )
        await asyncio.sleep(0.08)
        self.events.append(
            run_id,
            "model.delta",
            {"delta": "I’ll inspect the request, gather evidence, and keep risky actions behind policy controls."},
            span_id="span_model_1",
            parent_span_id="span_main",
        )
        await asyncio.sleep(0.05)

        if plan.get("subagent_bindings"):
            subagent = plan["subagent_bindings"][0]
            self.events.append(
                run_id,
                "subagent.started",
                {"agent_name": subagent["name"], "task": "Gather release and risk context"},
                span_id="span_subagent_1",
                parent_span_id="span_main",
                execution_path=["main", subagent["name"]],
            )
            await asyncio.sleep(0.06)
            self.events.append(
                run_id,
                "subagent.progress",
                {"progress": 65, "summary": "Found three relevant constraints"},
                span_id="span_subagent_1",
                parent_span_id="span_main",
                execution_path=["main", subagent["name"]],
            )
            self.events.append(
                run_id,
                "subagent.completed",
                {"result": "Risk context and dependency notes are ready"},
                span_id="span_subagent_1",
                parent_span_id="span_main",
                execution_path=["main", subagent["name"]],
            )

        self.events.append(
            run_id,
            "tool.requested",
            {"tool_name": "knowledge_search", "arguments": {"query": run["input"][:160]}, "risk_level": "low"},
            span_id="span_tool_search",
            parent_span_id="span_main",
        )
        self.events.append(
            run_id,
            "tool.started",
            {"tool_name": "knowledge_search"},
            span_id="span_tool_search",
            parent_span_id="span_main",
        )
        await asyncio.sleep(0.07)
        self.events.append(
            run_id,
            "tool.completed",
            {"tool_name": "knowledge_search", "result_count": 4, "redacted": False},
            span_id="span_tool_search",
            parent_span_id="span_main",
        )

        if self._needs_approval(run["input"], plan):
            await self._pause_for_approval(run, plan)
            return

        await self._complete(run, plan, approved=False)

    def _acquire_lease(self, run: Dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        lease_token = f"lease_{secrets.token_hex(8)}"
        self.db.execute(
            """UPDATE run_attempts SET status='RUNNING', worker_id=?, lease_token=?,
               acquired_at=?, heartbeat_at=?, expires_at=?, updated_at=? WHERE id=?""",
            (
                self.worker_id,
                lease_token,
                now.isoformat(),
                now.isoformat(),
                (now + timedelta(seconds=30)).isoformat(),
                now.isoformat(),
                run["current_attempt_id"],
            ),
        )

    def _needs_approval(self, text: str, plan: Dict[str, Any]) -> bool:
        mode = plan.get("approval_mode", "high_risk")
        if mode == "always":
            return True
        if mode == "never":
            return False
        normalized = text.lower()
        return any(term in normalized for term in HIGH_RISK_TERMS)

    async def _pause_for_approval(self, run: Dict[str, Any], plan: Dict[str, Any]) -> None:
        action_id = new_id("act")
        interrupt_id = new_id("int")
        checkpoint_id = new_id("ckpt")
        arguments = {"path": "/artifacts/release-plan.md", "mode": "write", "environment": "production"}
        actions = [
            {
                "action_id": action_id,
                "tool_name": "artifact_write",
                "arguments": arguments,
                "risk_level": "high",
                "allowed_decisions": ["approve", "edit", "reject", "respond"],
            }
        ]
        now = utc_now()
        self.events.append(
            run["id"],
            "tool.requested",
            {"tool_name": "artifact_write", "arguments": arguments, "risk_level": "high"},
            span_id="span_tool_write",
            parent_span_id="span_main",
        )
        self.db.execute(
            """INSERT INTO interrupts
               (id, tenant_id, project_id, run_id, checkpoint_id, version, policy_reason,
                status, actions_json, expires_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 1, ?, 'PENDING', ?, ?, ?, ?)""",
            (
                interrupt_id,
                run["tenant_id"],
                run["project_id"],
                run["id"],
                checkpoint_id,
                "Production write requires explicit human approval",
                self.db.encode(actions),
                (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
                now,
                now,
            ),
        )
        checkpoint = {
            "checkpoint_id": checkpoint_id,
            "stage": "awaiting_approval",
            "interrupt_id": interrupt_id,
            "action_id": action_id,
            "arguments": arguments,
            "plan_hash": plan["plan_hash"],
        }
        self.db.execute(
            """UPDATE runs SET status='WAITING_FOR_APPROVAL', checkpoint_json=?,
               version=version+1, updated_at=? WHERE id=?""",
            (self.db.encode(checkpoint), now, run["id"]),
        )
        self.db.execute(
            "UPDATE run_attempts SET status='SUCCEEDED', updated_at=? WHERE id=?",
            (now, run["current_attempt_id"]),
        )
        self.events.append(
            run["id"],
            "tool.approval_required",
            {"interrupt_id": interrupt_id, "policy_reason": "Production write requires explicit human approval", "actions": actions},
            span_id="span_tool_write",
            parent_span_id="span_main",
        )
        self.events.append(
            run["id"],
            "interrupt.created",
            {"interrupt_id": interrupt_id, "checkpoint_id": checkpoint_id, "version": 1},
        )

    async def _resume_after_approval(
        self,
        run: Dict[str, Any],
        plan: Dict[str, Any],
        executable: Dict[str, Any],
        checkpoint: Dict[str, Any],
    ) -> None:
        self._set_status(run["id"], RunStatus.RESUMING.value)
        self.events.append(
            run["id"],
            "run.resumed",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "plan_hash": plan["plan_hash"],
                "harness_adapter_version": executable["adapter_version"],
            },
        )
        await asyncio.sleep(0.08)
        self._set_status(run["id"], RunStatus.RUNNING.value)
        decision = checkpoint.get("decision", {})
        decision_type = decision.get("type", "approve")
        if decision_type == "reject":
            self.events.append(
                run["id"],
                "tool.failed",
                {"tool_name": "artifact_write", "reason": decision.get("message") or "Rejected by reviewer"},
                span_id="span_tool_write",
                parent_span_id="span_main",
            )
            await self._complete(run, plan, approved=False, rejected=True)
            return
        arguments = decision.get("edited_arguments") or checkpoint.get("arguments", {})
        self.events.append(
            run["id"],
            "tool.started",
            {"tool_name": "artifact_write", "arguments": arguments, "idempotency_key": checkpoint["checkpoint_id"]},
            span_id="span_tool_write",
            parent_span_id="span_main",
        )
        await asyncio.sleep(0.08)
        self.events.append(
            run["id"],
            "tool.completed",
            {"tool_name": "artifact_write", "path": arguments.get("path", "/artifacts/release-plan.md")},
            span_id="span_tool_write",
            parent_span_id="span_main",
        )
        await self._complete(run, plan, approved=True)

    async def _complete(
        self, run: Dict[str, Any], plan: Dict[str, Any], *, approved: bool, rejected: bool = False
    ) -> None:
        if self._is_cancelled(run["id"]):
            return
        output = (
            "The requested production write was safely declined. I completed the analysis and preserved the evidence without changing external state."
            if rejected
            else "Analysis complete. I verified the execution plan, gathered the relevant context, and produced an auditable release recommendation."
        )
        content = (
            "# Release recommendation\n\n"
            f"Request: {run['input']}\n\n"
            "## Findings\n\n- Dependencies are resolved and version-locked.\n"
            "- High-risk writes are controlled by policy and idempotency keys.\n"
            f"- Human approval: {'granted' if approved else ('rejected' if rejected else 'not required')}.\n"
        )
        artifact_id = new_id("art")
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        now = utc_now()
        self.db.execute(
            """INSERT INTO artifacts
               (id, tenant_id, project_id, run_id, name, media_type, size_bytes,
                content_hash, content, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                artifact_id,
                run["tenant_id"],
                run["project_id"],
                run["id"],
                "release-recommendation.md",
                "text/markdown",
                len(content.encode()),
                content_hash,
                content,
                now,
            ),
        )
        self.events.append(
            run["id"],
            "artifact.created",
            {
                "artifact_id": artifact_id,
                "name": "release-recommendation.md",
                "media_type": "text/markdown",
                "content_hash": content_hash,
            },
        )
        self.events.append(
            run["id"],
            "todo.updated",
            {
                "items": [
                    {"id": "todo_1", "title": "Understand request and constraints", "status": "completed"},
                    {"id": "todo_2", "title": "Inspect relevant project context", "status": "completed"},
                    {"id": "todo_3", "title": "Prepare an auditable recommendation", "status": "completed"},
                ]
            },
            span_id="span_main",
        )
        self.events.append(
            run["id"],
            "model.completed",
            {"output": output, "finish_reason": "stop"},
            span_id="span_model_2",
            parent_span_id="span_main",
        )
        input_tokens = max(24, len(run["input"]) // 3)
        output_tokens = max(48, len(output) // 3)
        subagent_calls = 1 if plan.get("subagent_bindings") else 0
        tool_calls = 2 if approved or rejected else 1
        cost = round(input_tokens * 0.0000008 + output_tokens * 0.0000032, 6)
        self.db.execute(
            """INSERT INTO usage_ledger
               (id, tenant_id, project_id, run_id, input_tokens, output_tokens,
                model_calls, tool_calls, subagent_calls, cost, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 2, ?, ?, ?, ?)""",
            (
                new_id("usage"),
                run["tenant_id"],
                run["project_id"],
                run["id"],
                input_tokens,
                output_tokens,
                tool_calls,
                subagent_calls,
                cost,
                now,
            ),
        )
        self.events.append(
            run["id"],
            "usage.updated",
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "model_calls": 2,
                "tool_calls": tool_calls,
                "subagent_calls": subagent_calls,
                "cost": cost,
            },
        )
        self._set_status(run["id"], RunStatus.SUCCEEDED.value, output)
        self.db.execute(
            "UPDATE run_attempts SET status='SUCCEEDED', updated_at=? WHERE id=?",
            (utc_now(), run["current_attempt_id"]),
        )
        self.events.append(run["id"], "run.completed", {"status": "SUCCEEDED", "output": output})

    def _set_status(self, run_id: str, status: str, output: str = None) -> None:
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

    def _is_cancelled(self, run_id: str) -> bool:
        run = self.db.fetch_one("SELECT status FROM runs WHERE id=?", (run_id,))
        return not run or run["status"] in {"CANCELLED", "CANCELLING"}

