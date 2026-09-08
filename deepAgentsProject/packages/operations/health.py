from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime

from packages.domain.models import utc_now
from packages.runtime.worker_lease import worker_summary

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HealthSettings:
    interval: float = 5
    timeout: float = 2
    stale_after: float = 20
    runtime_queue_limit: int = 1000
    knowledge_queue_limit: int = 500
    runtime_wait_seconds: int = 300
    knowledge_wait_seconds: int = 900

    def __post_init__(self):
        if any(not math.isfinite(value) or value <= 0 for value in vars(self).values()):
            raise ValueError("Health thresholds must be finite and positive")
        if self.stale_after <= self.interval + self.timeout:
            raise ValueError("Health stale interval must exceed refresh interval plus timeout")

    @classmethod
    def from_environment(cls):
        defaults = cls()
        return cls(**{name: type(value)(os.getenv("DEEPAGENT_HEALTH_" + name.upper(), str(value)))
                      for name, value in vars(defaults).items()})


class HealthMonitor:
    """Single-flight dependency collection; probe HTTP requests never touch DB.

    Timeout cannot kill a Python DB thread, so a timed-out collection remains
    owned and blocks new collection submissions until it finishes. Its late
    result is discarded. SQL/pool timeouts bound the underlying DB operations.
    """

    def __init__(self, services, settings: HealthSettings | None = None):
        self.services = services
        self.settings = settings or HealthSettings.from_environment()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="readiness")
        self.task = None
        self.inflight = None
        self.discard_late = False
        self.stopping = False
        self.lock = asyncio.Lock()
        self.observed = 0.0
        self.data = self._unavailable("starting")

    @staticmethod
    def _unavailable(reason):
        return {"status": "unhealthy", "service": "platform-api",
                "checks": {"observation": reason}, "checked_at": utc_now()}

    def _store(self, data):
        if data["status"] != self.data["status"] or data["checks"] != self.data["checks"]:
            logger.warning("Platform readiness changed: %s", data["checks"])
        self.data, self.observed = data, time.monotonic()

    def collect(self):
        db = self.services.db
        if not db.fetch_one("SELECT 1 AS value"):
            raise RuntimeError("Database probe returned no row")
        now = db.current_time()
        workers = worker_summary(db)
        checks = {"database": "ok"}
        for kind in ("runtime", "knowledge"):
            checks[kind + "_workers"] = "ok" if workers["by_type"].get(kind, 0) > 0 else "unavailable"
        queues = {}
        queries = {
            "runtime": """SELECT COUNT(*) AS count, MIN(a.created_at) AS oldest
                FROM runs r JOIN run_attempts a ON a.id=r.current_attempt_id
                WHERE r.status IN ('CREATED','QUEUED','ORPHANED') AND a.status='PENDING'""",
            "knowledge": """SELECT COUNT(*) AS count, MIN(updated_at) AS oldest
                FROM knowledge_ingestion_jobs WHERE status='QUEUED'""",
        }
        for kind, sql in queries.items():
            row = db.fetch_one(sql)
            age = max(0, (now - datetime.fromisoformat(row["oldest"])).total_seconds()) if row["oldest"] else 0
            queues[kind] = {"depth": row["count"], "oldest_wait_seconds": round(age, 3)}
            checks[kind + "_queue"] = (
                "overloaded" if row["count"] >= getattr(self.settings, kind + "_queue_limit") else
                "stalled" if age >= getattr(self.settings, kind + "_wait_seconds") else "ok")
        cancellation = db.fetch_one("""SELECT COUNT(*) AS pending, MIN(COALESCE(c.created_at,r.updated_at)) AS oldest
            FROM runs r LEFT JOIN run_cancellations c ON c.run_id=r.id WHERE r.status='CANCELLING'""")
        cancellation_age = (max(0, (now - datetime.fromisoformat(cancellation['oldest'])).total_seconds())
                            if cancellation['oldest'] else 0)
        return {"status": "healthy" if all(value == "ok" for value in checks.values()) else "degraded",
                "service": "platform-api", "checks": checks, "checked_at": now.isoformat(),
                "cancellations": {"pending": cancellation['pending'], "oldest_seconds": cancellation_age},
                "workers": workers, "queues": queues, "queue_depth": queues["runtime"]["depth"]}

    async def refresh(self):
        async with self.lock:
            if self.stopping:
                return
            if self.inflight is not None and self.discard_late:
                if not self.inflight.done():
                    self._store(self._unavailable("probe_timeout"))
                    return
                # Retrieve the exception without retaining or publishing it.
                try:
                    self.inflight.result()
                except Exception:
                    pass
                self.inflight = None
                self.discard_late = False
            if self.inflight is None:
                self.inflight = asyncio.get_running_loop().run_in_executor(self.executor, self.collect)
            try:
                data = await asyncio.wait_for(asyncio.shield(self.inflight), self.settings.timeout)
            except TimeoutError:
                self.discard_late = True
                self._store(self._unavailable("probe_timeout"))
            except asyncio.CancelledError:
                raise
            except Exception:
                self.inflight = None
                self._store(self._unavailable("dependency_unavailable"))
            else:
                self.inflight = None
                self._store(data)

    async def start(self):
        await self.refresh()
        self.task = asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            await asyncio.sleep(self.settings.interval)
            await self.refresh()

    def snapshot(self, *, details=False):
        if self.stopping:
            return self._unavailable("stopping")
        if not self.observed or time.monotonic() - self.observed > self.settings.stale_after:
            return self._unavailable("stale_observation")
        data = deepcopy(self.data)
        # A local consumer failure must not be hidden behind its last heartbeat.
        for kind, service in (("runtime", self.services.orchestrator), ("knowledge", self.services.knowledge)):
            tasks = (service.task, service.reconcile_task)
            if any(task is not None and task.done() for task in tasks):
                data["checks"][kind + "_workers"] = "local_consumer_failed"
                data["status"] = "degraded"
        return data if details else {key: data[key] for key in ("status", "service", "checks", "checked_at")}

    async def stop(self):
        self.stopping = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.inflight:
            try:
                await asyncio.wait_for(asyncio.shield(self.inflight), self.settings.timeout)
            except (Exception, asyncio.CancelledError):
                # DB statement and pool timeouts still bound this owned thread.
                self.inflight.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
        self.executor.shutdown(wait=False, cancel_futures=True)
