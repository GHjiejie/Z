from __future__ import annotations

from packages.auth.resource_access import ResourceAccess, document_allowed, refresh_context
from packages.auth.permissions import Permission, authorize
from packages.billing.calls import embed as metered_embed

import asyncio
import hashlib
import json
import logging
import os
import math
import re
import secrets
import time
import threading
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from packages.domain.models import TenantContext, utc_now
from packages.content_security import ContentRejectedError, ContentScanError, ContentScanner, NoopContentScanner
from packages.knowledge.embedding import lexical_tokens
from packages.knowledge.errors import (
    KnowledgeConflictError,
    KnowledgeNotFoundError,
    KnowledgeValidationError,
)
from packages.knowledge.ingestion.isolated import IsolatedDocumentParser
from packages.knowledge.models import (
    KnowledgeBaseCreate,
    KnowledgeSearchFilters,
    KnowledgeSearchRequest,
    UploadComplete,
    UploadPrepare,
)
from packages.knowledge.ports import EmbeddingProvider, ObjectStorage
from packages.knowledge.presentation import EVENT_FIELDS, event_view, job_view, revision_view, version_view
from packages.knowledge.storage.object_keys import build_object_key
from packages.persistence import Database
from packages.persistence.pagination import authorized_page
from packages.persistence.fencing import IngestionWriteFence, LeaseLostError, execution_scope
from packages.coding.redaction import redact_text
from packages.runtime.task_queue import InMemoryTaskQueue, TaskQueue
from packages.runtime.worker_lease import WorkerLease
from packages.runtime.admission import TaskAdmission
from packages.knowledge.upload_governance import UploadGovernance


logger = logging.getLogger(__name__)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


DEFAULT_RETRIEVAL_PROFILE = {
    "profile_version": "hybrid-rrf-2.1",
    "strategy": "hybrid_rrf",
    "dense_candidates": 50,
    "lexical_candidates": 50,
    "fusion_k": 60,
    "rerank_candidates": 20,
    "default_top_k": 8,
    "max_chunks_per_document": 3,
    "min_dense_score": 0.05,
    "max_context_characters": 24_000,
    "routing_min_score": 0.04,
    "require_lexical_overlap": False,
}


