from __future__ import annotations

import asyncio
import contextvars
import os
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from packages.persistence import Database
from packages.persistence.fencing import LeaseLostError


class TaskQueue(Protocol):
    async def put(self, payload: str, *, dedupe_key: str | None = None) -> None: ...

    async def get(self) -> str: ...

    def task_done(self, *, failed: bool = False, error: str | None = None) -> None: ...

    def release(self, *, error: str = "worker stopped") -> None: ...

    def qsize(self) -> int: ...

    async def heartbeat(self) -> None: ...


class InMemoryTaskQueue(asyncio.Queue):
    def __init__(self):
        super().__init__()
        self._delivery = contextvars.ContextVar("memory_queue_delivery", default=None)
        self._active: set[tuple[str, str | None]] = set()

    @property
    def delivery_key(self) -> str | None:
        delivery = self._delivery.get()
        return delivery[1] if delivery else None

    async def put(self, payload: str, *, dedupe_key: str | None = None) -> None:
        delivery = (payload, dedupe_key)
        if delivery not in self._active:
            self._active.add(delivery)
            super().put_nowait(delivery)

    async def get(self) -> str:
        delivery = await super().get()
        self._delivery.set(delivery)
        return delivery[0]

    def task_done(self, *, failed: bool = False, error: str | None = None) -> None:
        delivery = self._delivery.get()
        self._active.discard(delivery)
        super().task_done()
        self._delivery.set(None)

    def release(self, *, error: str = "worker stopped") -> None:
        delivery = self._delivery.get()
        if delivery:
            super().put_nowait(delivery)
            super().task_done()
            self._delivery.set(None)

    async def heartbeat(self) -> None:
        return None


@dataclass(frozen=True)
class QueueDelivery:
    task_id: str
    dedupe_key: str
    generation: int


