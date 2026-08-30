from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from apps.platform_api.dependencies import services, tenant_context
from packages.domain.models import TenantContext
from packages.routing.models import (
    IntentRoutingResolve,
    RoutedRunCreate,
    RoutingProfileUpdate,
)


router = APIRouter(prefix="/api/v1", tags=["intent-routing"])


@router.get("/intent-routing/profile")
def get_routing_profile(
    context: TenantContext = Depends(tenant_context), container=Depends(services)
):
    return container.routing.get_profile(context)


@router.put("/intent-routing/profile")
def update_routing_profile(
    payload: RoutingProfileUpdate,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    if not {"owner", "admin"}.intersection(context.roles):
        raise HTTPException(status_code=403, detail="Routing profile updates require owner or admin role")
    return container.routing.update_profile(payload, context)


@router.post("/intent-routing:resolve", status_code=201)
async def resolve_intent_route(
    payload: IntentRoutingResolve,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return await container.routing.resolve(payload, context)


@router.post("/routed-runs", status_code=201)
async def create_routed_run(
    payload: RoutedRunCreate,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return await container.routing.create_routed_run(payload, context)


@router.get("/intent-routing/decisions")
def list_routing_decisions(
    limit: int = Query(default=100, ge=1, le=500),
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return {"items": container.routing.list_decisions(context, limit)}


@router.get("/intent-routing/decisions/{decision_id}")
def get_routing_decision(
    decision_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.routing.get_decision(decision_id, context)
