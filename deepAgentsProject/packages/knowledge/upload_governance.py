"""Admission for retained document bytes, upload intents and ingestion workers.

Retained-byte accounting deliberately includes failed/expired uploads. Expiry
does not prove that an object (or an OSS historical version) has been deleted.
These are logical document quotas, not an assertion about provider-billed bytes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from packages.knowledge.errors import KnowledgeConflictError
from packages.runtime.admission import CapacityExceeded


@dataclass(frozen=True)
class UploadSettings:
    tenant_bytes: int = 10 * 1024**3
    project_bytes: int = 5 * 1024**3
    user_bytes: int = 1024**3
    tenant_pending: int = 1000
    project_pending: int = 200
    user_pending: int = 50
    global_running: int = 8
    tenant_running: int = 4
    project_running: int = 2
    user_running: int = 2
    metadata_per_process: int = 8
    pending_seconds: int = 86400
    grant_seconds: int = 900

    def __post_init__(self):
        for name, value in vars(self).items():
            maximum = 2**50 if name.endswith('_bytes') else 1_000_000
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError('Upload limits must be bounded nonnegative integers')
        if not 1 <= self.grant_seconds <= 900 or not self.grant_seconds <= self.pending_seconds <= 604800:
            raise ValueError('Upload grant/intent lifetimes are invalid')

    @classmethod
    def from_environment(cls):
        return cls(**{name: int(os.getenv('DEEPAGENT_UPLOAD_' + name.upper(), str(value)))
                      for name, value in vars(cls()).items()})


class UploadGovernance:
    def __init__(self, db, admission, settings=None):
        self.db, self.admission = db, admission
        self.settings = settings or UploadSettings.from_environment()

    def reserve(self, context, size):
        # Caller owns users -> tenant admission -> version lock order.
        self.admission.lock_tenant(context.tenant_id)
        now = self.db.current_time().isoformat()
        for scope in ('tenant', 'project', 'user'):
            clause = 'v.tenant_id=?'
            params = [context.tenant_id]
            if scope == 'project':
                clause += ' AND v.project_id=?'
                params.append(context.project_id)
            elif scope == 'user':
                clause += ' AND d.created_by=?'
                params.append(context.user_id)
            row = self.db.fetch_one(f"""SELECT
                COALESCE(SUM(CASE WHEN v.size_bytes>v.expected_size_bytes
                    THEN v.size_bytes ELSE v.expected_size_bytes END),0) AS bytes,
                COALESCE(SUM(CASE WHEN v.status='PENDING_UPLOAD' AND v.upload_expires_at>?
                    THEN 1 ELSE 0 END),0) AS pending
                FROM knowledge_document_versions v JOIN knowledge_documents d ON d.id=v.document_id
                WHERE {clause}""", (now, *params))
            if row['bytes'] + size > getattr(self.settings, scope + '_bytes'):
                raise CapacityExceeded(f'Knowledge {scope} retained-byte quota reached; deletion must be verified before quota is released')
            if row['pending'] >= getattr(self.settings, scope + '_pending'):
                raise CapacityExceeded(f'Knowledge {scope} pending-upload quota reached')
        return (self.db.current_time() + timedelta(seconds=self.settings.pending_seconds)).isoformat()

    def grant_lifetime(self, version):
        expires = version.get('upload_expires_at')
        remaining = int((datetime.fromisoformat(expires) - self.db.current_time()).total_seconds()) if expires else 0
        if remaining < 1:
            raise KnowledgeConflictError('Upload intent expired; create a new upload intent')
        return min(self.settings.grant_seconds, remaining)

    def running_available(self, job):
        # One cross-process lock serializes the global running-slot claim.
        # Stale RUNNING jobs still count until the durable reconciler fences them.
        if self.db.dialect == 'postgresql':
            self.db.fetch_one('SELECT pg_advisory_xact_lock(726593927603)')
        for scope in ('global', 'tenant', 'project', 'user'):
            clause, params = "status='RUNNING'", []
            if scope != 'global':
                clause += ' AND tenant_id=?'
                params.append(job['tenant_id'])
            if scope == 'project':
                clause += ' AND project_id=?'
                params.append(job['project_id'])
            elif scope == 'user':
                clause += ' AND requested_by=?'
                params.append(job['requested_by'])
            count = self.db.fetch_one('SELECT COUNT(*) AS n FROM knowledge_ingestion_jobs WHERE ' + clause, params)['n']
            if count >= getattr(self.settings, scope + '_running'):
                return False
        return True

    def expire_intents(self):
        # This transition never deletes objects or releases retained-byte quota.
        # Lock only versions; completion rechecks status under its version lock.
        with self.db.transaction():
            now = self.db.current_time().isoformat()
            candidates = self.db.fetch_all("""SELECT id,document_id FROM knowledge_document_versions
                WHERE status='PENDING_UPLOAD' AND upload_expires_at<=?
                ORDER BY upload_expires_at,id LIMIT 100""", (now,))
            for version in candidates:
                changed = self.db.execute_count("""UPDATE knowledge_document_versions SET status='EXPIRED',
                    error_code='UPLOAD_EXPIRED', error_message='Upload intent expired before completion'
                    WHERE id=? AND status='PENDING_UPLOAD' AND upload_expires_at<=?""", (version['id'], now))
                if changed:
                    self.db.execute("""UPDATE knowledge_documents SET status='EXPIRED',updated_at=?
                        WHERE id=? AND status='PENDING_UPLOAD' AND current_version_id IS NULL""", (now, version['document_id']))
