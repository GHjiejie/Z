from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass


class CapacityExceeded(Exception):
    """Retryable admission refusal; no task or queue record has been committed."""


@dataclass(frozen=True)
class AdmissionSettings:
    runtime_tenant_active: int = 1000
    runtime_project_active: int = 200
    runtime_user_active: int = 100
    runtime_tenant_queued: int = 500
    runtime_project_queued: int = 100
    runtime_user_queued: int = 50
    knowledge_tenant_active: int = 200
    knowledge_project_active: int = 100
    knowledge_user_active: int = 20

    def __post_init__(self):
        if any(type(value) is not int or not 0 <= value <= 1_000_000 for value in vars(self).values()):
            raise ValueError("Admission limits must be integers between zero and one million")

    @classmethod
    def from_environment(cls):
        return cls(**{key: int(os.getenv("DEEPAGENT_ADMISSION_" + key.upper(), str(value)))
                      for key, value in vars(cls()).items()})


class TaskAdmission:
    """Counts outstanding work, not paid calls. Caller owns the write transaction.

    All external entries that can increase outstanding work share one tenant
    lock. Completions only lower usage and need no lock. Recovery of previously
    accepted nonterminal work keeps its existing active slot.
    """

    def __init__(self, db, settings: AdmissionSettings | None = None):
        self.db = db
        self.settings = settings or AdmissionSettings.from_environment()

    def lock_tenant(self, tenant_id):
        if self.db.dialect == "postgresql":
            if self.db._active_connection.get() is None:
                raise RuntimeError("Task admission requires a transaction")
            key = int.from_bytes(hashlib.sha256(("task-admission:" + tenant_id).encode()).digest()[:8], "big", signed=True)
            self.db.fetch_one("SELECT pg_advisory_xact_lock(?)", (key,))
        elif not getattr(self.db._transaction_state, "depth", 0):
            raise RuntimeError("Task admission requires a transaction")

    def _check(self, kind, rows, context, user_id, queued=False):
        for scope in ("tenant", "project", "user"):
            selected = [row for row in rows if scope == "tenant" or
                        (scope == "project" and row["project_id"] == context.project_id) or
                        (scope == "user" and row["user_id"] == user_id)]
            fields = ("active", "queued") if queued else ("active",)
            for field in fields:
                count = sum(row["count"] for row in selected if field == "active" or
                            row["status"] in {"CREATED", "QUEUED", "ORPHANED", "RESUMING"})
                if count >= getattr(self.settings, f"{kind}_{scope}_{field}"):
                    raise CapacityExceeded(f"{kind.title()} {scope} {field} capacity reached; retry after outstanding tasks finish")

    def run(self, context, *, ignore_run_id="", principal_user_id=None):
        self.lock_tenant(context.tenant_id)
        rows = self.db.fetch_all("""SELECT project_id,principal_user_id AS user_id,status,COUNT(*) AS count
            FROM runs WHERE tenant_id=? AND id<>?
            AND status NOT IN ('CANCELLED','TIMED_OUT','FAILED','FAILED_BUDGET','SUCCEEDED')
            GROUP BY project_id,principal_user_id,status""", (context.tenant_id, ignore_run_id))
        self._check("runtime", rows, context, principal_user_id or context.user_id, queued=True)

    def ingestion(self, context, *, ignore_job_id=""):
        self.lock_tenant(context.tenant_id)
        rows = self.db.fetch_all("""SELECT project_id,requested_by AS user_id,status,COUNT(*) AS count
            FROM knowledge_ingestion_jobs WHERE tenant_id=? AND id<>? AND status IN ('QUEUED','RUNNING')
            GROUP BY project_id,requested_by,status""", (context.tenant_id, ignore_job_id))
        self._check("knowledge", rows, context, context.user_id)
