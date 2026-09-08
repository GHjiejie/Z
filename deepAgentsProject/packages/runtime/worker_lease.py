from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Dict

from packages.persistence import Database

logger = logging.getLogger(__name__)

class WorkerLease:
    def __init__(
        self,
        db: Database,
        worker_id: str,
        worker_type: str,
        metadata: Dict[str, Any],
        *,
        heartbeat_seconds: int = 15,
    ):
        self.db = db
        self.worker_id = worker_id
        self.worker_type = worker_type
        self.metadata = metadata
        self.heartbeat_seconds = heartbeat_seconds
        self.task: asyncio.Task | None = None
        self.consumers: tuple[asyncio.Task, ...] = ()

    async def start(self) -> None:
        now = self.db.current_time().isoformat()
        with self.db.transaction():
            self.db.execute("DELETE FROM worker_nodes WHERE id=?", (self.worker_id,))
            self.db.execute(
                """INSERT INTO worker_nodes
                   (id, worker_type, status, started_at, heartbeat_at, metadata_json)
                   VALUES (?, ?, 'ONLINE', ?, ?, ?)""",
                (
                    self.worker_id,
                    self.worker_type,
                    now,
                    now,
                    self.db.encode(self.metadata),
                ),
            )
        self.task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        now = self.db.current_time().isoformat()
        self.db.execute(
            """UPDATE worker_nodes SET status='OFFLINE', stopped_at=?, heartbeat_at=?
               WHERE id=?""",
            (now, now, self.worker_id),
        )

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            try:
                self.db.execute(
                    """UPDATE worker_nodes SET heartbeat_at=?, status=?
                       WHERE id=?""",
                    (self.db.current_time().isoformat(),
                     "DEGRADED" if any(task.done() for task in self.consumers) else "ONLINE",
                     self.worker_id),
                )
            except Exception:
                # Do not silently lose heartbeats forever after one transient
                # outage. Runtime/job leases independently fence executions.
                logger.exception("Worker heartbeat failed; retrying next interval")


def worker_summary(db: Database, stale_seconds: int = 45) -> Dict[str, Any]:
    cutoff = (db.current_time() - timedelta(seconds=stale_seconds)).isoformat()
    online = db.fetch_all(
        """SELECT worker_type, COUNT(*) AS count FROM worker_nodes
           WHERE status='ONLINE' AND heartbeat_at>=? GROUP BY worker_type""",
        (cutoff,),
    )
    by_type = {row["worker_type"]: int(row["count"]) for row in online}
    return {"online": sum(by_type.values()), "by_type": by_type, "cutoff": cutoff}
