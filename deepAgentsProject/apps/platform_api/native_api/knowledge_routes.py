from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import RedirectResponse, Response

from apps.platform_api.dependencies import require_permission, services
from packages.auth import Permission
from packages.domain.models import TenantContext
from packages.knowledge.models import (
    KnowledgeBaseCreate,
    KnowledgeSearchRequest,
    UploadComplete,
    UploadPrepare,
)


router = APIRouter(prefix="/api/v1", tags=["knowledge"])

knowledge_read = require_permission(Permission.KNOWLEDGE_READ)
knowledge_manage = require_permission(Permission.KNOWLEDGE_MANAGE)


@router.get("/knowledge-bases")
def list_knowledge_bases(
    context: TenantContext = Depends(knowledge_read), container=Depends(services)
):
    return {"items": container.knowledge.list_knowledge_bases(context)}


@router.post("/knowledge-bases", status_code=201)
def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
    context: TenantContext = Depends(knowledge_manage),
    container=Depends(services),
):
    return container.knowledge.create_knowledge_base(payload, context, idempotency_key)


@router.get("/knowledge-bases/{knowledge_base_id}")
def get_knowledge_base(
    knowledge_base_id: str,
    context: TenantContext = Depends(knowledge_read),
    container=Depends(services),
):
    return container.knowledge.get_knowledge_base(knowledge_base_id, context)


@router.get("/knowledge-bases/{knowledge_base_id}/documents")
def list_knowledge_documents(
    knowledge_base_id: str,
    context: TenantContext = Depends(knowledge_read),
    container=Depends(services),
):
    return {"items": container.knowledge.list_documents(knowledge_base_id, context)}


@router.get("/knowledge-bases/{knowledge_base_id}/revisions")
def list_knowledge_revisions(
    knowledge_base_id: str,
    context: TenantContext = Depends(knowledge_read),
    container=Depends(services),
):
    return {"items": container.knowledge.list_revisions(knowledge_base_id, context)}


@router.post("/knowledge-bases/{knowledge_base_id}/documents:prepare-upload", status_code=201)
def prepare_knowledge_upload(
    knowledge_base_id: str,
    payload: UploadPrepare,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
    context: TenantContext = Depends(knowledge_manage),
    container=Depends(services),
):
    return container.knowledge.prepare_upload(knowledge_base_id, payload, context, idempotency_key)


@router.put("/knowledge-document-versions/{version_id}/content")
async def upload_knowledge_content(
    version_id: str,
    request: Request,
    content_type: str = Header(default="application/octet-stream", alias="Content-Type"),
    context: TenantContext = Depends(knowledge_manage),
    container=Depends(services),
):
    content = await request.body()
    return container.knowledge.upload_content(version_id, content, content_type, context)


@router.post("/knowledge-document-versions/{version_id}:complete", status_code=202)
async def complete_knowledge_upload(
    version_id: str,
    payload: UploadComplete,
    context: TenantContext = Depends(knowledge_manage),
    container=Depends(services),
):
    return await container.knowledge.complete_upload(version_id, payload, context)


@router.get("/knowledge-documents/{document_id}")
def get_knowledge_document(
    document_id: str,
    context: TenantContext = Depends(knowledge_read),
    container=Depends(services),
):
    return container.knowledge.get_document(document_id, context)


@router.get("/knowledge-documents/{document_id}/download")
def download_knowledge_document(
    document_id: str,
    context: TenantContext = Depends(knowledge_read),
    container=Depends(services),
):
    result = container.knowledge.download_document(document_id, context)
    if result["url"]:
        return RedirectResponse(result["url"], status_code=302)
    filename = quote(result["filename"], safe="")
    return Response(
        result["content"],
        media_type=result["content_type"],
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.get("/knowledge-document-versions/{version_id}/download")
def download_knowledge_document_version(
    version_id: str,
    context: TenantContext = Depends(knowledge_read),
    container=Depends(services),
):
    result = container.knowledge.download_document_version(version_id, context)
    if result["url"]:
        return RedirectResponse(result["url"], status_code=302)
    filename = quote(result["filename"], safe="")
    return Response(
        result["content"],
        media_type=result["content_type"],
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.get("/knowledge-ingestion-jobs/{job_id}")
def get_knowledge_ingestion_job(
    job_id: str,
    context: TenantContext = Depends(knowledge_read),
    container=Depends(services),
):
    return container.knowledge.get_ingestion_job(job_id, context)


@router.post("/knowledge-ingestion-jobs/{job_id}:retry", status_code=202)
async def retry_knowledge_ingestion_job(
    job_id: str,
    context: TenantContext = Depends(knowledge_manage),
    container=Depends(services),
):
    return await container.knowledge.retry_ingestion_job(job_id, context)


@router.post("/knowledge:search")
def search_knowledge(
    payload: KnowledgeSearchRequest,
    context: TenantContext = Depends(knowledge_read),
    container=Depends(services),
):
    return container.knowledge.search(payload, context)


@router.get("/knowledge-events")
def list_knowledge_events(
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None, max_length=1024),
    context: TenantContext = Depends(knowledge_read),
    container=Depends(services),
):
    return container.knowledge.list_events(context, limit=limit, cursor=cursor)
