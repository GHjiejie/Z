from __future__ import annotations

import asyncio
import hashlib
import json
import math
import secrets
import time
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional

from packages.domain.models import TenantContext, utc_now
from packages.knowledge.embedding import lexical_tokens
from packages.knowledge.errors import (
    KnowledgeConflictError,
    KnowledgeNotFoundError,
    KnowledgeValidationError,
)
from packages.knowledge.ingestion import DocumentParser, StructureAwareChunker
from packages.knowledge.models import (
    KnowledgeBaseCreate,
    KnowledgeSearchFilters,
    KnowledgeSearchRequest,
    UploadComplete,
    UploadPrepare,
)
from packages.knowledge.ports import EmbeddingProvider, ObjectStorage
from packages.knowledge.storage.object_keys import build_object_key
from packages.persistence import Database


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


DEFAULT_RETRIEVAL_PROFILE = {
    "strategy": "hybrid_rrf",
    "dense_candidates": 50,
    "lexical_candidates": 50,
    "fusion_k": 60,
    "rerank_candidates": 20,
    "default_top_k": 8,
    "max_chunks_per_document": 3,
}


class KnowledgeService:
    """Knowledge control plane, ingestion worker, and runtime retriever."""

    def __init__(
        self,
        db: Database,
        storage: ObjectStorage,
        embedding: EmbeddingProvider,
    ):
        self.db = db
        self.storage = storage
        self.embedding = embedding
        self.parser = DocumentParser()
        self.chunker = StructureAwareChunker()
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.worker_id = f"knowledge_worker_{secrets.token_hex(4)}"
        self.task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self._worker_loop())
        recoverable = self.db.fetch_all(
            "SELECT id FROM knowledge_ingestion_jobs WHERE status IN ('QUEUED','RUNNING')"
        )
        for job in recoverable:
            self.db.execute(
                """UPDATE knowledge_ingestion_jobs SET status='QUEUED', stage='QUEUED',
                   worker_id=NULL, lease_token=NULL, updated_at=? WHERE id=?""",
                (utc_now(), job["id"]),
            )
            await self.queue.put(job["id"])

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    def create_knowledge_base(
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
        items = self.db.fetch_all(
            """SELECT * FROM knowledge_bases WHERE tenant_id=? AND project_id=?
               ORDER BY updated_at DESC""",
            (context.tenant_id, context.project_id),
        )
        for item in items:
            item["document_count"] = self.db.fetch_one(
                """SELECT COUNT(*) AS count FROM knowledge_documents
                   WHERE knowledge_base_id=? AND tenant_id=? AND project_id=?""",
                (item["id"], context.tenant_id, context.project_id),
            )["count"]
            item["ready_document_count"] = self.db.fetch_one(
                """SELECT COUNT(*) AS count FROM knowledge_documents
                   WHERE knowledge_base_id=? AND tenant_id=? AND project_id=? AND status='READY'""",
                (item["id"], context.tenant_id, context.project_id),
            )["count"]
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
        self._require_knowledge_base(knowledge_base_id, context)
        return self.db.fetch_all(
            """SELECT d.*, v.content_type, v.size_bytes, v.content_sha256, v.canonical_uri,
                      v.status AS version_status, v.indexed_at
               FROM knowledge_documents d
               LEFT JOIN knowledge_document_versions v ON v.id=d.current_version_id
               WHERE d.knowledge_base_id=? AND d.tenant_id=? AND d.project_id=?
               ORDER BY d.updated_at DESC""",
            (knowledge_base_id, context.tenant_id, context.project_id),
        )

    def list_revisions(self, knowledge_base_id: str, context: TenantContext) -> List[Dict[str, Any]]:
        self._require_knowledge_base(knowledge_base_id, context)
        return self.db.fetch_all(
            """SELECT * FROM knowledge_base_revisions
               WHERE knowledge_base_id=? AND tenant_id=? AND project_id=?
               ORDER BY revision_number DESC""",
            (knowledge_base_id, context.tenant_id, context.project_id),
        )

    def prepare_upload(
        self, knowledge_base_id: str, payload: UploadPrepare, context: TenantContext
    ) -> Dict[str, Any]:
        self._require_knowledge_base(knowledge_base_id, context)
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
        authorization = self.storage.create_upload_authorization(
            object_key, payload.content_type, expires_seconds=900
        )
        if authorization.url.startswith("local://"):
            authorization.url = f"/api/v1/knowledge-document-versions/{version_id}/content"
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
                expected_size_bytes, status, created_at)
               VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_UPLOAD', ?)""",
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
                payload.sha256.lower() if payload.sha256 else None,
                payload.content_type,
                payload.size_bytes,
                now,
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
        return {
            "document_id": document_id,
            "document_version_id": version_id,
            "storage": {
                "provider": self.storage.provider,
                "bucket": self.storage.bucket,
                "region": self.storage.region,
                "canonical_uri": self.storage.canonical_uri(object_key),
            },
            "upload": {
                "method": authorization.method,
                "url": authorization.url,
                "expires_at": authorization.expires_at,
                "required_headers": authorization.headers,
            },
        }

    def upload_content(
        self, version_id: str, content: bytes, content_type: str, context: TenantContext
    ) -> Dict[str, Any]:
        version = self._require_version(version_id, context)
        if self.storage.provider != "local":
            raise KnowledgeConflictError("Direct platform upload is only available for local storage")
        if version["status"] not in {"PENDING_UPLOAD", "UPLOADED"}:
            raise KnowledgeConflictError(f"Document version cannot be uploaded from {version['status']}")
        if len(content) != version["expected_size_bytes"]:
            raise KnowledgeValidationError(
                f"Uploaded size {len(content)} does not match expected {version['expected_size_bytes']}"
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
        version = self._require_version(version_id, context)
        existing_job = self.db.fetch_one(
            "SELECT * FROM knowledge_ingestion_jobs WHERE document_version_id=?", (version_id,)
        )
        if existing_job and existing_job["status"] != "FAILED":
            return existing_job
        metadata = self.storage.head_object(version["object_key"], payload.object_version_id)
        if metadata.size_bytes != version["expected_size_bytes"]:
            raise KnowledgeValidationError(
                f"OSS object size {metadata.size_bytes} does not match expected {version['expected_size_bytes']}"
            )
        if payload.etag and metadata.etag and payload.etag.strip('"') != metadata.etag.strip('"'):
            raise KnowledgeValidationError("OSS object ETag does not match the upload completion request")
        now = utc_now()
        self.db.execute(
            """UPDATE knowledge_document_versions
               SET status='UPLOADED', object_version_id=?, etag=?, content_type=?, size_bytes=?,
                   storage_class=?, uploaded_at=?, error_code=NULL, error_message=NULL
               WHERE id=?""",
            (
                metadata.version_id,
                metadata.etag,
                metadata.content_type or version["content_type"],
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
                   error_code=NULL, error_message=NULL, updated_at=? WHERE id=?""",
                (now, job_id),
            )
        else:
            self.db.execute(
                """INSERT INTO knowledge_ingestion_jobs
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
        self._append_event(
            context,
            "knowledge.ingestion.queued",
            {"job_id": job_id, "document_version_id": version_id},
            knowledge_base_id=version["knowledge_base_id"],
            document_version_id=version_id,
            ingestion_job_id=job_id,
        )
        await self.queue.put(job_id)
        return self.get_ingestion_job(job_id, context)

    async def retry_ingestion_job(self, job_id: str, context: TenantContext) -> Dict[str, Any]:
        job = self.get_ingestion_job(job_id, context)
        if job["status"] != "FAILED":
            raise KnowledgeConflictError("Only failed ingestion jobs can be retried")
        now = utc_now()
        self.db.execute(
            """UPDATE knowledge_ingestion_jobs SET status='QUEUED', stage='QUEUED',
               error_code=NULL, error_message=NULL, updated_at=? WHERE id=?""",
            (now, job_id),
        )
        self.db.execute(
            """UPDATE knowledge_document_versions SET status='UPLOADED', error_code=NULL,
               error_message=NULL WHERE id=?""",
            (job["document_version_id"],),
        )
        await self.queue.put(job_id)
        return self.get_ingestion_job(job_id, context)

    def get_ingestion_job(self, job_id: str, context: TenantContext) -> Dict[str, Any]:
        job = self.db.fetch_one(
            """SELECT * FROM knowledge_ingestion_jobs
               WHERE id=? AND tenant_id=? AND project_id=?""",
            (job_id, context.tenant_id, context.project_id),
        )
        if not job:
            raise KnowledgeNotFoundError("Knowledge ingestion job not found")
        return job

    def get_document(self, document_id: str, context: TenantContext) -> Dict[str, Any]:
        document = self.db.fetch_one(
            """SELECT * FROM knowledge_documents
               WHERE id=? AND tenant_id=? AND project_id=?""",
            (document_id, context.tenant_id, context.project_id),
        )
        if not document:
            raise KnowledgeNotFoundError("Knowledge document not found")
        document["versions"] = self.db.fetch_all(
            "SELECT * FROM knowledge_document_versions WHERE document_id=? ORDER BY revision_number DESC",
            (document_id,),
        )
        return document

    def download_document(self, document_id: str, context: TenantContext) -> Dict[str, Any]:
        document = self.get_document(document_id, context)
        if not self._document_allowed(document, context):
            raise KnowledgeNotFoundError("Knowledge document not found")
        if not document.get("current_version_id"):
            raise KnowledgeConflictError("Knowledge document is not ready")
        version = self._require_version(document["current_version_id"], context)
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
    ) -> Dict[str, Any]:
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
        revisions = self._require_revisions(revision_ids, context)
        rows = self._candidate_rows(revision_ids, payload.filters, context)
        rows = [row for row in rows if self._document_allowed(row, context)]
        query_vector = self.embedding.embed_query(payload.query)
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
        dense_ranked = sorted(scored.values(), key=lambda item: item["dense"], reverse=True)[:50]
        lexical_ranked = sorted(scored.values(), key=lambda item: item["lexical"], reverse=True)[:50]
        fused: Dict[str, float] = defaultdict(float)
        for rank, item in enumerate(dense_ranked, start=1):
            fused[item["row"]["id"]] += 1.0 / (60 + rank)
        for rank, item in enumerate(lexical_ranked, start=1):
            fused[item["row"]["id"]] += 1.0 / (60 + rank)
        ranked = sorted(
            scored.values(),
            key=lambda item: fused[item["row"]["id"]]
            + max(0.0, item["dense"]) * 0.15
            + item["lexical"] * 0.1,
            reverse=True,
        )
        hits = []
        per_document: Dict[str, int] = defaultdict(int)
        for item in ranked:
            row = item["row"]
            if item["lexical"] <= 0 and item["dense"] <= 0.05:
                continue
            if per_document[row["document_id"]] >= 3:
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
                        "download_url": f"/api/v1/knowledge-documents/{row['document_id']}/download",
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
            job_id = await self.queue.get()
            try:
                await self._process_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._fail_job(job_id, exc)
            finally:
                self.queue.task_done()

    async def _process_job(self, job_id: str) -> None:
        job = self.db.fetch_one("SELECT * FROM knowledge_ingestion_jobs WHERE id=?", (job_id,))
        if not job or job["status"] == "SUCCEEDED":
            return
        version = self.db.fetch_one(
            """SELECT v.*, d.display_name, d.id AS document_id, d.knowledge_base_id
               FROM knowledge_document_versions v
               JOIN knowledge_documents d ON d.id=v.document_id WHERE v.id=?""",
            (job["document_version_id"],),
        )
        if not version:
            raise KnowledgeNotFoundError("Document version for ingestion job is missing")
        lease_token = _new_id("lease")
        now = utc_now()
        self.db.execute(
            """UPDATE knowledge_ingestion_jobs SET status='RUNNING', stage='DOWNLOADING',
               attempts=attempts+1, worker_id=?, lease_token=?, heartbeat_at=?, updated_at=?
               WHERE id=?""",
            (self.worker_id, lease_token, now, now, job_id),
        )
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
        content = await asyncio.to_thread(
            self.storage.get_content,
            version["object_key"],
            version.get("object_version_id"),
        )
        digest = hashlib.sha256(content).hexdigest()
        if version.get("expected_sha256") and digest != version["expected_sha256"]:
            raise KnowledgeValidationError("Document SHA-256 does not match the upload declaration")
        self._set_job_stage(job_id, "PARSING")
        blocks = await asyncio.to_thread(
            self.parser.parse, content, version["content_type"], version["display_name"]
        )
        chunks = self.chunker.chunk(blocks)
        if not chunks:
            raise KnowledgeValidationError("Document produced no indexable chunks")
        self._set_job_stage(job_id, "EMBEDDING")
        vectors: List[List[float]] = []
        for start in range(0, len(chunks), 64):
            vectors.extend(
                await asyncio.to_thread(
                    self.embedding.embed_documents,
                    [chunk.text for chunk in chunks[start : start + 64]],
                )
            )
        if len(vectors) != len(chunks):
            raise KnowledgeValidationError("Embedding provider returned an invalid result count")
        self._set_job_stage(job_id, "INDEXING")
        self.db.execute("DELETE FROM knowledge_chunks WHERE document_version_id=?", (version["id"],))
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
        self.db.execute_many(
            """INSERT INTO knowledge_chunks
               (id, tenant_id, project_id, knowledge_base_id, document_id,
                document_version_id, position, text, token_count, content_hash,
                locator_json, embedding_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        indexed_at = utc_now()
        self.db.execute(
            """UPDATE knowledge_document_versions
               SET status='READY', content_sha256=?, parser_version=?, chunker_version=?,
                   embedding_revision_id=?, indexed_at=?, error_code=NULL, error_message=NULL
               WHERE id=?""",
            (
                digest,
                self.parser.version,
                self.chunker.version,
                self.embedding.model_revision,
                indexed_at,
                version["id"],
            ),
        )
        self.db.execute(
            """UPDATE knowledge_documents SET current_version_id=?, status='READY', updated_at=?
               WHERE id=?""",
            (version["id"], indexed_at, version["document_id"]),
        )
        revision = self._publish_revision(job["knowledge_base_id"], context)
        self.db.execute(
            """UPDATE knowledge_ingestion_jobs SET status='SUCCEEDED', stage='COMPLETED',
               chunk_count=?, heartbeat_at=?, updated_at=? WHERE id=?""",
            (len(chunks), indexed_at, indexed_at, job_id),
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

    def _publish_revision(
        self, knowledge_base_id: str, context: TenantContext
    ) -> Dict[str, Any]:
        document_versions = self.db.fetch_all(
            """SELECT v.id, v.content_sha256, v.parser_version, v.chunker_version
               FROM knowledge_documents d
               JOIN knowledge_document_versions v ON v.id=d.current_version_id
               WHERE d.knowledge_base_id=? AND d.tenant_id=? AND d.project_id=?
                 AND d.status='READY' AND v.status='READY'
               ORDER BY d.id""",
            (knowledge_base_id, context.tenant_id, context.project_id),
        )
        if not document_versions:
            raise KnowledgeConflictError("Cannot publish an empty knowledge base revision")
        manifest = {
            "document_versions": [item["id"] for item in document_versions],
            "parser_versions": sorted({item["parser_version"] for item in document_versions}),
            "chunker_versions": sorted({item["chunker_version"] for item in document_versions}),
        }
        canonical = json.dumps(
            {
                "manifest": manifest,
                "embedding_model": self.embedding.model_revision,
                "embedding_dimensions": self.embedding.dimensions,
                "retrieval_profile": DEFAULT_RETRIEVAL_PROFILE,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        index_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        existing = self.db.fetch_one(
            """SELECT * FROM knowledge_base_revisions
               WHERE knowledge_base_id=? AND index_hash=?""",
            (knowledge_base_id, index_hash),
        )
        if existing:
            return existing
        latest = self.db.fetch_one(
            """SELECT MAX(revision_number) AS value FROM knowledge_base_revisions
               WHERE knowledge_base_id=?""",
            (knowledge_base_id,),
        )
        revision_id = _new_id("kbrev")
        revision_number = (latest["value"] or 0) + 1
        now = utc_now()
        with self.db.lock:
            connection = self.db.connection
            try:
                connection.execute("BEGIN")
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
                        self.db.encode(DEFAULT_RETRIEVAL_PROFILE),
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
                    """UPDATE knowledge_bases SET current_revision_id=?, updated_at=? WHERE id=?""",
                    (revision_id, now, knowledge_base_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.db.fetch_one("SELECT * FROM knowledge_base_revisions WHERE id=?", (revision_id,))

    def _fail_job(self, job_id: str, exc: Exception) -> None:
        job = self.db.fetch_one("SELECT * FROM knowledge_ingestion_jobs WHERE id=?", (job_id,))
        if not job:
            return
        code = getattr(exc, "code", "KNOWLEDGE_INGESTION_FAILED")
        message = str(exc)[:2000]
        now = utc_now()
        self.db.execute(
            """UPDATE knowledge_ingestion_jobs SET status='FAILED', stage='FAILED',
               error_code=?, error_message=?, heartbeat_at=?, updated_at=? WHERE id=?""",
            (code, message, now, now, job_id),
        )
        self.db.execute(
            """UPDATE knowledge_document_versions SET status='FAILED', error_code=?,
               error_message=? WHERE id=?""",
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
            """UPDATE knowledge_ingestion_jobs SET stage=?, heartbeat_at=?, updated_at=? WHERE id=?""",
            (stage, now, now, job_id),
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
        self, revision_ids: Iterable[str], context: TenantContext
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
        if any(revision["embedding_dimensions"] != self.embedding.dimensions for revision in revisions):
            raise KnowledgeConflictError("Knowledge revision embedding dimensions are incompatible")
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
        if document.get("visibility") == "private" and document.get("created_by") != context.user_id:
            return False
        allowed_roles = document.get("allowed_roles") or []
        return not allowed_roles or bool(set(allowed_roles).intersection(context.roles)) or "owner" in context.roles

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
                ingestion_job_id, type, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            ),
        )
