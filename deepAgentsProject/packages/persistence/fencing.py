from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator


class LeaseLostError(RuntimeError):
    """The caller is no longer authorized to commit execution side effects."""


@dataclass(frozen=True)
class RunWriteFence:
    run_id: str
    attempt_id: str
    worker_id: str
    lease_token: str

    def validate(self, connection: Any, dialect: str) -> None:
        lock = " FOR UPDATE OF r, a" if dialect == "postgresql" else ""
        live = (
            "CAST(a.expires_at AS TIMESTAMPTZ) > clock_timestamp()"
            if dialect == "postgresql"
            else "a.expires_at > ?"
        )
        params = (
            (self.run_id,)
            if dialect == "postgresql"
            else (datetime.now(timezone.utc).isoformat(), self.run_id)
        )
        row = connection.execute(
            "SELECT r.current_attempt_id, a.worker_id, a.lease_token, "
            + live
            + " AS lease_live FROM runs r JOIN run_attempts a ON a.id=r.current_attempt_id "
            + "WHERE r.id=?"
            + lock,
            params,
        ).fetchone()
        if (
            row is None
            or row["current_attempt_id"] != self.attempt_id
            or row["worker_id"] != self.worker_id
            or row["lease_token"] != self.lease_token
            or not row["lease_live"]
        ):
            raise LeaseLostError("Run execution lease is no longer owned")


@dataclass(frozen=True)
class CancellationWriteFence:
    """Control-plane finalization authority, never an agent execution lease."""

    run_id: str
    attempt_id: str
    worker_id: str
    lease_token: str = field(repr=False)

    def validate(self, connection: Any, dialect: str) -> None:
        lock = " FOR UPDATE OF r, c, a" if dialect == "postgresql" else ""
        row = connection.execute(
            """SELECT c.*, r.status AS run_status, r.current_attempt_id,
                      r.coding_workspace_id AS current_workspace_id,
                      a.lease_token AS execution_token
               FROM run_cancellations c JOIN runs r ON r.id=c.run_id
               JOIN run_attempts a ON a.id=r.current_attempt_id
               WHERE r.id=?""" + lock, (self.run_id,),
        ).fetchone()
        now = (connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
               if dialect == "postgresql" else datetime.now(timezone.utc))
        if (not row or row["run_status"] != "CANCELLING" or row["status"] != "RUNNING"
                or row["attempt_id"] != self.attempt_id or row["current_attempt_id"] != self.attempt_id
                or row['current_workspace_id'] != row['workspace_id']
                or row["worker_id"] != self.worker_id or row["lease_token"] != self.lease_token
                or row["execution_token"] is not None or not row["expires_at"]
                or datetime.fromisoformat(row["expires_at"]) <= now):
            raise LeaseLostError("Cancellation finalization lease is no longer owned")
        if row["workspace_id"]:
            workspace = connection.execute(
                "SELECT workspace_generation, sandbox_instance_id FROM coding_workspaces WHERE id=?"
                + (" FOR UPDATE" if dialect == "postgresql" else ""), (row["workspace_id"],),
            ).fetchone()
            if (not workspace or workspace["workspace_generation"] != row["workspace_generation"]
                    or workspace["sandbox_instance_id"] != row["sandbox_instance_id"]):
                raise LeaseLostError("Cancellation workspace was replaced")


@dataclass(frozen=True)
class IngestionWriteFence:
    job_id: str
    worker_id: str
    lease_token: str
    lease_seconds: int

    def validate(self, connection: Any, dialect: str) -> None:
        lock = " FOR UPDATE" if dialect == "postgresql" else ""
        row = connection.execute(
            "SELECT worker_id, lease_token, heartbeat_at FROM knowledge_ingestion_jobs WHERE id=?" + lock,
            (self.job_id,),
        ).fetchone()
        now = (
            connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            if dialect == "postgresql" else datetime.now(timezone.utc)
        )
        if (
            not row or row["worker_id"] != self.worker_id or row["lease_token"] != self.lease_token
            or not row["heartbeat_at"]
            or datetime.fromisoformat(row["heartbeat_at"]) <= now - timedelta(seconds=self.lease_seconds)
        ):
            raise LeaseLostError("Knowledge ingestion lease is no longer owned")


_write_fence: contextvars.ContextVar[RunWriteFence | IngestionWriteFence | CancellationWriteFence | None] = contextvars.ContextVar(
    "deepagent_execution_write_fence", default=None
)


def current_write_fence() -> RunWriteFence | IngestionWriteFence | CancellationWriteFence | None:
    return _write_fence.get()


@contextmanager
def execution_scope(fence: RunWriteFence | IngestionWriteFence | CancellationWriteFence) -> Iterator[None]:
    token = _write_fence.set(fence)
    try:
        yield
    finally:
        _write_fence.reset(token)


def validate_write_fence(connection: Any, dialect: str) -> None:
    fence = current_write_fence()
    if fence is not None:
        fence.validate(connection, dialect)