class PostgresTaskQueue:
    """Durable, lease-based PostgreSQL queue safe for competing workers."""

    def __init__(
        self,
        db: Database,
        queue_name: str,
        *,
        lease_seconds: int = 300,
        poll_seconds: float = 0.25,
    ):
        self.db = db
        self.queue_name = queue_name
        self.worker_id = f"queue_worker_{secrets.token_hex(8)}"
        self.lease_seconds = max(30, lease_seconds)
        self.poll_seconds = max(0.05, poll_seconds)
        self._delivery: contextvars.ContextVar[QueueDelivery | None] = (
            contextvars.ContextVar(f"deepagent_queue_delivery_{queue_name}", default=None)
        )

    @property
    def delivery_key(self) -> str | None:
        delivery = self._delivery.get()
        return delivery.dedupe_key if delivery else None

    async def put(self, payload: str, *, dedupe_key: str | None = None) -> None:
        await asyncio.to_thread(self.put_transactional, payload, dedupe_key=dedupe_key)

    def put_transactional(self, payload: str, *, dedupe_key: str | None = None) -> None:
        """Joins the caller's database transaction; no network queue dual-write."""
        now = self.db.current_time().isoformat()
        key = dedupe_key or payload
        active_key = f"{self.queue_name}:{key}"
        self.db.execute(
            """INSERT OR IGNORE INTO task_queue
               (id, queue_name, payload_json, dedupe_key, active_key, status,
                priority, attempts, max_attempts, available_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'QUEUED', 0, 0, 3, ?, ?, ?)""",
            (
                f"task_{secrets.token_hex(16)}",
                self.queue_name,
                self.db.encode({"value": payload}),
                key,
                active_key,
                now,
                now,
                now,
            ),
        )

    async def get(self) -> str:
        while True:
            task = await asyncio.to_thread(self._claim)
            if task:
                self._delivery.set(QueueDelivery(task["id"], task["dedupe_key"], task["attempts"]))
                return str(task["payload"]["value"])
            await asyncio.sleep(self.poll_seconds)

    def _claim(self):
        now = self.db.current_time()
        timestamp = now.isoformat()
        self.db.execute(
            """UPDATE task_queue SET status='FAILED', active_key=NULL,
                      last_error='lease expired after maximum attempts', updated_at=?
               WHERE queue_name=? AND status='RUNNING' AND lease_expires_at<?
                 AND attempts>=max_attempts""",
            (timestamp, self.queue_name, timestamp),
        )
        self.db.execute(
            """UPDATE task_queue SET status='QUEUED', lease_owner=NULL,
                      lease_expires_at=NULL, available_at=?, updated_at=?
               WHERE queue_name=? AND status='RUNNING' AND lease_expires_at<?
                 AND attempts<max_attempts""",
            (timestamp, timestamp, self.queue_name, timestamp),
        )
        expires_at = (now + timedelta(seconds=self.lease_seconds)).isoformat()
        return self.db.fetch_one(
            """WITH candidate AS (
                 SELECT id FROM task_queue
                 WHERE queue_name=? AND status='QUEUED' AND available_at<=?
                 ORDER BY priority DESC, created_at
                 FOR UPDATE SKIP LOCKED LIMIT 1
               )
               UPDATE task_queue AS task
               SET status='RUNNING', attempts=attempts+1, lease_owner=?,
                   lease_expires_at=?, updated_at=?
               FROM candidate WHERE task.id=candidate.id
               RETURNING task.id, task.payload_json, task.dedupe_key, task.attempts""",
            (
                self.queue_name,
                timestamp,
                self.worker_id,
                expires_at,
                timestamp,
            ),
        )

    def task_done(self, *, failed: bool = False, error: str | None = None) -> None:
        delivery = self._delivery.get()
        if not delivery:
            return
        now = self.db.current_time().isoformat()
        self.db.execute(
            """UPDATE task_queue SET status=?, active_key=NULL, last_error=?,
                      lease_owner=NULL, lease_expires_at=NULL, updated_at=?
               WHERE id=? AND status='RUNNING' AND lease_owner=? AND attempts=?
                 AND lease_expires_at>?""",
            (
                "FAILED" if failed else "SUCCEEDED", (error or "")[:1000] or None,
                now, delivery.task_id, self.worker_id, delivery.generation, now,
            ),
        )
        self._delivery.set(None)

    def release(self, *, error: str = "worker stopped") -> None:
        delivery = self._delivery.get()
        if not delivery:
            return
        now = self.db.current_time().isoformat()
        self.db.execute(
            """UPDATE task_queue
               SET status=CASE WHEN attempts>=max_attempts THEN 'FAILED' ELSE 'QUEUED' END,
                   active_key=CASE WHEN attempts>=max_attempts THEN NULL ELSE active_key END,
                   lease_owner=NULL, lease_expires_at=NULL, last_error=?, available_at=?, updated_at=?
               WHERE id=? AND status='RUNNING' AND lease_owner=? AND attempts=?""",
            (error[:1000], now, now, delivery.task_id, self.worker_id, delivery.generation),
        )
        self._delivery.set(None)

    async def heartbeat(self) -> None:
        delivery = self._delivery.get()
        if not delivery:
            return
        now = self.db.current_time()
        expires_at = (now + timedelta(seconds=self.lease_seconds)).isoformat()
        updated = await asyncio.to_thread(
            self.db.execute_count,
            """UPDATE task_queue SET lease_expires_at=?, updated_at=?
               WHERE id=? AND status='RUNNING' AND lease_owner=? AND attempts=?
                 AND lease_expires_at>?""",
            (expires_at, now.isoformat(), delivery.task_id, self.worker_id, delivery.generation, now.isoformat()),
        )
        if updated != 1:
            raise LeaseLostError("Queue delivery lease expired or was replaced")

    def qsize(self) -> int:
        row = self.db.fetch_one(
            """SELECT COUNT(*) AS count FROM task_queue
               WHERE queue_name=? AND status='QUEUED'""",
            (self.queue_name,),
        )
        return int(row["count"] if row else 0)


def create_task_queue(db: Database, queue_name: str) -> TaskQueue:
    if getattr(db, "dialect", "sqlite") == "postgresql":
        return PostgresTaskQueue(
            db,
            queue_name,
            lease_seconds=int(os.getenv("DEEPAGENT_QUEUE_LEASE_SECONDS", "300")),
            poll_seconds=float(os.getenv("DEEPAGENT_QUEUE_POLL_SECONDS", "0.25")),
        )
    return InMemoryTaskQueue()
