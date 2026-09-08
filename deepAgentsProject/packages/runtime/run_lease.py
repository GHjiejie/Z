from __future__ import annotations

import secrets
import os
from datetime import timedelta
from typing import Any

from packages.persistence import Database
from packages.persistence.fencing import LeaseLostError, RunWriteFence


CLAIMABLE_RUN_STATUSES = {"CREATED", "QUEUED", "ORPHANED"}
EXECUTING_RUN_STATUSES = {"CREATED", "QUEUED", "ORPHANED", "PREPARING", "RUNNING", "RESUMING"}


def finalize_cancellation(db: Database, events: Any, run_id: str) -> None:
    with db.transaction():
        suffix = " FOR UPDATE" if db.dialect == "postgresql" else ""
        run = db.fetch_one("SELECT * FROM runs WHERE id=?" + suffix, (run_id,))
        if not run or run["status"] != "CANCELLING":
            return
        if run.get('coding_workspace_id'):
            finalization = db.fetch_one('SELECT status FROM run_cancellations WHERE run_id=?', (run_id,))
            if not finalization or finalization['status'] != 'COMPLETED':
                return
        now = db.current_time().isoformat()
        db.execute("UPDATE runs SET status='CANCELLED', version=version+1, updated_at=? WHERE id=?", (now, run_id))
        db.execute(
            "UPDATE run_attempts SET status='CANCELLED', lease_token=NULL, expires_at=NULL, updated_at=? WHERE id=?",
            (now, run["current_attempt_id"]),
        )
        events.append(run_id, "graph.cancelled", {
            "graph_id": run["current_attempt_id"], "status": "cancelled",
        }, span_id="span_main")
        events.append(run_id, "run.cancelled", {})


class RunLeaseManager:
    def __init__(self, db: Database, worker_id: str, *, lease_seconds: int = 30):
        self.db = db
        self.worker_id = worker_id
        self.lease_seconds = max(3, lease_seconds)

    def locked_run(self, run_id: str) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if self.db.dialect == "postgresql" else ""
        return self.db.fetch_one("SELECT * FROM runs WHERE id=?" + suffix, (run_id,))

    def claim(self, run_id: str, expected_attempt_id: str | None = None) -> RunWriteFence | None:
        with self.db.transaction():
            run = self.locked_run(run_id)
            if not run or run["status"] not in CLAIMABLE_RUN_STATUSES:
                return None
            if expected_attempt_id and run["current_attempt_id"] != expected_attempt_id:
                return None
            now = self.db.current_time()
            token = f"lease_{secrets.token_hex(24)}"
            updated = self.db.execute_count(
                """UPDATE run_attempts SET status='RUNNING', worker_id=?, lease_token=?,
                   acquired_at=?, heartbeat_at=?, expires_at=?, updated_at=?
                   WHERE id=? AND status='PENDING' AND lease_token IS NULL""",
                (
                    self.worker_id, token, now.isoformat(), now.isoformat(),
                    (now + timedelta(seconds=self.lease_seconds)).isoformat(),
                    now.isoformat(), run["current_attempt_id"],
                ),
            )
            if updated != 1:
                return None
            return RunWriteFence(run_id, run["current_attempt_id"], self.worker_id, token)

    def heartbeat(self, fence: RunWriteFence) -> None:
        with self.db.transaction():
            run = self.locked_run(fence.run_id)
            if not run or run["current_attempt_id"] != fence.attempt_id:
                raise LeaseLostError("Run attempt was superseded")
            now = self.db.current_time()
            updated = self.db.execute_count(
                """UPDATE run_attempts SET heartbeat_at=?, expires_at=?, updated_at=?
                   WHERE id=? AND worker_id=? AND lease_token=? AND expires_at>?
                     AND status IN ('RUNNING','SUCCEEDED','FAILED')""",
                (
                    now.isoformat(), (now + timedelta(seconds=self.lease_seconds)).isoformat(),
                    now.isoformat(), fence.attempt_id, fence.worker_id, fence.lease_token,
                    now.isoformat(),
                ),
            )
            if updated != 1:
                raise LeaseLostError("Run lease expired or was revoked")

    def release(self, fence: RunWriteFence) -> None:
        self.db.execute(
            """UPDATE run_attempts SET lease_token=NULL, expires_at=NULL
               WHERE id=? AND worker_id=? AND lease_token=?""",
            (fence.attempt_id, fence.worker_id, fence.lease_token),
        )

    def abandon(self, fence: RunWriteFence) -> None:
        """Revoke this owner's lease immediately; the reconciler creates a fresh attempt."""
        self.db.execute(
            """UPDATE run_attempts SET expires_at=?, updated_at=?
               WHERE id=? AND worker_id=? AND lease_token=? AND status='RUNNING'""",
            (
                self.db.current_time().isoformat(), self.db.current_time().isoformat(),
                fence.attempt_id, fence.worker_id, fence.lease_token,
            ),
        )

    def recover(self, run_id: str) -> tuple[dict[str, Any], int] | None:
        with self.db.transaction():
            run = self.locked_run(run_id)
            if not run or run["status"] not in EXECUTING_RUN_STATUSES:
                return None
            attempt = self.db.fetch_one(
                "SELECT * FROM run_attempts WHERE id=?", (run["current_attempt_id"],)
            )
            now = self.db.current_time().isoformat()
            if not attempt or attempt["status"] != "RUNNING":
                return None
            if attempt.get("expires_at") and attempt["expires_at"] > now:
                return None
            prior_recoveries = self.db.fetch_one(
                "SELECT COUNT(*) AS count FROM run_attempts WHERE run_id=? AND status='ORPHANED'", (run_id,)
            )["count"]
            if prior_recoveries >= int(os.getenv("DEEPAGENT_RUN_RECOVERY_LIMIT", "3")):
                self.db.execute(
                    "UPDATE run_attempts SET status='FAILED', lease_token=NULL, expires_at=NULL, updated_at=? WHERE id=?",
                    (now, attempt["id"]),
                )
                self.db.execute(
                    "UPDATE runs SET status='FAILED', output=?, version=version+1, updated_at=? WHERE id=?",
                    ("Worker recovery limit exhausted; operator retry is required", now, run_id),
                )
                return self.db.fetch_one("SELECT * FROM runs WHERE id=?", (run_id,)), 0
            attempt_number = self.db.fetch_one(
                "SELECT MAX(attempt_number) AS number FROM run_attempts WHERE run_id=?", (run_id,)
            )["number"] + 1
            self.db.execute(
                """UPDATE run_attempts SET status='ORPHANED', lease_token=NULL,
                   expires_at=NULL, updated_at=? WHERE id=?""",
                (now, attempt["id"]),
            )
            attempt_id = f"att_{secrets.token_hex(16)}"
            self.db.execute(
                """INSERT INTO run_attempts
                   (id, run_id, attempt_number, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'PENDING', ?, ?)""",
                (attempt_id, run_id, attempt_number, now, now),
            )
            self.db.execute(
                """UPDATE runs SET status='ORPHANED', current_attempt_id=?,
                   version=version+1, updated_at=? WHERE id=?""",
                (attempt_id, now, run_id),
            )
            return self.db.fetch_one("SELECT * FROM runs WHERE id=?", (run_id,)), attempt_number
