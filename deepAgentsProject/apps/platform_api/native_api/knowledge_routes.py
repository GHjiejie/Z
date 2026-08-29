from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import RedirectResponse, Response

from apps.platform_api.dependencies import services, tenant_context
from packages.domain.models import TenantContext
from packages.knowledge.models import (
    KnowledgeBaseCreate,
    KnowledgeSearchRequest,
    UploadComplete,
    UploadPrepare,
)


router = APIRouter(prefix="/api/v1", tags=["knowledge"])


@router.get("/knowledge-bases")
def list_knowledge_bases(
    context: TenantContext = Depends(tenant_context), container=Depends(services)
):
    return {"items": container.knowledge.list_knowledge_bases(context)}


@router.post("/knowledge-bases", status_code=201)
def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.knowledge.create_knowledge_base(payload, context)


@router.get("/knowledge-bases/{knowledge_base_id}")
def get_knowledge_base(
    knowledge_base_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.knowledge.get_knowledge_base(knowledge_base_id, context)


@router.get("/knowledge-bases/{knowledge_base_id}/documents")
def list_knowledge_documents(
    knowledge_base_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return {"items": container.knowledge.list_documents(knowledge_base_id, context)}


@router.get("/knowledge-bases/{knowledge_base_id}/revisions")
def list_knowledge_revisions(
    knowledge_base_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return {"items": container.knowledge.list_revisions(knowledge_base_id, context)}


@router.post("/knowledge-bases/{knowledge_base_id}/documents:prepare-upload", status_code=201)
def prepare_knowledge_upload(
    knowledge_base_id: str,
    payload: UploadPrepare,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.knowledge.prepare_upload(knowledge_base_id, payload, context)


@router.put("/knowledge-document-versions/{version_id}/content")
async def upload_knowledge_content(
    version_id: str,
    request: Request,
    content_type: str = Header(default="application/octet-stream", alias="Content-Type"),
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    content = await request.body()
    return container.knowledge.upload_content(version_id, content, content_type, context)


@router.post("/knowledge-document-versions/{version_id}:complete", status_code=202)
async def complete_knowledge_upload(
    version_id: str,
    payload: UploadComplete,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return await container.knowledge.complete_upload(version_id, payload, context)


@router.get("/knowledge-documents/{document_id}")
def get_knowledge_document(
    document_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.knowledge.get_document(document_id, context)


@router.get("/knowledge-documents/{document_id}/download")
def download_knowledge_document(
    document_id: str,
    context: TenantContext = Depends(tenant_context),
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


@router.get("/knowledge-ingestion-jobs/{job_id}")
def get_knowledge_ingestion_job(
    job_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.knowledge.get_ingestion_job(job_id, context)


@router.post("/knowledge-ingestion-jobs/{job_id}:retry", status_code=202)
async def retry_knowledge_ingestion_job(
    job_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return await container.knowledge.retry_ingestion_job(job_id, context)


@router.post("/knowledge:search")
def search_knowledge(
    payload: KnowledgeSearchRequest,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.knowledge.search(payload, context)


@router.get("/knowledge-events")
def list_knowledge_events(
    limit: int = Query(default=100, ge=1, le=500),
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return {
        "items": container.db.fetch_all(
            """SELECT * FROM knowledge_events WHERE tenant_id=? AND project_id=?
               ORDER BY created_at DESC LIMIT ?""",
            (context.tenant_id, context.project_id, limit),
        )
    }
