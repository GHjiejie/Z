from __future__ import annotations

from packages.auth.resource_access import ResourceAccess
from packages.auth.service import AuthAuthorizationError

import asyncio
import logging
import os
import secrets
from typing import Optional

from packages.coding.redaction import redact_text
from packages.domain.models import TERMINAL_RUN_STATUSES, utc_now
from packages.knowledge.service import KnowledgeService
from packages.persistence import Database
from packages.persistence.fencing import LeaseLostError, RunWriteFence, execution_scope
from packages.runtime.event_emitter import EventEmitter
from packages.runtime.budget import RunBudgetExceeded
from packages.runtime.executor_registry import ExecutorRegistry, RuntimeExecutor
from packages.runtime.executor import ReferenceRuntimeExecutor
from packages.runtime.model_gateway import ModelGateway
from packages.runtime.run_lease import RunLeaseManager, finalize_cancellation
from packages.runtime.task_queue import InMemoryTaskQueue, TaskQueue
from packages.runtime.worker_lease import WorkerLease


logger = logging.getLogger(__name__)


class RunOrchestrator:
    def __init__(
        self,
        db: Database,
        events: EventEmitter,
        knowledge: Optional[KnowledgeService],
        model_gateway: ModelGateway,
        coding_executor: RuntimeExecutor | None = None,
        queue: TaskQueue | None = None,
    ):
        self.db = db
        self.events = events
        self.queue = queue or InMemoryTaskQueue()
        self.worker_id = f"worker_model_{secrets.token_hex(8)}"
        self.worker_lease = WorkerLease(
            db, self.worker_id, "runtime", {"queue": "runtime-runs"}
        )
        self.run_leases = RunLeaseManager(
            db, self.worker_id,
            lease_seconds=int(os.getenv("DEEPAGENT_RUN_LEASE_SECONDS", "30")),
        )
        self.executors = ExecutorRegistry(
            ReferenceRuntimeExecutor(db, events, self.worker_id, knowledge, model_gateway),
            coding_executor,
        )
        self.task: asyncio.Task | None = None
        self.reconcile_task: asyncio.Task | None = None
        self.active_run_id: str | None = None
        self.active_execution_task: asyncio.Task | None = None
        self.active_executor: RuntimeExecutor | None = None

    async def start(self) -> None:
        await self.worker_lease.start()
        await self.reconcile()
        if self.task is None:
            self.task = asyncio.create_task(self._worker_loop())
            self.reconcile_task = asyncio.create_task(self._reconcile_loop())
            self.worker_lease.consumers = (self.task, self.reconcile_task)

    async def stop(self) -> None:
        for task in (self.reconcile_task, self.task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self.reconcile_task, self.task) if task is not None),
            return_exceptions=True,
        )
        self.task = self.reconcile_task = None
        await self.worker_lease.stop()

    def enqueue_in_transaction(self, run_id: str) -> bool:
        put = getattr(self.queue, "put_transactional", None)
        if put is None:
            return False
        run = self.db.fetch_one("SELECT current_attempt_id FROM runs WHERE id=?", (run_id,))
        if run:
            put(run_id, dedupe_key=run["current_attempt_id"])
        return True

    async def enqueue(self, run_id: str) -> None:
        run = self.db.fetch_one("SELECT current_attempt_id FROM runs WHERE id=?", (run_id,))
        if run:
            await self.queue.put(run_id, dedupe_key=run["current_attempt_id"])

    async def reconcile(self) -> None:
        now = self.db.current_time().isoformat()
        stale = self.db.fetch_all(
            """SELECT r.id FROM runs r JOIN run_attempts a ON a.id=r.current_attempt_id
               WHERE r.status IN ('CREATED','QUEUED','ORPHANED','PREPARING','RUNNING','RESUMING')
                 AND a.status='RUNNING' AND (a.expires_at IS NULL OR a.expires_at<=?)""",
            (now,),
        )
        for candidate in stale:
            with self.db.transaction():
                recovered = self.run_leases.recover(candidate["id"])
                if recovered:
                    run, number = recovered
                    if not number:
                        self.events.append(run["id"], "run.failed", {
                            "code": "WORKER_RECOVERY_EXHAUSTED", "message": run["output"],
                        })
                        continue
                    self.events.append(run["id"], "run.orphaned", {
                        "reason": "worker_lease_expired", "recovery_attempt": number,
                    })
                    self.enqueue_in_transaction(run["id"])
        pending = self.db.fetch_all(
            """SELECT r.id FROM runs r JOIN run_attempts a ON a.id=r.current_attempt_id
               WHERE r.status IN ('CREATED','QUEUED','ORPHANED') AND a.status='PENDING'"""
        )
        for run in pending:
            await self.enqueue(run["id"])
        for run in self.db.fetch_all("SELECT id FROM runs WHERE status='CANCELLING'"):
            await self.cancel_execution(run["id"])
            finalize_cancellation(self.db, self.events, run["id"])

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(float(os.getenv("DEEPAGENT_RECONCILE_SECONDS", "2")))
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Runtime reconciliation failed; retrying on next interval")

    async def cancel_execution(self, run_id: str) -> None:
        # The API may run on a different process from the owning Worker. Sandbox
        # interruption is addressed by run/workspace, not just a local Task ID.
        run = self.db.fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
        if run and run.get("coding_workspace_id") and self.executors.coding:
            await self.executors.coding.cancel(run_id)
        if self.active_run_id == run_id and self.active_execution_task:
            self.active_execution_task.cancel()

    def _abandon_safely(self, fence: RunWriteFence) -> None:
        try:
            self.run_leases.abandon(fence)
        except Exception:
            logger.exception("Could not revoke Run lease; expiry will fence the execution")

    async def _worker_loop(self) -> None:
        while True:
            try:
                run_id = await self.queue.get()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Runtime queue claim failed")
                await asyncio.sleep(1)
                continue
            fence: RunWriteFence | None = None
            retry_delivery = False
            failed = False
            error: str | None = None
            try:
                fence = self.run_leases.claim(
                    run_id, expected_attempt_id=getattr(self.queue, "delivery_key", None)
                )
                if fence is None:
                    continue
                run = self.db.fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
                plan = self.db.fetch_one(
                    "SELECT * FROM resolved_execution_plans WHERE id=?", (run["resolved_plan_id"],)
                )
                if not plan:
                    raise RuntimeError("Resolved execution plan is missing")
                self.active_executor = self.executors.resolve(plan["plan"])
                self.active_run_id = run_id
                await self._execute(fence, self.active_executor)
                result = self.db.fetch_one("SELECT status FROM runs WHERE id=?", (run_id,))
                failed = bool(result and result["status"] in {"FAILED", "FAILED_BUDGET", "TIMED_OUT"})
            except asyncio.CancelledError:
                stopping = bool(self.task and self.task.cancelling())
                try:
                    state = self.db.fetch_one("SELECT status FROM runs WHERE id=?", (run_id,))
                except Exception:
                    state = None
                retry_delivery = stopping or not state or state["status"] not in {"CANCELLING", "CANCELLED"}
                if retry_delivery and fence:
                    self._abandon_safely(fence)
                if stopping:
                    raise
            except LeaseLostError:
                retry_delivery = True
                error = "Execution lease lost"
                if fence:
                    self._abandon_safely(fence)
            except Exception as exc:
                failed = True
                failure_status = "FAILED_BUDGET" if isinstance(exc, RunBudgetExceeded) else "FAILED"
                failure_code = "RUN_BUDGET_EXCEEDED" if isinstance(exc, RunBudgetExceeded) else "MODEL_RUNTIME_ERROR"
                error = redact_text(str(exc))[:2000]
                if fence:
                    try:
                        with execution_scope(fence), self.db.transaction():
                            run = self.db.fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
                            if run and run["status"] not in TERMINAL_RUN_STATUSES:
                                now = utc_now()
                                self.db.execute(
                                    "UPDATE runs SET status=?, output=?, updated_at=? WHERE id=?",
                                    (failure_status, error, now, run_id),
                                )
                                self.db.execute(
                                    "UPDATE run_attempts SET status='FAILED', updated_at=? WHERE id=?",
                                    (now, fence.attempt_id),
                                )
                                self.events.append(run_id, "graph.failed", {
                                    "graph_id": fence.attempt_id, "status": "failed",
                                    "code": failure_code, "message": error,
                                }, span_id="span_main")
                                self.events.append(run_id, "run.failed", {
                                    "code": failure_code, "message": error,
                                })
                    except LeaseLostError:
                        retry_delivery = True
                    except Exception:
                        retry_delivery = True
                        logger.exception("Could not persist execution failure; leaving recovery to the lease reconciler")
            finally:
                self.active_run_id = None
                self.active_execution_task = None
                self.active_executor = None
                try:
                    if fence and not retry_delivery:
                        self.run_leases.release(fence)
                    if retry_delivery:
                        self.queue.release(error=error or "Worker stopped")
                    else:
                        self.queue.task_done(failed=failed, error=error)
                except Exception:
                    logger.exception("Could not settle queue delivery; lease expiry will recover it")

    async def _execute(self, fence: RunWriteFence, executor: RuntimeExecutor) -> None:
        async def invoke():
            from packages.operations.telemetry import task_operation
            with task_operation(self.db, 'run', fence.run_id, 'runtime.attempt', attempt_id=fence.attempt_id) as span, execution_scope(fence):
                ResourceAccess(self.db).require_execution(fence.run_id)
                await executor.execute(fence.run_id)
                result = self.db.fetch_one('SELECT status FROM runs WHERE id=?', (fence.run_id,))
                if span is not None and result and result['status'] in {'FAILED', 'FAILED_BUDGET', 'TIMED_OUT'}:
                    from opentelemetry.trace import StatusCode
                    span.set_status(StatusCode.ERROR)

        execution = asyncio.create_task(invoke())
        self.active_execution_task = execution
        monitor = asyncio.create_task(self._monitor(fence, execution))
        try:
            done, _ = await asyncio.wait((execution, monitor), return_when=asyncio.FIRST_COMPLETED)
            if monitor in done:
                await monitor
            await execution
        finally:
            monitor.cancel()
            if not execution.done():
                self._abandon_safely(fence)
                execution.cancel()
            await asyncio.gather(execution, monitor, return_exceptions=True)

    async def _monitor(self, fence: RunWriteFence, execution: asyncio.Task) -> None:
        interval = min(10, self.run_leases.lease_seconds / 3)
        next_heartbeat = 0.0
        loop = asyncio.get_running_loop()
        while not execution.done():
            try:
                ResourceAccess(self.db).require_execution(fence.run_id)
            except AuthAuthorizationError:
                with self.db.transaction():
                    self.db.execute("UPDATE runs SET status='CANCELLING',updated_at=? WHERE id=? AND status NOT IN ('SUCCEEDED','FAILED','FAILED_BUDGET','CANCELLED','TIMED_OUT')",
                                    (utc_now(), fence.run_id))
                    self.events.append(fence.run_id, "run.authorization_revoked", {"reason": "principal_or_resource_access_changed"})
                await self.cancel_execution(fence.run_id)
                finalize_cancellation(self.db, self.events, fence.run_id)
                raise
            with self.db.transaction() as connection:
                fence.validate(connection, self.db.dialect)
            if loop.time() >= next_heartbeat:
                self.run_leases.heartbeat(fence)
                await self.queue.heartbeat()
                next_heartbeat = loop.time() + interval
            await asyncio.sleep(min(0.5, interval))