class KnowledgeService:
    """Knowledge control plane, ingestion worker, and runtime retriever."""

    def __init__(
        self,
        db: Database,
        storage: ObjectStorage,
        embedding: EmbeddingProvider,
        queue: TaskQueue | None = None,
        content_scanner: ContentScanner | None = None,
    ):
        self.db = db
        self.admission = TaskAdmission(db)
        self.uploads = UploadGovernance(db, self.admission)
        self._metadata_slots = threading.BoundedSemaphore(self.uploads.settings.metadata_per_process)
        self.storage = storage
        self.embedding = embedding
        self.retrieval_profile = {
            **DEFAULT_RETRIEVAL_PROFILE,
            # The deterministic reference embedding is a lexical hash, not a
            # semantic model. Dense-only matches can therefore be collisions.
            "require_lexical_overlap": embedding.model_revision.startswith(
                "deepagent-hash-embedding-"
            ),
        }
        self.parser = IsolatedDocumentParser()
        self.queue = queue or InMemoryTaskQueue()
        self.content_scanner = content_scanner or NoopContentScanner()
        self.worker_id = f"knowledge_worker_{secrets.token_hex(4)}"
        self.worker_lease = WorkerLease(
            db, self.worker_id, "knowledge", {"queue": "knowledge-ingestion"}
        )
        self.task: Optional[asyncio.Task] = None
        self.reconcile_task: Optional[asyncio.Task] = None
        self.lease_seconds = max(3, int(os.getenv("DEEPAGENT_INGESTION_LEASE_SECONDS", "30")))

    async def start(self) -> None:
        # Fail before publishing a heartbeat or consuming durable jobs. API-only
        # processes do not start ingestion and need not host parser isolation.
        await self.parser.validate_runtime()
        await self.worker_lease.start()
        await self.reconcile()
        if self.task is None:
            self.task = asyncio.create_task(self._worker_loop())
            self.reconcile_task = asyncio.create_task(self._reconcile_loop())
            self.worker_lease.consumers = (self.task, self.reconcile_task)

    async def stop(self) -> None:
        for task in (self.reconcile_task, self.task):
            if task:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self.reconcile_task, self.task) if task), return_exceptions=True,
        )
        self.task = self.reconcile_task = None
        await self.worker_lease.stop()

    def _enqueue_in_transaction(self, job_id: str) -> bool:
        put = getattr(self.queue, "put_transactional", None)
        if put is None:
            return False
        job = self.db.fetch_one("SELECT attempts FROM knowledge_ingestion_jobs WHERE id=?", (job_id,))
        put(job_id, dedupe_key=f"{job_id}:{job['attempts'] + 1}")
        return True

    async def _enqueue(self, job_id: str) -> None:
        job = self.db.fetch_one("SELECT attempts FROM knowledge_ingestion_jobs WHERE id=?", (job_id,))
        await self.queue.put(job_id, dedupe_key=f"{job_id}:{job['attempts'] + 1}")

    async def reconcile(self) -> None:
        self.uploads.expire_intents()
        cutoff = (self.db.current_time() - timedelta(seconds=self.lease_seconds)).isoformat()
        stale = self.db.fetch_all(
            "SELECT id FROM knowledge_ingestion_jobs WHERE status='RUNNING' AND (heartbeat_at IS NULL OR heartbeat_at<=?)",
            (cutoff,),
        )
        for candidate in stale:
            with self.db.transaction():
                suffix = " FOR UPDATE" if self.db.dialect == "postgresql" else ""
                job = self.db.fetch_one("SELECT * FROM knowledge_ingestion_jobs WHERE id=?" + suffix, (candidate["id"],))
                if job["status"] != "RUNNING" or (job["heartbeat_at"] and job["heartbeat_at"] > cutoff):
                    continue
                now = self.db.current_time().isoformat()
                if job["attempts"] >= int(os.getenv("DEEPAGENT_INGESTION_RECOVERY_LIMIT", "3")):
                    self.db.execute(
                        """UPDATE knowledge_ingestion_jobs SET status='FAILED', stage='FAILED',
                           worker_id=NULL, lease_token=NULL, error_code='RECOVERY_EXHAUSTED',
                           error_message='Worker recovery limit exhausted; explicit retry required', updated_at=?
                           WHERE id=?""",
                        (now, job["id"]),
                    )
                    self.db.execute(
                        """UPDATE knowledge_document_versions SET status='FAILED', error_code='RECOVERY_EXHAUSTED'
                           WHERE id=? AND status<>'READY'""", (job["document_version_id"],),
                    )
                else:
                    self.db.execute(
                        """UPDATE knowledge_ingestion_jobs SET status='QUEUED', stage='QUEUED',
                           worker_id=NULL, lease_token=NULL, updated_at=? WHERE id=?""", (now, job["id"]),
                    )
                    self._enqueue_in_transaction(job["id"])
        for job in self.db.fetch_all("SELECT id FROM knowledge_ingestion_jobs WHERE status='QUEUED'"):
            await self._enqueue(job["id"])

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(2)
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Knowledge reconciliation failed; retrying on next interval")

    @contextmanager
    def _write_scope(self, context: TenantContext):
        with self.db.transaction():
            # Match account governance's users -> resource lock ordering. A
            # concurrent account/session revocation either wins before this
            # check or waits for the complete authorized write to commit.
            if self.db.dialect == "postgresql":
                self.db.fetch_one("SELECT id FROM users WHERE id=? FOR UPDATE", (context.user_id,))
            current = refresh_context(self.db, context)
            authorize(current, Permission.KNOWLEDGE_MANAGE)
            yield current

    def _request_identity(self, operation, payload, context, key):
        if key is not None and (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", key)):
            raise KnowledgeValidationError("Idempotency-Key must contain 1-200 ASCII letters, digits, dots, underscores, colons or hyphens, starting with a letter or digit")

        def digest(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        scope = "knowledge-write-v1:" + digest([operation, context.project_id, context.environment_id])
        request_hash = digest({"actor": context.user_id, "body": payload.model_dump()})
        return scope, request_hash

    def _request_replay(self, context, scope, request_hash, key):
        if key is None:
            return None
        if self.db.dialect == "postgresql":
            # The row may not yet exist. Serialize its unique identity across
            # processes rather than relying on SELECT FOR UPDATE on no rows.
            identity = json.dumps([context.tenant_id, scope, key], separators=(",", ":"))
            lock_key = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big", signed=True)
            self.db.fetch_one("SELECT pg_advisory_xact_lock(?)", (lock_key,))
        row = self.db.fetch_one(
            "SELECT response_json FROM idempotency_records WHERE tenant_id=? AND scope=? AND key=?",
            (context.tenant_id, scope, key),
        )
        if not row:
            return None
        try:
            record = json.loads(row["response_json"])
        except (TypeError, ValueError) as exc:
            raise KnowledgeConflictError("Stored idempotency record is invalid") from exc
        if (not isinstance(record, dict) or record.get("version") != 1
                or record.get("request_hash") != request_hash or not isinstance(record.get("resource_id"), str)):
            raise KnowledgeConflictError("Idempotency key was used for different content or principal")
        return record["resource_id"]

    def _record_request(self, context, scope, request_hash, key, resource_id):
        if key is not None:
            # Never persist signed URLs, provider headers or source contents.
            self.db.execute(
                """INSERT INTO idempotency_records(tenant_id,scope,key,response_json,created_at)
                   VALUES(?,?,?,?,?)""",
                (context.tenant_id, scope, key, self.db.encode({"version": 1,
                    "request_hash": request_hash, "resource_id": resource_id}), utc_now()),
            )

    def create_knowledge_base(
        self, payload: KnowledgeBaseCreate, context: TenantContext, idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        scope, request_hash = self._request_identity("create-base", payload, context, idempotency_key)
        with self._write_scope(context) as current:
            previous = self._request_replay(current, scope, request_hash, idempotency_key)
            if previous:
                return self.get_knowledge_base(previous, current)
            result = self._create_knowledge_base(payload, current)
            self._record_request(current, scope, request_hash, idempotency_key, result["id"])
            return result

    def _create_knowledge_base(
        self, payload: KnowledgeBaseCreate, context: TenantContext
    ) -> Dict[str, Any]:
        knowledge_base_id = _new_id("kb")
        now = utc_now()
        self.db.execute(
            """INSERT INTO knowledge_bases
               (id, tenant_id, project_id, name, description, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?)""",
            (
                knowledge_base_id,
                context.tenant_id,
                context.project_id,
                payload.name,
                payload.description,
                now,
                now,
            ),
        )
        self._append_event(
            context,
            "knowledge.base.created",
            {"knowledge_base_id": knowledge_base_id, "name": payload.name},
            knowledge_base_id=knowledge_base_id,
        )
        return self.get_knowledge_base(knowledge_base_id, context)

    def list_knowledge_bases(self, context: TenantContext) -> List[Dict[str, Any]]:
        context = refresh_context(self.db, context)
        authorize(context, Permission.KNOWLEDGE_READ)
        items = self.db.fetch_all(
            """SELECT * FROM knowledge_bases WHERE tenant_id=? AND project_id=?
               ORDER BY updated_at DESC""",
            (context.tenant_id, context.project_id),
        )
        for item in items:
            documents = self.list_documents(item["id"], context)
            item["document_count"] = len(documents)
            item["ready_document_count"] = sum(document["status"] == "READY" for document in documents)
        return items

    def get_knowledge_base(self, knowledge_base_id: str, context: TenantContext) -> Dict[str, Any]:
        item = self.db.fetch_one(
            """SELECT * FROM knowledge_bases
               WHERE id=? AND tenant_id=? AND project_id=?""",
            (knowledge_base_id, context.tenant_id, context.project_id),
        )
        if not item:
            raise KnowledgeNotFoundError("Knowledge base not found")
        item["documents"] = self.list_documents(knowledge_base_id, context)
        item["revisions"] = self.list_revisions(knowledge_base_id, context)
        return item

    def list_documents(self, knowledge_base_id: str, context: TenantContext) -> List[Dict[str, Any]]:
        context = refresh_context(self.db, context)
        authorize(context, Permission.KNOWLEDGE_READ)
        self._require_knowledge_base(knowledge_base_id, context)
        items = self.db.fetch_all(
            """SELECT d.*, v.content_type, v.size_bytes, v.content_sha256, v.canonical_uri,
                      v.status AS version_status, v.indexed_at
               FROM knowledge_documents d
               LEFT JOIN knowledge_document_versions v ON v.id=d.current_version_id
               WHERE d.knowledge_base_id=? AND d.tenant_id=? AND d.project_id=?
               ORDER BY d.updated_at DESC""",
            (knowledge_base_id, context.tenant_id, context.project_id),
        )
        return [item for item in items if self._document_allowed(item, context)]

    def list_revisions(self, knowledge_base_id: str, context: TenantContext) -> List[Dict[str, Any]]:
        context = refresh_context(self.db, context)
        authorize(context, Permission.KNOWLEDGE_READ)
        self._require_knowledge_base(knowledge_base_id, context)
        return [revision_view(row) for row in self.db.fetch_all(
            """SELECT * FROM knowledge_base_revisions
               WHERE knowledge_base_id=? AND tenant_id=? AND project_id=?
               ORDER BY revision_number DESC""",
            (knowledge_base_id, context.tenant_id, context.project_id),
        )]

    def prepare_upload(
        self, knowledge_base_id: str, payload: UploadPrepare, context: TenantContext,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        scope, request_hash = self._request_identity("prepare-upload:" + knowledge_base_id, payload, context, idempotency_key)
        with self._write_scope(context) as current:
            self._require_knowledge_base(knowledge_base_id, current)
            previous = self._request_replay(current, scope, request_hash, idempotency_key)
            if previous:
                return self._upload_preparation(previous, current)
            result = self._prepare_upload(knowledge_base_id, payload, current)
            self._record_request(current, scope, request_hash, idempotency_key, result["document_version_id"])
            return result

    def _prepare_upload(
        self, knowledge_base_id: str, payload: UploadPrepare, context: TenantContext,
    ) -> Dict[str, Any]:
        self._require_knowledge_base(knowledge_base_id, context)
        expires_at = self.uploads.reserve(context, payload.size_bytes)
        document_id = _new_id("doc")
        version_id = _new_id("docver")
        object_key = build_object_key(
            context.environment_id,
            context.tenant_id,
            context.project_id,
            knowledge_base_id,
            version_id,
            payload.filename,
        )
        now = utc_now()
        self.db.execute(
            """INSERT INTO knowledge_documents
               (id, tenant_id, project_id, knowledge_base_id, display_name, description,
                source_type, status, visibility, allowed_roles_json, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'upload', 'PENDING_UPLOAD', ?, ?, ?, ?, ?)""",
            (
                document_id,
                context.tenant_id,
                context.project_id,
                knowledge_base_id,
                payload.filename,
                payload.description,
                payload.visibility,
                self.db.encode(payload.allowed_roles),
                context.user_id,
                now,
                now,
            ),
        )
        self.db.execute(
            """INSERT INTO knowledge_document_versions
               (id, document_id, tenant_id, project_id, revision_number, storage_provider,
                bucket, region, object_key, canonical_uri, expected_sha256, content_type,
                expected_size_bytes, status, created_at, upload_expires_at)
               VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_UPLOAD', ?, ?)""",
            (
                version_id,
                document_id,
                context.tenant_id,
                context.project_id,
                self.storage.provider,
                self.storage.bucket,
                self.storage.region,
                object_key,
                self.storage.canonical_uri(object_key),
                payload.sha256.lower(),
                payload.content_type,
                payload.size_bytes,
                now,
                expires_at,
            ),
        )
        self._append_event(
            context,
            "knowledge.upload.prepared",
            {
                "document_id": document_id,
                "document_version_id": version_id,
                "storage_provider": self.storage.provider,
                "object_key_hash": hashlib.sha256(object_key.encode()).hexdigest(),
            },
            knowledge_base_id=knowledge_base_id,
            document_version_id=version_id,
        )
        return self._upload_preparation(version_id, context)

    def _upload_preparation(self, version_id: str, context: TenantContext) -> Dict[str, Any]:
        if self.db.dialect == "postgresql":
            self.db.fetch_one("""SELECT id FROM knowledge_document_versions
                WHERE id=? AND tenant_id=? AND project_id=? FOR UPDATE""",
                (version_id, context.tenant_id, context.project_id))
        version = self._require_version(version_id, context)
        # Do not replay stale document permissions or expose a signed grant from
        # another operation. Fresh grants are only for still-pending uploads.
        self.get_document(version["document_id"], context)
        upload = None
        if version["status"] == "PENDING_UPLOAD":
            authorization = self.storage.create_upload_authorization(
                version["object_key"], version["content_type"],
                expires_seconds=self.uploads.grant_lifetime(version),
                size_bytes=version['expected_size_bytes'],
            )
            if authorization.url.startswith("local://"):
                authorization.url = f"/api/v1/knowledge-document-versions/{version_id}/content"
            upload = {"method": authorization.method, "url": authorization.url,
                      "expires_at": authorization.expires_at, "required_headers": authorization.headers}
        return {
            "document_id": version["document_id"],
            "document_version_id": version_id,
            "status": version["status"],
            "storage": {
                "provider": version["storage_provider"],
                "bucket": version["bucket"],
                "region": version["region"],
                "canonical_uri": version["canonical_uri"],
            },
            "upload": upload,
        }

    def upload_content(
        self, version_id: str, content: bytes, content_type: str, context: TenantContext
    ) -> Dict[str, Any]:
        version = self._require_version(version_id, context)
        if self.storage.provider != "local":
            raise KnowledgeConflictError("Direct platform upload is only available for local storage")
        if version["status"] not in {"PENDING_UPLOAD", "UPLOADED"}:
            raise KnowledgeConflictError(f"Document version cannot be uploaded from {version['status']}")
        if version['status'] == 'PENDING_UPLOAD':
            self.uploads.grant_lifetime(version)
        if len(content) != version["expected_size_bytes"]:
            raise KnowledgeValidationError(
                f"Uploaded size {len(content)} does not match expected {version['expected_size_bytes']}"
            )
        expected_type = version["content_type"].split(";", 1)[0].strip().lower()
        actual_type = content_type.split(";", 1)[0].strip().lower()
        if actual_type != expected_type:
            raise KnowledgeValidationError(
                f"Uploaded content type {actual_type} does not match expected {expected_type}"
            )
        metadata = self.storage.put_content(version["object_key"], content, content_type)
        return {
            "status": "UPLOADED",
            "etag": metadata.etag,
            "size_bytes": metadata.size_bytes,
        }

    async def complete_upload(
        self, version_id: str, payload: UploadComplete, context: TenantContext
    ) -> Dict[str, Any]:
        # A 202 means validation was durably accepted, not that the file is safe.
        # Only bounded metadata IO happens here; bytes and scans belong to workers.
        request_hash = hashlib.sha256(self.db.encode({
            'actor': context.user_id, 'tenant': context.tenant_id,
            'project': context.project_id, 'environment': context.environment_id,
            'document_version': version_id,
        }).encode()).hexdigest()
        with self._write_scope(context) as context:
            version = self._require_version(version_id, context)
            self.get_document(version['document_id'], context)
            if version.get('upload_request_hash') and version['upload_request_hash'] != request_hash:
                raise KnowledgeConflictError('Upload completion was already bound to different content or principal')
            self._assert_completion_source(version, payload)
            existing_job = self.db.fetch_one(
                'SELECT * FROM knowledge_ingestion_jobs WHERE document_version_id=?', (version_id,))
            if existing_job and existing_job['status'] != 'FAILED':
                return self.get_ingestion_job(existing_job['id'], context)
            if version['status'] not in {'PENDING_UPLOAD', 'UPLOADED', 'FAILED'}:
                raise KnowledgeConflictError('Document version cannot be completed from ' + version['status'])
            if version['status'] == 'PENDING_UPLOAD':
                self.uploads.grant_lifetime(version)
            if (version.get('object_version_id') and payload.object_version_id
                    and version['object_version_id'] != payload.object_version_id):
                raise KnowledgeConflictError('A pinned object version cannot be replaced on retry')
            self.admission.ingestion(context, ignore_job_id=existing_job['id'] if existing_job else '')
        if not self._metadata_slots.acquire(blocking=False):
            from packages.runtime.admission import CapacityExceeded
            raise CapacityExceeded('Upload metadata concurrency reached; retry later')
        try:
            metadata = await self._owned_io(self.storage.head_object, version['object_key'],
                version.get('object_version_id') or payload.object_version_id)
        finally:
            self._metadata_slots.release()
        if metadata.bucket != version['bucket'] or metadata.object_key != version['object_key']:
            raise KnowledgeValidationError('Object metadata does not match the upload destination')
        if self.storage.provider != 'local' and (not metadata.version_id or metadata.version_id == 'null'):
            raise KnowledgeConflictError('Object storage versioning must be enabled before completing uploads')
        requested_version = version.get('object_version_id') or payload.object_version_id
        if requested_version and metadata.version_id != requested_version:
            raise KnowledgeValidationError('Object metadata does not match the requested fixed version')
        if metadata.size_bytes != version['expected_size_bytes']:
            raise KnowledgeValidationError('Stored object size does not match the upload declaration')
        if metadata.content_type.split(';', 1)[0].strip().lower() != version['content_type'].split(';', 1)[0].strip().lower():
            raise KnowledgeValidationError('Stored content type does not match the upload declaration')
        if payload.etag and (not metadata.etag or payload.etag.strip('"') != metadata.etag.strip('"')):
            raise KnowledgeValidationError('Object ETag does not match the upload completion request')
        with self._write_scope(context) as context:
            self.admission.lock_tenant(context.tenant_id)
            if self.db.dialect == "postgresql":
                self.db.fetch_one("SELECT id FROM knowledge_document_versions WHERE id=? FOR UPDATE", (version_id,))
            version = self._require_version(version_id, context)
            self.get_document(version['document_id'], context)
            if version.get('upload_request_hash') and version['upload_request_hash'] != request_hash:
                raise KnowledgeConflictError('Upload completion was already bound to different content or principal')
            self._assert_completion_source(version, payload)
            existing_job = self.db.fetch_one(
                "SELECT * FROM knowledge_ingestion_jobs WHERE document_version_id=?", (version_id,),
            )
            if existing_job and existing_job["status"] != "FAILED":
                return self.get_ingestion_job(existing_job["id"], context)
            if version["status"] not in {"PENDING_UPLOAD", "UPLOADED", "FAILED"}:
                raise KnowledgeConflictError("Document version changed during upload completion")
            if version['status'] == 'PENDING_UPLOAD':
                self.uploads.grant_lifetime(version)
            if (version.get('object_version_id') and payload.object_version_id
                    and version['object_version_id'] != payload.object_version_id):
                raise KnowledgeConflictError('A pinned object version cannot be replaced on retry')
            self.admission.ingestion(context, ignore_job_id=existing_job["id"] if existing_job else "")
            now = utc_now()
            self.db.execute(
                """UPDATE knowledge_document_versions
                   SET status='UPLOADED', object_version_id=COALESCE(object_version_id,?),
                       etag=COALESCE(etag,?), upload_request_hash=?, size_bytes=?, storage_class=?,
                       uploaded_at=?, error_code=NULL, error_message=NULL
                   WHERE id=?""",
                (
                    metadata.version_id,
                    metadata.etag,
                    request_hash,
                    metadata.size_bytes,
                    metadata.storage_class,
                    now,
                    version_id,
                ),
            )
            self.db.execute(
                "UPDATE knowledge_documents SET status='UPLOADED', updated_at=? WHERE id=?",
                (now, version["document_id"]),
            )
            job_id = existing_job["id"] if existing_job else _new_id("kjob")
            if existing_job:
                self.db.execute(
                    """UPDATE knowledge_ingestion_jobs SET status='QUEUED', stage='QUEUED',
                       worker_id=NULL, lease_token=NULL, error_code=NULL, error_message=NULL,
                       updated_at=? WHERE id=?""",
                    (now, job_id),
                )
            else:
                self.db.execute(
                    """INSERT OR IGNORE INTO knowledge_ingestion_jobs
                       (id, tenant_id, project_id, knowledge_base_id, document_version_id,
                        status, stage, attempts, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'QUEUED', 'QUEUED', 0, ?, ?)""",
                    (
                        job_id,
                        context.tenant_id,
                        context.project_id,
                        version["knowledge_base_id"],
                        version_id,
                        now,
                        now,
                    ),
                )
                actual_job = self.db.fetch_one(
                    "SELECT id FROM knowledge_ingestion_jobs WHERE document_version_id=?",
                    (version_id,),
                )
                job_id = actual_job["id"]
            from packages.operations.telemetry import persist_origin
            persist_origin(self.db, 'ingestion', job_id)
            self._set_ingestion_principal(job_id, context)
            self._append_event(
                context,
                "knowledge.ingestion.queued",
                {"job_id": job_id, "document_version_id": version_id},
                knowledge_base_id=version["knowledge_base_id"],
                document_version_id=version_id,
                ingestion_job_id=job_id,
            )
            enqueued = self._enqueue_in_transaction(job_id)
        if not enqueued:
            await self._enqueue(job_id)
        return self.get_ingestion_job(job_id, context)

    @staticmethod
    def _assert_completion_source(version, payload):
        # A replay may omit optional provider hints (e.g. browser response loss),
        # but may not replace a fixed source already accepted for this document.
        if ((version.get('object_version_id') and payload.object_version_id
                and version['object_version_id'] != payload.object_version_id)
                or (version.get('etag') and payload.etag
                    and version['etag'].strip('"') != payload.etag.strip('"'))):
            raise KnowledgeConflictError('Upload completion was already bound to different content or principal')

    async def retry_ingestion_job(self, job_id: str, context: TenantContext) -> Dict[str, Any]:
        with self._write_scope(context) as context:
            self.admission.lock_tenant(context.tenant_id)
            if self.db.dialect == "postgresql":
                self.db.fetch_one("SELECT id FROM knowledge_ingestion_jobs WHERE id=? FOR UPDATE", (job_id,))
            job = self.get_ingestion_job(job_id, context)
            version = self._require_version(job["document_version_id"], context)
            self.get_document(version["document_id"], context)
            if job["status"] != "FAILED":
                raise KnowledgeConflictError("Only failed ingestion jobs can be retried")
            self.admission.ingestion(context, ignore_job_id=job_id)
            now = utc_now()
            self.db.execute(
                """UPDATE knowledge_ingestion_jobs SET status='QUEUED', stage='QUEUED',
                   worker_id=NULL, lease_token=NULL, error_code=NULL, error_message=NULL,
                   updated_at=? WHERE id=?""",
                (now, job_id),
            )
            self.db.execute(
                """UPDATE knowledge_document_versions SET status='UPLOADED', error_code=NULL,
                   error_message=NULL WHERE id=?""",
                (job["document_version_id"],),
            )
            self._set_ingestion_principal(job_id, context)
            enqueued = self._enqueue_in_transaction(job_id)
        if not enqueued:
            await self._enqueue(job_id)
        return self.get_ingestion_job(job_id, context)

    def _set_ingestion_principal(self, job_id, context):
        self.db.execute("""UPDATE knowledge_ingestion_jobs SET requested_by=?,
            requested_environment_id=?,requested_roles_json=? WHERE id=?""",
            (context.user_id,context.environment_id,self.db.encode(context.roles),job_id))

    def _ingestion_principal(self, job):
        if not job.get("requested_by") or not job.get("requested_environment_id") or not job.get("requested_roles"):
            raise KnowledgeConflictError("Legacy ingestion requires an authorized retry to establish its billing principal")
        context = refresh_context(self.db, TenantContext(tenant_id=job["tenant_id"],project_id=job["project_id"],
            user_id=job["requested_by"],environment_id=job["requested_environment_id"],roles=job["requested_roles"]))
        authorize(context, Permission.KNOWLEDGE_MANAGE)
        version = self._require_version(job["document_version_id"], context)
        self.get_document(version["document_id"], context)
        return context

    def get_ingestion_job(self, job_id: str, context: TenantContext) -> Dict[str, Any]:
        context = refresh_context(self.db, context)
        authorize(context, Permission.KNOWLEDGE_READ)
        job = self.db.fetch_one(
            """SELECT * FROM knowledge_ingestion_jobs
               WHERE id=? AND tenant_id=? AND project_id=?""",
            (job_id, context.tenant_id, context.project_id),
        )
        if not job:
            raise KnowledgeNotFoundError("Knowledge ingestion job not found")
        version = self._require_version(job["document_version_id"], context)
        self.get_document(version["document_id"], context)
        return job_view(job)

    def list_events(self, context: TenantContext, *, limit=100, cursor=None):
        context = refresh_context(self.db, context)
        authorize(context, Permission.KNOWLEDGE_READ)

        def visible(event):
            current = refresh_context(self.db, context)
            authorize(current, Permission.KNOWLEDGE_READ)
            if event["type"] not in EVENT_FIELDS:
                return False
            if event["type"] == "knowledge.search.completed":
                # Historical unattributed searches are not guessed to belong to anyone.
                return event.get("actor_user_id") == current.user_id
            if event.get("document_version_id") or event.get("ingestion_job_id"):
                version_id = event.get("document_version_id")
                if event.get("ingestion_job_id"):
                    job = self.db.fetch_one("""SELECT document_version_id FROM knowledge_ingestion_jobs
                        WHERE id=? AND tenant_id=? AND project_id=?""",
                        (event["ingestion_job_id"], current.tenant_id, current.project_id))
                    if not job or (version_id and version_id != job["document_version_id"]):
                        return False
                    version_id = job["document_version_id"]
                document = self.db.fetch_one("""SELECT d.* FROM knowledge_documents d
                    JOIN knowledge_document_versions v ON v.document_id=d.id
                    WHERE d.tenant_id=? AND d.project_id=? AND v.id=?""",
                    (current.tenant_id, current.project_id, version_id))
                return bool(document and self._document_allowed(document, current))
            # Unknown/malformed unbound document events fail closed.
            return event["type"] == "knowledge.base.created" and bool(self.db.fetch_one(
                "SELECT id FROM knowledge_bases WHERE id=? AND tenant_id=? AND project_id=?",
                (event.get("knowledge_base_id"), current.tenant_id, current.project_id)))

        page = authorized_page(self.db, query="SELECT r.* FROM knowledge_events r WHERE r.tenant_id=? AND r.project_id=?",
            params=(context.tenant_id, context.project_id), alias="r", resource="knowledge-events",
            context=context, visible=visible, limit=limit, cursor=cursor)
        page["items"] = [event_view(row) for row in page["items"]]
        return page

    def get_document(self, document_id: str, context: TenantContext) -> Dict[str, Any]:
        context = refresh_context(self.db, context)
        authorize(context, Permission.KNOWLEDGE_READ)
        document = self.db.fetch_one(
            """SELECT * FROM knowledge_documents
               WHERE id=? AND tenant_id=? AND project_id=?""",
            (document_id, context.tenant_id, context.project_id),
        )
        if not document:
            raise KnowledgeNotFoundError("Knowledge document not found")
        if not self._document_allowed(document, context):
            raise KnowledgeNotFoundError("Knowledge document not found")
        document["versions"] = [version_view(row) for row in self.db.fetch_all(
            "SELECT * FROM knowledge_document_versions WHERE document_id=? ORDER BY revision_number DESC",
            (document_id,),
        )]
        return document

    def download_document(self, document_id: str, context: TenantContext) -> Dict[str, Any]:
        document = self.get_document(document_id, context)
        if not self._document_allowed(document, context):
            raise KnowledgeNotFoundError("Knowledge document not found")
        if not document.get("current_version_id"):
            raise KnowledgeConflictError("Knowledge document is not ready")
        return self.download_document_version(document["current_version_id"], context)

    def download_document_version(
        self, version_id: str, context: TenantContext
    ) -> Dict[str, Any]:
        version = self._require_version(version_id, context)
        document = self.db.fetch_one(
            """SELECT * FROM knowledge_documents
               WHERE id=? AND tenant_id=? AND project_id=?""",
            (version["document_id"], context.tenant_id, context.project_id),
        )
        if not document or not self._document_allowed(document, context):
            raise KnowledgeNotFoundError("Knowledge document not found")
        if version["status"] != "READY":
            raise KnowledgeConflictError("Document version has not passed ingestion and security checks")
        signed_url = self.storage.create_download_url(
            version["object_key"], version.get("object_version_id"), expires_seconds=300
        )
        return {
            "url": signed_url,
            "content": None
            if signed_url
            else self.storage.get_content(version["object_key"], version.get("object_version_id")),
            "content_type": version["content_type"],
            "filename": document["display_name"],
        }

    def search(
        self,
        payload: KnowledgeSearchRequest,
        context: TenantContext,
        *,
        run_id: Optional[str] = None,
        expected_bindings: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        context = refresh_context(self.db, context)
        started = time.perf_counter()
        revision_ids = list(dict.fromkeys(payload.revision_ids))
        if payload.knowledge_base_id:
            knowledge_base = self._require_knowledge_base(payload.knowledge_base_id, context)
            if knowledge_base.get("current_revision_id"):
                revision_ids.append(knowledge_base["current_revision_id"])
        revision_ids = list(dict.fromkeys(revision_ids))
        if not revision_ids:
            latency_ms = int((time.perf_counter() - started) * 1000)
            audit_id = self._record_search_audit(
                context, payload.query, [], [], latency_ms, run_id
            )
            self._append_event(
                context,
                "knowledge.search.completed",
                {"audit_id": audit_id, "result_count": 0, "latency_ms": latency_ms, "revision_count": 0},
            )
            return {
                "status": "insufficient_evidence",
                "hits": [],
                "revision_ids": [],
                "audit_id": audit_id,
                "latency_ms": latency_ms,
            }
        revisions = self._require_revisions(
            revision_ids, context, expected_bindings=expected_bindings
        )
        profiles = [revision["retrieval_profile"] for revision in revisions]
        profile = profiles[0]
        if any(candidate != profile for candidate in profiles[1:]):
            raise KnowledgeConflictError(
                "Knowledge revisions use incompatible retrieval profiles"
            )
        rows = self._candidate_rows(revision_ids, payload.filters, context)
        rows = [row for row in rows if self._document_allowed(row, context)]
        query_vector = metered_embed(self.db, self.embedding, [payload.query], context,
            purpose="query_embedding", resource_id=run_id or revision_ids[0])[0]
        query_terms = set(lexical_tokens(payload.query))
        scored: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            if row["id"] in scored:
                continue
            embedding = row.get("embedding") or []
            dense_score = self._cosine(query_vector, embedding)
            chunk_terms = set(lexical_tokens(row["text"]))
            lexical_score = (
                len(query_terms.intersection(chunk_terms)) / len(query_terms) if query_terms else 0.0
            )
            if payload.query.lower() in row["text"].lower():
                lexical_score += 0.5
            scored[row["id"]] = {
                "row": row,
                "dense": dense_score,
                "lexical": lexical_score,
            }
        dense_ranked = sorted(scored.values(), key=lambda item: item["dense"], reverse=True)[
            : int(profile["dense_candidates"])
        ]
        lexical_ranked = sorted(
            scored.values(), key=lambda item: item["lexical"], reverse=True
        )[: int(profile["lexical_candidates"])]
        fused: Dict[str, float] = defaultdict(float)
        fusion_k = int(profile["fusion_k"])
        for rank, item in enumerate(dense_ranked, start=1):
            fused[item["row"]["id"]] += 1.0 / (fusion_k + rank)
        for rank, item in enumerate(lexical_ranked, start=1):
            fused[item["row"]["id"]] += 1.0 / (fusion_k + rank)
        ranked = sorted(
            scored.values(),
            key=lambda item: fused[item["row"]["id"]]
            + max(0.0, item["dense"]) * 0.15
            + item["lexical"] * 0.1,
            reverse=True,
        )[: int(profile["rerank_candidates"])]
        hits = []
        per_document: Dict[str, int] = defaultdict(int)
        min_dense_score = float(profile["min_dense_score"])
        max_chunks_per_document = int(profile["max_chunks_per_document"])
        for item in ranked:
            row = item["row"]
            if item["lexical"] <= 0 and item["dense"] <= min_dense_score:
                continue
            if profile.get("require_lexical_overlap") and item["lexical"] <= 0:
                continue
            if per_document[row["document_id"]] >= max_chunks_per_document:
                continue
            per_document[row["document_id"]] += 1
            score = (
                fused[row["id"]] + max(0.0, item["dense"]) * 0.15 + item["lexical"] * 0.1
            )
            hits.append(
                {
                    "citation_id": f"cite_{len(hits) + 1:02d}",
                    "chunk_id": row["id"],
                    "document_id": row["document_id"],
                    "document_version_id": row["document_version_id"],
                    "text": row["text"],
                    "score": round(score, 6),
                    "source": {
                        "title": row["display_name"],
                        "content_type": row["content_type"],
                        "locator": row.get("locator") or {},
                        "page": (row.get("locator") or {}).get("page"),
                        "section": (row.get("locator") or {}).get("section"),
                        "content_hash": row["content_hash"],
                        "canonical_uri": row["canonical_uri"],
                        "download_url": (
                            "/api/v1/knowledge-document-versions/"
                            f"{row['document_version_id']}/download"
                        ),
                    },
                }
            )
            if len(hits) >= payload.top_k:
                break
        latency_ms = int((time.perf_counter() - started) * 1000)
        audit_id = self._record_search_audit(
            context, payload.query, revision_ids, hits, latency_ms, run_id
        )
        self._append_event(
            context,
            "knowledge.search.completed",
            {
                "audit_id": audit_id,
                "result_count": len(hits),
                "latency_ms": latency_ms,
                "revision_count": len(revisions),
            },
        )
        return {
            "status": "ok" if hits else "insufficient_evidence",
            "hits": hits,
            "revision_ids": revision_ids,
            "audit_id": audit_id,
            "latency_ms": latency_ms,
        }

    async def _worker_loop(self) -> None:
        while True:
            try:
                job_id = await self.queue.get()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Knowledge queue claim failed")
                await asyncio.sleep(1)
                continue
            retry = False
            failed = False
            error = None
            try:
                await self._process_job(job_id)
                job = self.db.fetch_one("SELECT status, error_message FROM knowledge_ingestion_jobs WHERE id=?", (job_id,))
                failed = bool(job and job["status"] == "FAILED")
                error = job.get("error_message") if failed else None
            except asyncio.CancelledError:
                retry = True
                raise
            except Exception as exc:
                retry = True
                error = redact_text(str(exc))[:1000]
                logger.exception("Knowledge execution interrupted; lease reconciliation will recover it")
            finally:
                try:
                    if retry:
                        self.queue.release(error=error or "Worker stopped")
                    else:
                        self.queue.task_done(failed=failed, error=error)
                except Exception:
                    logger.exception("Could not settle knowledge queue delivery")

    async def _heartbeat(self, fence: IngestionWriteFence) -> None:
        while True:
            with execution_scope(fence), self.db.transaction():
                self.db.execute(
                    "UPDATE knowledge_ingestion_jobs SET heartbeat_at=? WHERE id=?",
                    (self.db.current_time().isoformat(), fence.job_id),
                )
            await self.queue.heartbeat()
            await asyncio.sleep(min(10, self.lease_seconds / 3))

    @staticmethod
    async def _owned_io(function, *args, **kwargs):
        # Cancelling asyncio.to_thread does not stop its native thread. Retain
        # ownership until it really ends, so a stopped consumer cannot immediately
        # reuse its slot while an old download/scan still occupies memory/sockets.
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
            raise

    async def _process_job(self, job_id: str) -> None:
        lease_token = _new_id("lease")
        if not self._claim_job(job_id, lease_token):
            return
        fence = IngestionWriteFence(job_id, self.worker_id, lease_token, self.lease_seconds)

        async def invoke():
            from packages.operations.telemetry import task_operation
            with task_operation(self.db, 'ingestion', job_id, 'knowledge.ingestion') as span, execution_scope(fence):
                try:
                    await self._ingest_job(job_id, lease_token)
                except (asyncio.CancelledError, LeaseLostError):
                    raise
                except Exception as exc:
                    if span is not None:
                        from opentelemetry.trace import StatusCode
                        span.set_status(StatusCode.ERROR)
                    self._fail_job(job_id, exc)

        execution = asyncio.create_task(invoke())
        monitor = asyncio.create_task(self._heartbeat(fence))
        try:
            done, _ = await asyncio.wait((execution, monitor), return_when=asyncio.FIRST_COMPLETED)
            if monitor in done:
                await monitor
            await execution
        finally:
            if not execution.done():
                execution.cancel()
            # Keep heartbeating during owned-IO drain on ordinary shutdown. If
            # authority was lost, all subsequent writes remain fenced out.
            await asyncio.gather(execution, return_exceptions=True)
            monitor.cancel()
            try:
                self.db.execute(
                    """UPDATE knowledge_ingestion_jobs SET heartbeat_at=?
                       WHERE id=? AND status='RUNNING' AND worker_id=? AND lease_token=?""",
                    ((self.db.current_time() - timedelta(seconds=self.lease_seconds + 1)).isoformat(),
                     job_id, self.worker_id, lease_token),
                )
            finally:
                await asyncio.gather(monitor, return_exceptions=True)

    async def _ingest_job(self, job_id: str, lease_token: str) -> None:
        job = self.db.fetch_one("SELECT * FROM knowledge_ingestion_jobs WHERE id=?", (job_id,))
        if not job:
            return
        billing_context = self._ingestion_principal(job)
        version = self.db.fetch_one(
            """SELECT v.*, d.display_name, d.id AS document_id, d.knowledge_base_id
               FROM knowledge_document_versions v
               JOIN knowledge_documents d ON d.id=v.document_id WHERE v.id=?""",
            (job["document_version_id"],),
        )
        if not version:
            raise KnowledgeNotFoundError("Document version for ingestion job is missing")
        context = TenantContext(
            tenant_id=job["tenant_id"],
            project_id=job["project_id"],
            user_id="knowledge_worker",
            roles=["system"],
        )
        self._append_event(
            context,
            "knowledge.ingestion.started",
            {"job_id": job_id, "worker_id": self.worker_id},
            knowledge_base_id=job["knowledge_base_id"],
            document_version_id=job["document_version_id"],
            ingestion_job_id=job_id,
        )
        content = await self._owned_io(
            self.storage.get_content,
            version["object_key"],
            version.get("object_version_id"),
        )
        digest = hashlib.sha256(content).hexdigest()
        if len(content) != version['expected_size_bytes']:
            raise KnowledgeValidationError('Stored document size does not match the upload declaration')
        if digest != version.get("expected_sha256"):
            raise KnowledgeValidationError("Document SHA-256 does not match the upload declaration")
        self._set_job_stage(job_id, "SECURITY_SCAN")
        from packages.operations.telemetry import operation
        with operation('knowledge.scan'):
            await self._owned_io(
                self.content_scanner.scan,
                content,
                object_name=f"knowledge/{version['id']}",
            )
        self._set_job_stage(job_id, "PARSING")
        with operation('knowledge.parse'):
            chunks = await self.parser.parse(content, version["content_type"], version["display_name"])
        if not chunks:
            raise KnowledgeValidationError("Document produced no indexable chunks")
        self._set_job_stage(job_id, "EMBEDDING")
        vectors: List[List[float]] = []
        for start in range(0, len(chunks), 64):
            billing_context = self._ingestion_principal(job)
            vectors.extend(
                await asyncio.to_thread(
                    metered_embed, self.db, self.embedding,
                    [chunk.text for chunk in chunks[start : start + 64]],
                    billing_context, purpose="document_embedding", resource_id=job_id,
                )
            )
        if len(vectors) != len(chunks):
            raise KnowledgeValidationError("Embedding provider returned an invalid result count")
        if any(len(vector) != self.embedding.dimensions for vector in vectors):
            raise KnowledgeValidationError(
                "Embedding provider returned an invalid vector dimension"
            )
        self._set_job_stage(job_id, "INDEXING")
        rows = []
        created_at = utc_now()
        for chunk, vector in zip(chunks, vectors):
            rows.append(
                (
                    _new_id("chk"),
                    job["tenant_id"],
                    job["project_id"],
                    job["knowledge_base_id"],
                    version["document_id"],
                    version["id"],
                    chunk.position,
                    chunk.text,
                    chunk.token_count,
                    chunk.content_hash,
                    self.db.encode(chunk.locator),
                    self.db.encode(vector),
                    created_at,
                )
            )
        with self.db.transaction():
            indexed_at = utc_now()
            revision = self._commit_ingestion(
                job,
                version,
                rows,
                digest,
                indexed_at,
                len(chunks),
                lease_token,
                context,
            )
            self._append_event(
                context,
                "knowledge.ingestion.completed",
                {
                    "job_id": job_id,
                    "chunk_count": len(chunks),
                    "knowledge_base_revision_id": revision["id"],
                    "index_hash": revision["index_hash"],
                },
                knowledge_base_id=job["knowledge_base_id"],
                document_version_id=job["document_version_id"],
                ingestion_job_id=job_id,
            )

    def _claim_job(self, job_id: str, lease_token: str) -> bool:
        with self.db.transaction():
            job = self.db.fetch_one('SELECT * FROM knowledge_ingestion_jobs WHERE id=?', (job_id,))
            if not job or job['status'] != 'QUEUED' or not self.uploads.running_available(job):
                return False
            return self._claim_job_unlocked(job_id, lease_token)

    def _claim_job_unlocked(self, job_id: str, lease_token: str) -> bool:
        now = self.db.current_time().isoformat()
        job = self.db.fetch_one("SELECT attempts FROM knowledge_ingestion_jobs WHERE id=?", (job_id,))
        if not job:
            return False
        key = getattr(self.queue, "delivery_key", None)
        if key and key != f"{job_id}:{job['attempts'] + 1}":
            return False
        return self.db.execute_count(
            """UPDATE knowledge_ingestion_jobs SET status='RUNNING', stage='DOWNLOADING',
               attempts=attempts+1, worker_id=?, lease_token=?, heartbeat_at=?, updated_at=?
               WHERE id=? AND status='QUEUED' AND attempts=?""",
            (self.worker_id, lease_token, now, now, job_id, job["attempts"]),
        ) == 1

    def _commit_ingestion(
        self,
        job: Dict[str, Any],
        version: Dict[str, Any],
        rows: List[tuple],
        digest: str,
        indexed_at: str,
        chunk_count: int,
        lease_token: str,
        context: TenantContext,
    ) -> Dict[str, Any]:
        with self.db.transaction() as connection:
            if self.db.dialect == "postgresql":
                connection.execute("SELECT id FROM knowledge_bases WHERE id=? FOR UPDATE", (job["knowledge_base_id"],))
            try:
                claimed = connection.execute(
                    """SELECT status, worker_id, lease_token FROM knowledge_ingestion_jobs
                       WHERE id=?""",
                    (job["id"],),
                ).fetchone()
                if (
                    not claimed
                    or claimed["status"] != "RUNNING"
                    or claimed["worker_id"] != self.worker_id
                    or claimed["lease_token"] != lease_token
                ):
                    raise KnowledgeConflictError("Ingestion job lease is no longer owned")
                published = connection.execute(
                    """SELECT 1 FROM knowledge_revision_documents
                       WHERE document_version_id=? LIMIT 1""",
                    (version["id"],),
                ).fetchone()
                if published:
                    raise KnowledgeConflictError(
                        "Published document versions cannot be re-indexed"
                    )
                connection.execute(
                    "DELETE FROM knowledge_chunks WHERE document_version_id=?",
                    (version["id"],),
                )
                connection.executemany(
                    """INSERT INTO knowledge_chunks
                       (id, tenant_id, project_id, knowledge_base_id, document_id,
                        document_version_id, position, text, token_count, content_hash,
                        locator_json, embedding_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    rows,
                )
                connection.execute(
                    """UPDATE knowledge_document_versions
                       SET status='READY', content_sha256=?, parser_version=?, chunker_version=?,
                           embedding_revision_id=?, indexed_at=?, error_code=NULL,
                           error_message=NULL WHERE id=?""",
                    (
                        digest,
                        self.parser.parser_version,
                        self.parser.chunker_version,
                        self.embedding.model_revision,
                        indexed_at,
                        version["id"],
                    ),
                )
                connection.execute(
                    """UPDATE knowledge_documents SET current_version_id=?, status='READY',
                       updated_at=? WHERE id=?""",
                    (version["id"], indexed_at, version["document_id"]),
                )
                revision = self._publish_revision_in_connection(
                    connection, job["knowledge_base_id"], context
                )
                completed = connection.execute(
                    """UPDATE knowledge_ingestion_jobs SET status='SUCCEEDED', stage='COMPLETED',
                       chunk_count=?, heartbeat_at=?, updated_at=?
                       WHERE id=? AND status='RUNNING' AND worker_id=? AND lease_token=?""",
                    (
                        chunk_count,
                        indexed_at,
                        indexed_at,
                        job["id"],
                        self.worker_id,
                        lease_token,
                    ),
                )
                if completed.rowcount != 1:
                    raise KnowledgeConflictError("Ingestion job lease was lost during commit")
                return revision
            except Exception:
                raise

    def _publish_revision(
        self, knowledge_base_id: str, context: TenantContext
    ) -> Dict[str, Any]:
        with self.db.transaction() as connection:
            try:
                revision = self._publish_revision_in_connection(
                    connection, knowledge_base_id, context
                )
                return revision
            except Exception:
                raise

    def _publish_revision_in_connection(
        self, connection: Any, knowledge_base_id: str, context: TenantContext
    ) -> Dict[str, Any]:
        if self.db.dialect == "postgresql":
            connection.execute("SELECT id FROM knowledge_bases WHERE id=? FOR UPDATE", (knowledge_base_id,))
        rows = connection.execute(
            """SELECT v.id, v.content_sha256, v.parser_version, v.chunker_version,
                      v.embedding_revision_id
               FROM knowledge_documents d
               JOIN knowledge_document_versions v ON v.id=d.current_version_id
               WHERE d.knowledge_base_id=? AND d.tenant_id=? AND d.project_id=?
                 AND d.status='READY' AND v.status='READY'
               ORDER BY d.id""",
            (knowledge_base_id, context.tenant_id, context.project_id),
        ).fetchall()
        document_versions = [self.db._decode(row) for row in rows]
        if not document_versions:
            raise KnowledgeConflictError("Cannot publish an empty knowledge base revision")
        manifest = self._build_index_manifest(document_versions)
        index_hash = self._calculate_index_hash(
            manifest,
            self.embedding.model_revision,
            self.embedding.dimensions,
            self.retrieval_profile,
        )
        existing_row = connection.execute(
            """SELECT * FROM knowledge_base_revisions
               WHERE knowledge_base_id=? AND index_hash=?""",
            (knowledge_base_id, index_hash),
        ).fetchone()
        if existing_row:
            return self.db._decode(existing_row)
        latest = connection.execute(
            """SELECT MAX(revision_number) AS value FROM knowledge_base_revisions
               WHERE knowledge_base_id=?""",
            (knowledge_base_id,),
        ).fetchone()
        revision_id = _new_id("kbrev")
        revision_number = (latest["value"] or 0) + 1
        now = utc_now()
        connection.execute(
            """UPDATE knowledge_base_revisions SET status='DEPRECATED', deprecated_at=?
               WHERE knowledge_base_id=? AND status='ACTIVE'""",
            (now, knowledge_base_id),
        )
        connection.execute(
            """INSERT INTO knowledge_base_revisions
               (id, knowledge_base_id, tenant_id, project_id, revision_number,
                status, manifest_json, retrieval_profile_json, embedding_model,
                embedding_dimensions, index_hash, created_at, activated_at)
               VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?)""",
            (
                revision_id,
                knowledge_base_id,
                context.tenant_id,
                context.project_id,
                revision_number,
                self.db.encode(manifest),
                self.db.encode(self.retrieval_profile),
                self.embedding.model_revision,
                self.embedding.dimensions,
                index_hash,
                now,
                now,
            ),
        )
        connection.executemany(
            """INSERT INTO knowledge_revision_documents
               (revision_id, document_version_id) VALUES (?, ?)""",
            [(revision_id, item["id"]) for item in document_versions],
        )
        connection.execute(
            "UPDATE knowledge_bases SET current_revision_id=?, updated_at=? WHERE id=?",
            (revision_id, now, knowledge_base_id),
        )
        row = connection.execute(
            "SELECT * FROM knowledge_base_revisions WHERE id=?", (revision_id,)
        ).fetchone()
        return self.db._decode(row)

    def _build_index_manifest(
        self, document_versions: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        documents = []
        for version in sorted(document_versions, key=lambda item: item["id"]):
            chunks = self.db.fetch_all(
                """SELECT position, text, content_hash, embedding_json
                   FROM knowledge_chunks WHERE document_version_id=? ORDER BY position""",
                (version["id"],),
            )
            for chunk in chunks:
                actual_content_hash = hashlib.sha256(
                    chunk["text"].encode("utf-8")
                ).hexdigest()
                if actual_content_hash != chunk["content_hash"]:
                    raise KnowledgeConflictError(
                        f"Chunk content integrity check failed for {version['id']}"
                    )
            documents.append(
                {
                    "document_version_id": version["id"],
                    "content_sha256": version["content_sha256"],
                    "parser_version": version["parser_version"],
                    "chunker_version": version["chunker_version"],
                    "embedding_revision_id": version["embedding_revision_id"],
                    "chunks": [
                        {
                            "position": chunk["position"],
                            "content_hash": chunk["content_hash"],
                            "embedding_hash": hashlib.sha256(
                                self.db.encode(chunk["embedding"]).encode("utf-8")
                            ).hexdigest(),
                        }
                        for chunk in chunks
                    ],
                }
            )
        return {"schema_version": 2, "documents": documents}

    @staticmethod
    def _calculate_index_hash(
        manifest: Dict[str, Any],
        embedding_model: str,
        embedding_dimensions: int,
        retrieval_profile: Dict[str, Any],
    ) -> str:
        canonical = json.dumps(
            {
                "manifest": manifest,
                "embedding_model": embedding_model,
                "embedding_dimensions": embedding_dimensions,
                "retrieval_profile": retrieval_profile,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _verify_revision_index(self, revision: Dict[str, Any]) -> None:
        manifest = revision.get("manifest") or {}
        if manifest.get("schema_version") != 2:
            raise KnowledgeConflictError(
                f"Knowledge revision {revision['id']} uses an unverifiable legacy index; re-index it"
            )
        versions = self.db.fetch_all(
            """SELECT v.id, v.content_sha256, v.parser_version, v.chunker_version,
                      v.embedding_revision_id
               FROM knowledge_document_versions v
               JOIN knowledge_revision_documents rd ON rd.document_version_id=v.id
               WHERE rd.revision_id=? ORDER BY v.id""",
            (revision["id"],),
        )
        actual_manifest = self._build_index_manifest(versions)
        actual_hash = self._calculate_index_hash(
            actual_manifest,
            revision["embedding_model"],
            revision["embedding_dimensions"],
            revision["retrieval_profile"],
        )
        if actual_manifest != manifest or actual_hash != revision["index_hash"]:
            raise KnowledgeConflictError(
                f"Knowledge revision index integrity check failed for {revision['id']}"
            )

    def _fail_job(self, job_id: str, exc: Exception) -> None:
        with self.db.transaction():
            job = self.db.fetch_one("SELECT * FROM knowledge_ingestion_jobs WHERE id=?", (job_id,))
            if not job or job["status"] != "RUNNING" or job.get("worker_id") != self.worker_id:
                return
            code = getattr(exc, "code", "KNOWLEDGE_INGESTION_FAILED")
            message = redact_text(str(exc))[:2000]
            now = utc_now()
            updated = self.db.execute_count(
                """UPDATE knowledge_ingestion_jobs SET status='FAILED', stage='FAILED',
                   error_code=?, error_message=?, heartbeat_at=?, updated_at=?
                   WHERE id=? AND status='RUNNING' AND worker_id=? AND lease_token=?""",
                (
                    code,
                    message,
                    now,
                    now,
                    job_id,
                    self.worker_id,
                    job.get("lease_token"),
                ),
            )
            if updated != 1:
                return
            self.db.execute(
                """UPDATE knowledge_document_versions SET status='FAILED', error_code=?,
                   error_message=? WHERE id=? AND NOT EXISTS (
                     SELECT 1 FROM knowledge_revision_documents rd
                     WHERE rd.document_version_id=knowledge_document_versions.id
                   )""",
                (code, message, job["document_version_id"]),
            )
            version = self.db.fetch_one(
                "SELECT document_id FROM knowledge_document_versions WHERE id=?",
                (job["document_version_id"],),
            )
            if version:
                self.db.execute(
                    "UPDATE knowledge_documents SET status='FAILED', updated_at=? WHERE id=? AND current_version_id IS NULL",
                    (now, version["document_id"]),
                )
            context = TenantContext(
                tenant_id=job["tenant_id"],
                project_id=job["project_id"],
                user_id="knowledge_worker",
                roles=["system"],
            )
            self._append_event(
                context,
                "knowledge.ingestion.failed",
                {"job_id": job_id, "code": code, "message": message},
                knowledge_base_id=job["knowledge_base_id"],
                document_version_id=job["document_version_id"],
                ingestion_job_id=job_id,
            )

    def _set_job_stage(self, job_id: str, stage: str) -> None:
        now = utc_now()
        self.db.execute(
            """UPDATE knowledge_ingestion_jobs SET stage=?, heartbeat_at=?, updated_at=?
               WHERE id=? AND status='RUNNING' AND worker_id=?""",
            (stage, now, now, job_id, self.worker_id),
        )

    def _candidate_rows(
        self,
        revision_ids: List[str],
        filters: KnowledgeSearchFilters,
        context: TenantContext,
    ) -> List[Dict[str, Any]]:
        placeholders = ",".join("?" for _ in revision_ids)
        clauses = [
            f"r.id IN ({placeholders})",
            "c.tenant_id=?",
            "c.project_id=?",
        ]
        params: List[Any] = [*revision_ids, context.tenant_id, context.project_id]
        if filters.document_ids:
            clauses.append(f"d.id IN ({','.join('?' for _ in filters.document_ids)})")
            params.extend(filters.document_ids)
        if filters.content_types:
            clauses.append(f"v.content_type IN ({','.join('?' for _ in filters.content_types)})")
            params.extend(filters.content_types)
        return self.db.fetch_all(
            f"""SELECT c.*, d.display_name, d.visibility, d.allowed_roles_json, d.created_by,
                       v.content_type, v.canonical_uri, r.id AS knowledge_revision_id
                FROM knowledge_chunks c
                JOIN knowledge_revision_documents rd ON rd.document_version_id=c.document_version_id
                JOIN knowledge_base_revisions r ON r.id=rd.revision_id
                JOIN knowledge_documents d ON d.id=c.document_id
                JOIN knowledge_document_versions v ON v.id=c.document_version_id
                WHERE {' AND '.join(clauses)}""",
            params,
        )

    def _require_revisions(
        self,
        revision_ids: Iterable[str],
        context: TenantContext,
        *,
        expected_bindings: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        ids = list(revision_ids)
        placeholders = ",".join("?" for _ in ids)
        revisions = self.db.fetch_all(
            f"""SELECT * FROM knowledge_base_revisions
                WHERE id IN ({placeholders}) AND tenant_id=? AND project_id=?
                  AND status IN ('ACTIVE','DEPRECATED')""",
            [*ids, context.tenant_id, context.project_id],
        )
        found = {revision["id"] for revision in revisions}
        missing = [revision_id for revision_id in ids if revision_id not in found]
        if missing:
            raise KnowledgeNotFoundError("One or more knowledge revisions are unavailable")
        if any(
            revision["embedding_dimensions"] != self.embedding.dimensions
            or revision["embedding_model"] != self.embedding.model_revision
            for revision in revisions
        ):
            raise KnowledgeConflictError(
                "Knowledge revision embedding model is unavailable in this runtime"
            )
        if expected_bindings is not None:
            expected = {binding["revision_id"]: binding for binding in expected_bindings}
            if set(expected) != set(ids):
                raise KnowledgeConflictError(
                    "Runtime knowledge bindings do not match requested revisions"
                )
            for revision in revisions:
                binding = expected[revision["id"]]
                if (
                    binding.get("index_hash") != revision["index_hash"]
                    or binding.get("embedding_model") != revision["embedding_model"]
                    or binding.get("embedding_dimensions")
                    != revision["embedding_dimensions"]
                    or binding.get("retrieval_profile")
                    != revision["retrieval_profile"]
                ):
                    raise KnowledgeConflictError(
                        f"Knowledge binding integrity check failed for {revision['id']}"
                    )
        for revision in revisions:
            self._verify_revision_index(revision)
        return revisions

    def _require_knowledge_base(
        self, knowledge_base_id: str, context: TenantContext
    ) -> Dict[str, Any]:
        item = self.db.fetch_one(
            """SELECT * FROM knowledge_bases
               WHERE id=? AND tenant_id=? AND project_id=?""",
            (knowledge_base_id, context.tenant_id, context.project_id),
        )
        if not item:
            raise KnowledgeNotFoundError("Knowledge base not found")
        return item

    def _require_version(self, version_id: str, context: TenantContext) -> Dict[str, Any]:
        version = self.db.fetch_one(
            """SELECT v.*, d.knowledge_base_id, d.display_name
               FROM knowledge_document_versions v
               JOIN knowledge_documents d ON d.id=v.document_id
               WHERE v.id=? AND v.tenant_id=? AND v.project_id=?""",
            (version_id, context.tenant_id, context.project_id),
        )
        if not version:
            raise KnowledgeNotFoundError("Knowledge document version not found")
        if version["storage_provider"] != self.storage.provider:
            raise KnowledgeConflictError("Document storage provider is not available in this runtime")
        return version

    @staticmethod
    def _document_allowed(document: Dict[str, Any], context: TenantContext) -> bool:
        return document_allowed(document, context)

    @staticmethod
    def _cosine(left: List[float], right: List[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if not left_norm or not right_norm:
            return 0.0
        return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)

    def _record_search_audit(
        self,
        context: TenantContext,
        query: str,
        revision_ids: List[str],
        hits: List[Dict[str, Any]],
        latency_ms: int,
        run_id: Optional[str],
    ) -> str:
        # Must commit before references leave the retrieval boundary, including
        # Coding tools, failed attempts and later turns in this same workspace.
        if run_id:
            ResourceAccess(self.db).acquire_sources(run_id, context, hits)
        audit_id = _new_id("kaudit")
        self.db.execute(
            """INSERT INTO knowledge_retrieval_audits
               (id, tenant_id, project_id, user_id, run_id, query_hash, revision_ids_json,
                result_count, latency_ms, hits_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                audit_id,
                context.tenant_id,
                context.project_id,
                context.user_id,
                run_id,
                hashlib.sha256(query.encode("utf-8")).hexdigest(),
                self.db.encode(revision_ids),
                len(hits),
                latency_ms,
                self.db.encode(
                    [{"chunk_id": hit["chunk_id"], "score": hit["score"]} for hit in hits]
                ),
                utc_now(),
            ),
        )
        return audit_id

    def _append_event(
        self,
        context: TenantContext,
        event_type: str,
        payload: Dict[str, Any],
        *,
        knowledge_base_id: Optional[str] = None,
        document_version_id: Optional[str] = None,
        ingestion_job_id: Optional[str] = None,
    ) -> None:
        self.db.execute(
            """INSERT INTO knowledge_events
               (id, tenant_id, project_id, knowledge_base_id, document_version_id,
                ingestion_job_id, type, payload_json, created_at, actor_user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _new_id("kevt"),
                context.tenant_id,
                context.project_id,
                knowledge_base_id,
                document_version_id,
                ingestion_job_id,
                event_type,
                self.db.encode(payload),
                utc_now(),
                context.user_id,
            ),
        )
