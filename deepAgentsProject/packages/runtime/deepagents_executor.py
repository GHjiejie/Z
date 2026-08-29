from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from packages.adapters.harness.deepagents import DeepAgentsHarnessAdapter
from packages.adapters.harness.deepagents.event_adapter import DeepAgentsEventAdapter
from packages.application.services import new_id
from packages.coding.changeset import ChangeSetBuilder, VerificationService
from packages.domain.models import RunStatus, utc_now
from packages.knowledge.service import KnowledgeService
from packages.knowledge.tool import KnowledgeSearchTool
from packages.persistence import Database
from packages.runtime.event_emitter import EventEmitter
from packages.sandbox.manager import BoundCodingWorkspace, SandboxManager


class CodingRunCancelled(RuntimeError):
    pass


class CodingBudgetExceeded(RuntimeError):
    pass


class DeepAgentsRuntimeExecutor:
    """Real Deep Agents/LangGraph executor for the coding-agent-v1 profile."""

    def __init__(
        self,
        db: Database,
        events: EventEmitter,
        worker_id: str,
        sandbox_manager: SandboxManager,
        checkpointer: BaseCheckpointSaver,
        model: BaseChatModel,
        model_identity: dict[str, Any],
        knowledge: KnowledgeService | None = None,
    ):
        self.db = db
        self.events = events
        self.worker_id = worker_id
        self.sandbox_manager = sandbox_manager
        self.checkpointer = checkpointer
        self.model = model
        self.model_identity = model_identity
        self.knowledge_tool = KnowledgeSearchTool(knowledge) if knowledge else None
        self.harness = DeepAgentsHarnessAdapter()
        self.verification = VerificationService(db, events)
        self.changesets = ChangeSetBuilder(db, events)

    async def execute(self, run_id: str) -> None:
        run = self.db.fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run or run["status"] == RunStatus.CANCELLED.value:
            return
        if run.get("principal_verified") != 1 or not run.get("principal_user_id"):
            raise RuntimeError("Coding run has no verified runtime principal")
        plan_row = self.db.fetch_one(
            "SELECT * FROM resolved_execution_plans WHERE id=?", (run["resolved_plan_id"],)
        )
        if not plan_row:
            raise RuntimeError("Resolved execution plan is missing")
        plan = plan_row["plan"]
        coding_profile = plan.get("coding_profile") or {}
        timeout_seconds = min(
            int(plan.get("limits", {}).get("max_duration_seconds", 600)),
            int((coding_profile.get("sandbox") or {}).get("run_timeout_seconds", 1800)),
        )
        resume_orphaned = run["status"] == RunStatus.ORPHANED.value
        self._acquire_lease(run)
        heartbeat = asyncio.create_task(self._heartbeat(run["current_attempt_id"]))
        bound: BoundCodingWorkspace | None = None
        try:
            async with asyncio.timeout(timeout_seconds):
                bound = await self._prepare(run, plan)
                await self._run_graph_and_verify(
                    run, plan, bound, resume_orphaned=resume_orphaned
                )
        except asyncio.CancelledError:
            if bound:
                try:
                    await asyncio.shield(
                        self._preserve_partial(run, plan, bound, "run_cancelled")
                    )
                except Exception:
                    pass
            return
        except CodingRunCancelled:
            if bound:
                try:
                    await asyncio.shield(
                        self._preserve_partial(run, plan, bound, "run_cancelled")
                    )
                except Exception:
                    pass
            return
        except TimeoutError:
            if bound:
                await self._preserve_partial(run, plan, bound, "run_timeout")
            self._finish_failed(
                run,
                RunStatus.TIMED_OUT.value,
                "CODING_RUN_TIMEOUT",
                f"Coding run exceeded {timeout_seconds} seconds",
            )
        except CodingBudgetExceeded as exc:
            if bound:
                await self._preserve_partial(run, plan, bound, "budget_exceeded")
            self._finish_failed(
                run,
                RunStatus.FAILED_BUDGET.value,
                "CODING_BUDGET_EXCEEDED",
                str(exc),
            )
        except Exception:
            if bound:
                try:
                    await asyncio.shield(
                        self._preserve_partial(run, plan, bound, "runtime_error")
                    )
                except Exception:
                    pass
            raise
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    async def cancel(self, run_id: str) -> None:
        await self.sandbox_manager.interrupt_run(run_id)
        run = self.db.fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run or not run.get("coding_workspace_id"):
            return
        plan_row = self.db.fetch_one(
            "SELECT * FROM resolved_execution_plans WHERE id=?",
            (run["resolved_plan_id"],),
        )
        if not plan_row:
            return
        try:
            bound = await self.sandbox_manager.bind(run, plan_row["plan"])
            await self._preserve_partial(
                run, plan_row["plan"], bound, "run_cancelled"
            )
        except Exception as exc:
            self.events.append(
                run_id,
                "workspace.snapshot.failed",
                {"reason": "run_cancelled", "message": str(exc)[:500]},
            )

    async def _prepare(
        self, run: dict[str, Any], plan: dict[str, Any]
    ) -> BoundCodingWorkspace:
        self._set_status(run["id"], RunStatus.PREPARING.value)
        self.events.append(
            run["id"],
            "run.preparing",
            {
                "worker_id": self.worker_id,
                "plan_hash": plan["plan_hash"],
                "runtime_image_digest": plan["runtime_image_digest"],
                "executor": "deepagents-coding",
            },
        )
        bound = await self.sandbox_manager.bind(run, plan)
        source = self.db.fetch_one(
            """SELECT s.*, r.id AS repository_id, r.name AS repository_name
               FROM repository_snapshots s
               JOIN repositories r ON r.id=s.repository_id
               WHERE s.id=?""",
            (bound.workspace["repository_snapshot_id"],),
        )
        if source:
            self.events.append(
                run["id"],
                "repository.snapshot.resolved",
                {
                    "repository_id": source["repository_id"],
                    "repository_snapshot_id": source["id"],
                    "requested_ref": source["requested_ref"],
                    "resolved_commit_sha": source["resolved_commit_sha"],
                    "manifest_hash": source["manifest_hash"],
                },
            )
        self._set_status(run["id"], RunStatus.RUNNING.value)
        self.events.append(
            run["id"],
            "run.started",
            {
                "worker_id": self.worker_id,
                "attempt_id": run["current_attempt_id"],
                "harness_adapter_version": self.harness.adapter_version,
                "sandbox_instance_id": bound.sandbox_instance["id"],
                "workspace_id": bound.workspace["id"],
            },
            span_id="span_main",
        )
        self.events.append(
            run["id"],
            "graph.started",
            {
                "graph_id": run["current_attempt_id"],
                "graph_name": "coding-agent",
                "entry_node": "model",
            },
            span_id="span_main",
            execution_path=["main"],
        )
        for skill in plan.get("skill_versions", []):
            self.events.append(
                run["id"],
                "skill.loaded",
                {
                    "revision_id": skill["revision_id"],
                    "slug": skill["slug"],
                    "version": skill["version"],
                    "artifact_hash": skill["artifact_hash"],
                },
                span_id="span_main",
            )
        return bound

    async def _run_graph_and_verify(
        self,
        run: dict[str, Any],
        plan: dict[str, Any],
        bound: BoundCodingWorkspace,
        *,
        resume_orphaned: bool = False,
    ) -> None:
        graph = self.harness.build_coding_graph(
            model=self.model,
            backend=bound.backend,
            plan=plan,
            skill_paths=bound.skill_paths,
            checkpointer=self.checkpointer,
            db=self.db,
            run_id=run["id"],
            knowledge_tool=self.knowledge_tool,
            runtime_context={
                "tenant_id": run["tenant_id"],
                "project_id": run["project_id"],
                "environment_id": run.get("principal_environment_id")
                or "env_development",
                "user_id": run["principal_user_id"],
                "roles": run.get("principal_roles") or [],
                "run_id": run["id"],
            },
        )
        config = {
            "configurable": {"thread_id": self._graph_thread_id(run)},
            "recursion_limit": max(
                50, int(plan.get("limits", {}).get("max_tool_calls", 30)) * 4 + 20
            ),
        }
        adapter = DeepAgentsEventAdapter(
            self.events, run, self.model_identity
        )
        self._validate_resume_checkpoint(run, plan, bound.workspace["id"])
        graph_input = self._graph_input(run)
        if resume_orphaned:
            checkpoint_tuple = await self.checkpointer.aget_tuple(config)
            if checkpoint_tuple is not None:
                graph_input = None
                self.events.append(
                    run["id"],
                    "graph.resumed",
                    {
                        "checkpoint_id": checkpoint_tuple.config.get(
                            "configurable", {}
                        ).get("checkpoint_id"),
                        "reason": "worker_lease_recovery",
                    },
                    span_id="span_main",
                    execution_path=["main"],
                )
        await self._stream_graph(graph, graph_input, config, adapter, plan)
        if adapter.interrupt:
            await self._pause_for_interrupt(run, plan, bound, graph, config, adapter)
            return

        verification_policy = (plan.get("coding_profile") or {}).get(
            "verification_policy", {}
        )
        attempts = int(verification_policy.get("max_attempts", 2))
        report = None
        for attempt in range(1, attempts + 1):
            self._assert_active(run["id"])
            workspace = self._workspace(bound.workspace["id"])
            self.db.execute(
                "UPDATE coding_workspaces SET status='VERIFYING', updated_at=? WHERE id=?",
                (utc_now(), workspace["id"]),
            )
            report = self.verification.run(
                run, workspace, bound.backend, verification_policy
            )
            self._assert_budgets(adapter, plan)
            if report["status"] != "FAILED" or attempt == attempts:
                break
            failures = [
                check
                for check in report.get("checks", [])
                if check.get("status") == "failed"
            ]
            feedback = json.dumps(failures, ensure_ascii=False)[:6000]
            self.events.append(
                run["id"],
                "verification.retry.requested",
                {"attempt": attempt + 1, "failed_checks": len(failures)},
            )
            await self._stream_graph(
                graph,
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Platform verification failed. Inspect the real results below, "
                                "make a minimal correction, and rerun targeted checks before finishing.\n"
                                + feedback
                            ),
                        }
                    ]
                },
                config,
                adapter,
                plan,
            )
            if adapter.interrupt:
                await self._pause_for_interrupt(
                    run, plan, bound, graph, config, adapter
                )
                return

        workspace = self._workspace(bound.workspace["id"])
        snapshot = self.db.fetch_one(
            "SELECT * FROM repository_snapshots WHERE id=?",
            (workspace["repository_snapshot_id"],),
        )
        if not snapshot:
            raise RuntimeError("Repository snapshot is missing")
        change_set = self.changesets.build(
            run,
            workspace,
            snapshot,
            bound.backend,
            report,
            plan.get("coding_profile") or {},
            plan_hash=plan["plan_hash"],
        )
        await self.sandbox_manager.snapshot_workspace(
            workspace, run=run, plan=plan, reason="run_completed"
        )
        self._create_standard_artifacts(run, plan, adapter.output, report, change_set)
        self._record_usage(run, adapter)
        require_success = bool(verification_policy.get("require_success", True))
        if report and report["status"] == "FAILED" and require_success:
            self.db.execute(
                "UPDATE change_sets SET status='PARTIAL_FAILED' WHERE id=?",
                (change_set["id"],),
            )
            self._finish_failed(
                run,
                RunStatus.FAILED.value,
                "VERIFICATION_FAILED",
                "Platform verification failed; a partial ChangeSet was preserved",
            )
            return

        output = adapter.output or "Coding task completed; review the generated ChangeSet."
        now = utc_now()
        self.db.execute(
            """UPDATE runs SET status='SUCCEEDED', output=?, checkpoint_json=?,
               version=version+1, updated_at=? WHERE id=?""",
            (
                output,
                self.db.encode(
                    {
                        "stage": "completed",
                        "plan_hash": plan["plan_hash"],
                        "workspace_generation": workspace["workspace_generation"],
                    }
                ),
                now,
                run["id"],
            ),
        )
        self.db.execute(
            "UPDATE run_attempts SET status='SUCCEEDED', updated_at=? WHERE id=?",
            (now, run["current_attempt_id"]),
        )
        self.db.execute(
            "UPDATE coding_workspaces SET status='REVIEW_READY', updated_at=? WHERE id=?",
            (now, workspace["id"]),
        )
        self.events.append(
            run["id"],
            "graph.completed",
            {"graph_id": run["current_attempt_id"], "status": "completed"},
            span_id="span_main",
            execution_path=["main"],
        )
        self.events.append(
            run["id"],
            "run.completed",
            {
                "changeset_id": change_set["id"],
                "verification_status": report["status"] if report else "NOT_CONFIGURED",
            },
        )

    async def _stream_graph(
        self,
        graph: Any,
        graph_input: Any,
        config: dict[str, Any],
        adapter: DeepAgentsEventAdapter,
        plan: dict[str, Any],
    ) -> None:
        async for part in graph.astream(
            graph_input,
            config=config,
            stream_mode=["messages", "updates"],
            subgraphs=True,
            version="v2",
        ):
            self._assert_active(adapter.run["id"])
            adapter.consume(part)
            self._assert_budgets(adapter, plan)
            if adapter.interrupt:
                break

    async def _pause_for_interrupt(
        self,
        run: dict[str, Any],
        plan: dict[str, Any],
        bound: BoundCodingWorkspace,
        graph: Any,
        config: dict[str, Any],
        adapter: DeepAgentsEventAdapter,
    ) -> None:
        state = await graph.aget_state(config)
        graph_checkpoint_id = state.config.get("configurable", {}).get("checkpoint_id")
        interrupt = adapter.interrupt or {}
        action_requests = interrupt.get("action_requests") or []
        review_configs = interrupt.get("review_configs") or []
        actions = []
        for index, request in enumerate(action_requests):
            review = review_configs[index] if index < len(review_configs) else {}
            actions.append(
                {
                    "action_id": new_id("act"),
                    "tool_name": request.get("name", "unknown"),
                    "arguments": request.get("args") or {},
                    "risk_level": "high",
                    "allowed_decisions": review.get("allowed_decisions")
                    or ["approve", "edit", "reject", "respond"],
                }
            )
        interrupt_id = new_id("int")
        checkpoint_id = graph_checkpoint_id or new_id("ckpt")
        now = utc_now()
        self.db.execute(
            """INSERT INTO interrupts
               (id, tenant_id, project_id, run_id, checkpoint_id, version,
                policy_reason, status, actions_json, expires_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 1, ?, 'PENDING', ?, ?, ?, ?)""",
            (
                interrupt_id,
                run["tenant_id"],
                run["project_id"],
                run["id"],
                checkpoint_id,
                "Coding action requires explicit approval",
                self.db.encode(actions),
                (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
                now,
                now,
            ),
        )
        workspace = self._workspace(bound.workspace["id"])
        source_snapshot = self.db.fetch_one(
            "SELECT * FROM repository_snapshots WHERE id=?",
            (workspace["repository_snapshot_id"],),
        )
        if not source_snapshot:
            raise RuntimeError("Repository snapshot is missing at interrupt boundary")
        change_set = self.changesets.build(
            run,
            workspace,
            source_snapshot,
            bound.backend,
            None,
            plan.get("coding_profile") or {},
            plan_hash=plan["plan_hash"],
        )
        await self.sandbox_manager.snapshot_workspace(
            workspace, run=run, plan=plan, reason="human_interrupt"
        )
        self._create_standard_artifacts(
            run,
            plan,
            adapter.output or "Execution paused for an approved protected-path action.",
            {"status": "PARTIAL", "reason": "human_interrupt"},
            change_set,
        )
        checkpoint = {
            "stage": "awaiting_approval",
            "checkpoint_id": checkpoint_id,
            "interrupt_id": interrupt_id,
            "langgraph_interrupt_id": interrupt.get("langgraph_interrupt_id"),
            "langgraph_thread_id": self._graph_thread_id(run),
            "action_requests": action_requests,
            "plan_hash": plan["plan_hash"],
            "base_commit_sha": self._base_commit(workspace),
            "workspace_generation": workspace["workspace_generation"],
        }
        self.db.execute(
            """UPDATE runs SET status='WAITING_FOR_APPROVAL', checkpoint_json=?,
               version=version+1, updated_at=? WHERE id=?""",
            (self.db.encode(checkpoint), now, run["id"]),
        )
        self.db.execute(
            "UPDATE coding_workspaces SET last_checkpoint_id=?, updated_at=? WHERE id=?",
            (checkpoint_id, now, workspace["id"]),
        )
        self.db.execute(
            "UPDATE run_attempts SET status='SUCCEEDED', updated_at=? WHERE id=?",
            (now, run["current_attempt_id"]),
        )
        self._record_usage(run, adapter)
        self.events.append(
            run["id"],
            "tool.approval_required",
            {"interrupt_id": interrupt_id, "actions": actions},
        )
        self.events.append(
            run["id"],
            "interrupt.created",
            {"interrupt_id": interrupt_id, "checkpoint_id": checkpoint_id, "version": 1},
        )
        self.events.append(
            run["id"],
            "graph.paused",
            {
                "graph_id": run["current_attempt_id"],
                "checkpoint_id": checkpoint_id,
                "interrupt_id": interrupt_id,
                "reason": "human_approval_required",
            },
            span_id="span_main",
            execution_path=["main"],
        )

    def _graph_input(self, run: dict[str, Any]) -> Any:
        checkpoint = run.get("checkpoint") or {}
        if checkpoint.get("stage") in {"approval_resolved", "input_received"} and (
            checkpoint.get("decisions") or checkpoint.get("decision")
        ):
            decisions = checkpoint.get("decisions") or [
                checkpoint.get("decision") or {"type": "approve"}
            ]
            requests = checkpoint.get("action_requests") or []
            response_message = (run.get("metadata") or {}).get("resume_input")
            graph_decisions = []
            for index, decision in enumerate(decisions):
                if decision.get("type") == "edit":
                    original = requests[index] if index < len(requests) else {}
                    graph_decisions.append(
                        {
                            "type": "edit",
                            "edited_action": {
                                "name": original.get("name"),
                                "args": decision.get("edited_arguments")
                                or original.get("args")
                                or {},
                            },
                        }
                    )
                elif decision.get("type") == "respond":
                    graph_decisions.append(
                        {
                            "type": "respond",
                            "message": response_message
                            or decision.get("message")
                            or "The reviewer requested a different approach.",
                        }
                    )
                else:
                    graph_decisions.append({"type": "approve"})
            self.events.append(
                run["id"],
                "run.resumed",
                {"checkpoint_id": checkpoint.get("checkpoint_id")},
            )
            self.events.append(
                run["id"],
                "graph.resumed",
                {"checkpoint_id": checkpoint.get("checkpoint_id")},
                span_id="span_main",
                execution_path=["main"],
            )
            return Command(resume={"decisions": graph_decisions})
        text = (run.get("metadata") or {}).get("resume_input") or run["input"]
        return {"messages": [{"role": "user", "content": text}]}

    def _validate_resume_checkpoint(
        self, run: dict[str, Any], plan: dict[str, Any], workspace_id: str
    ) -> None:
        checkpoint = run.get("checkpoint") or {}
        if checkpoint.get("stage") not in {"approval_resolved", "input_received"}:
            return
        workspace = self._workspace(workspace_id)
        if checkpoint.get("plan_hash") != plan.get("plan_hash"):
            raise RuntimeError("Checkpoint plan hash does not match the immutable run plan")
        if checkpoint.get("base_commit_sha") != self._base_commit(workspace):
            raise RuntimeError("Checkpoint base commit does not match the coding workspace")
        if int(checkpoint.get("workspace_generation", -1)) != int(
            workspace["workspace_generation"]
        ):
            raise RuntimeError("Checkpoint workspace generation does not match restored files")

    async def _preserve_partial(
        self,
        run: dict[str, Any],
        plan: dict[str, Any],
        bound: BoundCodingWorkspace,
        reason: str,
    ) -> None:
        try:
            workspace = self._workspace(bound.workspace["id"])
            snapshot = self.db.fetch_one(
                "SELECT * FROM repository_snapshots WHERE id=?",
                (workspace["repository_snapshot_id"],),
            )
            await self.sandbox_manager.snapshot_workspace(
                workspace, run=run, plan=plan, reason=reason
            )
            existing = self.db.fetch_one(
                "SELECT * FROM change_sets WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
                (run["id"],),
            )
            change_set = existing
            if snapshot and (
                not existing
                or int(existing["workspace_generation"])
                != int(workspace["workspace_generation"])
            ):
                change_set = self.changesets.build(
                    run,
                    workspace,
                    snapshot,
                    bound.backend,
                    None,
                    plan.get("coding_profile") or {},
                    plan_hash=plan["plan_hash"],
                )
            if change_set:
                report = self.db.fetch_one(
                    "SELECT * FROM verification_reports WHERE run_id=?", (run["id"],)
                ) or {"status": "PARTIAL", "reason": reason}
                self._create_standard_artifacts(
                    run,
                    plan,
                    f"Execution stopped at a safe boundary: {reason}.",
                    report,
                    change_set,
                )
        except Exception as exc:
            self.events.append(
                run["id"],
                "workspace.snapshot.failed",
                {"reason": reason, "message": str(exc)[:500]},
            )

    def _create_standard_artifacts(
        self,
        run: dict[str, Any],
        plan: dict[str, Any],
        model_output: str,
        report: dict[str, Any] | None,
        change_set: dict[str, Any],
    ) -> None:
        self._artifact(
            run,
            "verification-report.json",
            "application/json",
            json.dumps(report or {"status": "NOT_CONFIGURED"}, ensure_ascii=False, indent=2),
        )
        commands = self.db.fetch_all(
            "SELECT * FROM sandbox_commands WHERE run_id=? ORDER BY created_at", (run["id"],)
        )
        self._artifact(
            run,
            "command-log.txt",
            "text/plain",
            "\n".join(
                f"{item['created_at']} {item['status']} exit={item.get('exit_code')} "
                f"duration_ms={item.get('duration_ms')} {item['command_preview']}"
                for item in commands
            ),
        )
        summary = (
            "# Coding Agent Summary\n\n"
            f"- Plan hash: `{plan['plan_hash']}`\n"
            f"- Base commit: `{change_set['base_commit_sha']}`\n"
            f"- Workspace generation: `{change_set['workspace_generation']}`\n"
            f"- ChangeSet: `{change_set['id']}`\n"
            f"- Verification: `{(report or {}).get('status', 'NOT_CONFIGURED')}`\n\n"
            "## Agent report\n\n"
            + (model_output or "No textual model summary was produced.")
        )
        self._artifact(run, "coding-agent-summary.md", "text/markdown", summary)

    def _artifact(
        self, run: dict[str, Any], name: str, media_type: str, content: str
    ) -> str:
        artifact_id = new_id("art")
        encoded = content.encode()
        digest = hashlib.sha256(encoded).hexdigest()
        self.db.execute(
            """INSERT INTO artifacts
               (id, tenant_id, project_id, run_id, name, media_type, size_bytes,
                content_hash, content, plan_hash, base_commit_sha, workspace_generation,
                artifact_metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                artifact_id,
                run["tenant_id"],
                run["project_id"],
                run["id"],
                name,
                media_type,
                len(encoded),
                digest,
                content,
                self._plan_hash(run),
                self._run_base_commit(run),
                self._run_workspace_generation(run),
                self.db.encode({"kind": "coding_run"}),
                utc_now(),
            ),
        )
        self.events.append(
            run["id"],
            "artifact.created",
            {"artifact_id": artifact_id, "name": name, "media_type": media_type, "content_hash": digest},
        )
        return artifact_id

    def _record_usage(
        self, run: dict[str, Any], adapter: DeepAgentsEventAdapter
    ) -> None:
        if not (adapter.model_calls or adapter.tool_calls):
            return
        plan_row = self.db.fetch_one(
            "SELECT plan_json FROM resolved_execution_plans WHERE id=?",
            (run["resolved_plan_id"],),
        )
        pricing = (
            ((plan_row or {}).get("plan") or {})
            .get("model_snapshot", {})
            .get("pricing", {})
        )
        cost = (
            adapter.input_tokens * float(pricing.get("input_per_million") or 0)
            + adapter.output_tokens * float(pricing.get("output_per_million") or 0)
        ) / 1_000_000
        self.db.execute(
            """INSERT INTO usage_ledger
               (id, tenant_id, project_id, run_id, input_tokens, output_tokens,
                model_calls, tool_calls, subagent_calls, cost, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("usage"),
                run["tenant_id"],
                run["project_id"],
                run["id"],
                adapter.input_tokens,
                adapter.output_tokens,
                adapter.model_calls,
                adapter.tool_calls,
                adapter.subagent_calls,
                cost,
                utc_now(),
            ),
        )

    def _assert_budgets(
        self, adapter: DeepAgentsEventAdapter, plan: dict[str, Any]
    ) -> None:
        limits = plan.get("limits") or {}
        if adapter.model_calls > int(limits.get("max_model_calls", 20)):
            raise CodingBudgetExceeded("Model call budget exhausted")
        if adapter.tool_calls > int(limits.get("max_tool_calls", 30)):
            raise CodingBudgetExceeded("Tool call budget exhausted")
        if len(adapter.output.encode("utf-8")) > int(
            limits.get("max_output_bytes", 1_000_000)
        ):
            raise CodingBudgetExceeded("Model output budget exhausted")
        command_usage = self.db.fetch_all(
            "SELECT resource_usage_json FROM sandbox_commands WHERE run_id=?",
            (adapter.run["id"],),
        )
        cpu_seconds = sum(
            float((item.get("resource_usage") or {}).get("cpu_seconds") or 0)
            for item in command_usage
        )
        if cpu_seconds > float(limits.get("max_sandbox_cpu_seconds", 120)):
            raise CodingBudgetExceeded("Sandbox CPU budget exhausted")
        max_cost = limits.get("max_cost")
        pricing = (plan.get("model_snapshot") or {}).get("pricing") or {}
        estimated_cost = (
            adapter.input_tokens * float(pricing.get("input_per_million") or 0)
            + adapter.output_tokens * float(pricing.get("output_per_million") or 0)
        ) / 1_000_000
        if max_cost is not None and estimated_cost > float(max_cost):
            raise CodingBudgetExceeded("Model cost budget exhausted")

    def _assert_active(self, run_id: str) -> None:
        row = self.db.fetch_one("SELECT status FROM runs WHERE id=?", (run_id,))
        if not row or row["status"] in {
            RunStatus.CANCELLING.value,
            RunStatus.CANCELLED.value,
        }:
            raise CodingRunCancelled()

    def _acquire_lease(self, run: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        self.db.execute(
            """UPDATE run_attempts SET status='RUNNING', worker_id=?, lease_token=?,
               acquired_at=?, heartbeat_at=?, expires_at=?, updated_at=? WHERE id=?""",
            (
                self.worker_id,
                f"lease_{secrets.token_hex(8)}",
                now.isoformat(),
                now.isoformat(),
                (now + timedelta(seconds=30)).isoformat(),
                now.isoformat(),
                run["current_attempt_id"],
            ),
        )

    async def _heartbeat(self, attempt_id: str) -> None:
        while True:
            await asyncio.sleep(10)
            now = datetime.now(timezone.utc)
            self.db.execute(
                """UPDATE run_attempts SET heartbeat_at=?, expires_at=?, updated_at=?
                   WHERE id=? AND status='RUNNING'""",
                (
                    now.isoformat(),
                    (now + timedelta(seconds=30)).isoformat(),
                    now.isoformat(),
                    attempt_id,
                ),
            )

    def _finish_failed(
        self, run: dict[str, Any], status: str, code: str, message: str
    ) -> None:
        now = utc_now()
        self.db.execute(
            "UPDATE runs SET status=?, output=?, version=version+1, updated_at=? WHERE id=?",
            (status, message, now, run["id"]),
        )
        self.db.execute(
            "UPDATE run_attempts SET status='FAILED', updated_at=? WHERE id=?",
            (now, run["current_attempt_id"]),
        )
        self.events.append(
            run["id"],
            "graph.failed",
            {"graph_id": run["current_attempt_id"], "code": code, "message": message},
            span_id="span_main",
            execution_path=["main"],
        )
        self.events.append(run["id"], "run.failed", {"code": code, "message": message})

    def _set_status(self, run_id: str, status: str) -> None:
        self.db.execute(
            "UPDATE runs SET status=?, version=version+1, updated_at=? WHERE id=?",
            (status, utc_now(), run_id),
        )

    def _workspace(self, workspace_id: str) -> dict[str, Any]:
        workspace = self.db.fetch_one(
            "SELECT * FROM coding_workspaces WHERE id=?", (workspace_id,)
        )
        if not workspace:
            raise RuntimeError("Coding workspace disappeared")
        return workspace

    def _base_commit(self, workspace: dict[str, Any]) -> str:
        snapshot = self.db.fetch_one(
            "SELECT resolved_commit_sha FROM repository_snapshots WHERE id=?",
            (workspace["repository_snapshot_id"],),
        )
        return str((snapshot or {}).get("resolved_commit_sha") or "")

    def _plan_hash(self, run: dict[str, Any]) -> str | None:
        row = self.db.fetch_one(
            "SELECT plan_hash FROM resolved_execution_plans WHERE id=?",
            (run["resolved_plan_id"],),
        )
        return str(row["plan_hash"]) if row else None

    def _run_base_commit(self, run: dict[str, Any]) -> str | None:
        if not run.get("coding_workspace_id"):
            return None
        workspace = self.db.fetch_one(
            "SELECT * FROM coding_workspaces WHERE id=?", (run["coding_workspace_id"],)
        )
        return self._base_commit(workspace) if workspace else None

    def _run_workspace_generation(self, run: dict[str, Any]) -> int | None:
        if not run.get("coding_workspace_id"):
            return None
        workspace = self.db.fetch_one(
            "SELECT workspace_generation FROM coding_workspaces WHERE id=?",
            (run["coding_workspace_id"],),
        )
        return int(workspace["workspace_generation"]) if workspace else None

    @staticmethod
    def _graph_thread_id(run: dict[str, Any]) -> str:
        return f"{run['tenant_id']}:{run['project_id']}:{run['thread_id']}"
