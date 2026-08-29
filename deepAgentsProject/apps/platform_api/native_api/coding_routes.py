from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from apps.platform_api.dependencies import services, tenant_context
from packages.coding.models import ChangeSetDecision
from packages.domain.models import TenantContext


router = APIRouter(prefix="/api/v1", tags=["coding"])


@router.get("/threads/{thread_id}/workspace")
def get_thread_workspace(
    thread_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.coding.get_thread_workspace(thread_id, context)


@router.get("/runs/{run_id}/workspace/tree")
async def get_workspace_tree(
    run_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return await container.coding.tree(run_id, context)


@router.get("/runs/{run_id}/workspace/file")
async def get_workspace_file(
    run_id: str,
    path: str = Query(min_length=1, max_length=4096),
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return await container.coding.file(run_id, path, context)


@router.get("/runs/{run_id}/diff")
def get_run_diff(
    run_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.coding.diff(run_id, context)


@router.get("/runs/{run_id}/verification")
def get_run_verification(
    run_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.coding.verification(run_id, context)


@router.get("/runs/{run_id}/changesets")
def list_run_changesets(
    run_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return {"items": container.coding.change_sets(run_id, context)}


@router.post("/runs/{run_id}/changesets/{change_set_id}:approve")
def approve_change_set(
    run_id: str,
    change_set_id: str,
    payload: ChangeSetDecision,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.coding.decide_change_set(
        run_id, change_set_id, True, context, payload.message
    )


@router.post("/runs/{run_id}/changesets/{change_set_id}:reject")
def reject_change_set(
    run_id: str,
    change_set_id: str,
    payload: ChangeSetDecision,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.coding.decide_change_set(
        run_id, change_set_id, False, context, payload.message
    )
