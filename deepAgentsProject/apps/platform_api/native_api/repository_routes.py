from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from apps.platform_api.dependencies import services, tenant_context
from packages.coding.models import RepositoryCreate, RepositorySnapshotCreate
from packages.domain.models import TenantContext


router = APIRouter(prefix="/api/v1", tags=["repositories"])


@router.get("/local-repository-folders")
def browse_local_repository_folders(
    path: Optional[str] = Query(default=None, max_length=4096),
    _: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.repositories.browse_local_folders(path)


@router.post("/repositories", status_code=201)
def create_repository(
    payload: RepositoryCreate,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.repositories.create_repository(payload, context)


@router.get("/repositories")
def list_repositories(
    context: TenantContext = Depends(tenant_context), container=Depends(services)
):
    return {"items": container.repositories.list_repositories(context)}


@router.get("/repositories/{repository_id}")
def get_repository(
    repository_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.repositories.get_repository(repository_id, context)


@router.post("/repositories/{repository_id}:probe")
def probe_repository(
    repository_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.repositories.probe(repository_id, context)


@router.post("/repositories/{repository_id}/snapshots", status_code=201)
def create_repository_snapshot(
    repository_id: str,
    payload: RepositorySnapshotCreate,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.repositories.create_snapshot(repository_id, payload, context)


@router.get("/repository-snapshots/{snapshot_id}")
def get_repository_snapshot(
    snapshot_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.repositories.get_snapshot(snapshot_id, context)
