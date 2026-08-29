from __future__ import annotations

import asyncio
import secrets
from typing import Optional

from packages.domain.models import utc_now
from packages.knowledge.service import KnowledgeService
from packages.persistence import Database
from packages.runtime.event_emitter import EventEmitter
from packages.runtime.executor_registry import ExecutorRegistry, RuntimeExecutor
from packages.runtime.executor import ReferenceRuntimeExecutor
from packages.runtime.model_gateway import ModelGateway


class RunOrchestrator:
    def __init__(
        self,
        db: Database,
        events: EventEmitter,
        knowledge: Optional[KnowledgeService],
        model_gateway: ModelGateway,
        coding_executor: RuntimeExecutor | None = None,
    ):
        self.db = db
        self.events = events
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.worker_id = f"worker_model_{secrets.token_hex(3)}"
        reference_executor = ReferenceRuntimeExecutor(
            db, events, self.worker_id, knowledge, model_gateway
        )
        self.executors = ExecutorRegistry(reference_executor, coding_executor)
        self.task: Optional[asyncio.Task] = None
        self.active_run_id: Optional[str] = None
        self.active_execution_task: Optional[asyncio.Task] = None
        self.active_executor: Optional[RuntimeExecutor] = None

    async def start(self) -> None:
        if not self.task:
            self.task = asyncio.create_task(self._worker_loop())
        now = utc_now()
        stale = self.db.fetch_all(
            """SELECT r.id, r.current_attempt_id FROM runs r
               JOIN run_attempts a ON a.id=r.current_attempt_id
               WHERE r.status IN ('PREPARING','RUNNING','RESUMING')
                 AND (a.expires_at IS NULL OR a.expires_at < ?)""",
            (now,),
        )
        for run in stale:
            attempt_number = self.db.fetch_one(
                """SELECT COALESCE(MAX(attempt_number), 0) AS value
                   FROM run_attempts WHERE run_id=?""",
                (run["id"],),
            )["value"] + 1
            attempt_id = f"att_{secrets.token_hex(8)}"
            self.db.execute(
                "UPDATE run_attempts SET status='ORPHANED', updated_at=? WHERE id=?",
                (now, run["current_attempt_id"]),
            )
            self.db.execute(
                """INSERT INTO run_attempts
                   (id, run_id, attempt_number, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'PENDING', ?, ?)""",
                (attempt_id, run["id"], attempt_number, now, now),
            )
            self.db.execute(
                """UPDATE runs SET status='ORPHANED', current_attempt_id=?,
                   version=version+1, updated_at=? WHERE id=?""",
                (attempt_id, now, run["id"]),
            )
            self.events.append(
                run["id"],
                "run.orphaned",
                {
                    "reason": "worker_lease_expired",
                    "recovery_attempt": attempt_number,
                },
            )
        queued = self.db.fetch_all("SELECT id FROM runs WHERE status IN ('CREATED', 'QUEUED', 'ORPHANED')")
        for run in queued:
            await self.enqueue(run["id"])

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def enqueue(self, run_id: str) -> None:
        await self.queue.put(run_id)

    async def cancel_execution(self, run_id: str) -> None:
        if self.active_run_id == run_id and self.active_execution_task:
            execution_task = self.active_execution_task
            executor = self.active_executor
            cancel = getattr(executor, "cancel", None)
            if cancel is not None:
                await cancel(run_id)
            if not execution_task.done():
                execution_task.cancel()

    async def _worker_loop(self) -> None:
        while True:
            run_id = await self.queue.get()
            try:
                run = self.db.fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
                if not run or run["status"] == "CANCELLED":
                    continue
                plan_row = self.db.fetch_one(
                    "SELECT * FROM resolved_execution_plans WHERE id=?",
                    (run["resolved_plan_id"],),
                )
                if not plan_row:
                    raise RuntimeError("Resolved execution plan is missing")
                plan = plan_row["plan"]
                executor = self.executors.resolve(plan)
                self.active_executor = executor
                self.active_run_id = run_id
                self.active_execution_task = asyncio.create_task(executor.execute(run_id))
                try:
                    await self.active_execution_task
                except asyncio.CancelledError:
                    # Cancelling an individual execution must not terminate the
                    # long-lived queue worker. stop() still cancels this worker.
                    if self.task and self.task.cancelling():
                        raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                run = self.db.fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
                if run and run["status"] not in {"CANCELLED", "SUCCEEDED"}:
                    self.db.execute(
                        "UPDATE runs SET status='FAILED', output=?, updated_at=? WHERE id=?",
                        (str(exc), utc_now(), run_id),
                    )
                    self.db.execute(
                        "UPDATE run_attempts SET status='FAILED', updated_at=? WHERE id=?",
                        (utc_now(), run["current_attempt_id"]),
                    )
                    self.events.append(
                        run_id,
                        "graph.failed",
                        {
                            "graph_id": run["current_attempt_id"],
                            "status": "failed",
                            "code": "MODEL_RUNTIME_ERROR",
                            "message": str(exc),
                        },
                        span_id="span_main",
                        execution_path=["main"],
                    )
                    self.events.append(
                        run_id,
                        "run.failed",
                        {"code": "MODEL_RUNTIME_ERROR", "message": str(exc)},
                    )
            finally:
                self.active_run_id = None
                self.active_execution_task = None
                self.active_executor = None
                self.queue.task_done()
