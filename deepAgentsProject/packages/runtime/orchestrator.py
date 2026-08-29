from __future__ import annotations

import asyncio
import secrets
from typing import Optional

from packages.domain.models import utc_now
from packages.knowledge.service import KnowledgeService
from packages.persistence import Database
from packages.runtime.event_emitter import EventEmitter
from packages.runtime.executor import ReferenceRuntimeExecutor
from packages.runtime.model_gateway import ModelGateway


class RunOrchestrator:
    def __init__(
        self,
        db: Database,
        events: EventEmitter,
        knowledge: Optional[KnowledgeService],
        model_gateway: ModelGateway,
    ):
        self.db = db
        self.events = events
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.worker_id = f"worker_model_{secrets.token_hex(3)}"
        self.executor = ReferenceRuntimeExecutor(
            db, events, self.worker_id, knowledge, model_gateway
        )
        self.task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if not self.task:
            self.task = asyncio.create_task(self._worker_loop())
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

    async def _worker_loop(self) -> None:
        while True:
            run_id = await self.queue.get()
            try:
                await self.executor.execute(run_id)
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
                self.queue.task_done()
