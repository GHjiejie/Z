from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query

from apps.platform_api.dependencies import require_permission, services
from packages.auth import Permission
from packages.domain.models import TenantContext
from packages.routing.models import (
    IntentRoutingResolve,
    RoutedRunCreate,
    RoutingProfileUpdate,
    RoutingChangeCreate,
)
from packages.releases.models import ReleaseDecision, ReleaseCancel
from packages.routing.governance import RoutingChangeService


router = APIRouter(prefix="/api/v1", tags=["intent-routing"])

routing_read = require_permission(Permission.ROUTING_READ)
routing_use = require_permission(Permission.ROUTING_USE)
routing_manage = require_permission(Permission.ROUTING_MANAGE)
runtime_use = require_permission(Permission.RUNTIME_USE)


def routing_changes(container=Depends(services)):
    return RoutingChangeService(container.db, container.routing, container.models)


@router.get("/production-routing/profile")
def production_profile(context: TenantContext = Depends(routing_read), service=Depends(routing_changes)):
    return service.profile(context)


@router.get("/production-routing/revisions")
def production_revisions(limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = Query(default=None, max_length=1024),
    context: TenantContext = Depends(routing_read), service=Depends(routing_changes)):
    return service.history(context, limit=limit, cursor=cursor)


@router.post("/routing-change-requests", status_code=202)
def request_routing_change(payload: RoutingChangeCreate,
    context: TenantContext = Depends(require_permission(Permission.ROUTING_REQUEST)), service=Depends(routing_changes),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=200)):
    return service.create(payload, context, idempotency_key)


@router.get("/routing-change-requests")
def list_routing_changes(limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = Query(default=None, max_length=1024),
    context: TenantContext = Depends(routing_read), service=Depends(routing_changes)):
    return service.list(context, limit=limit, cursor=cursor)


@router.get("/routing-change-requests/{request_id}")
def get_routing_change(request_id: str, context: TenantContext = Depends(routing_read), service=Depends(routing_changes)):
    return service.get(request_id, context)


@router.post("/routing-change-requests/{request_id}:decide")
def decide_routing_change(request_id: str, payload: ReleaseDecision,
    context: TenantContext = Depends(require_permission(Permission.ROUTING_APPROVE)), service=Depends(routing_changes)):
    return service.decide(request_id, payload, context)


@router.post("/routing-change-requests/{request_id}:cancel")
def cancel_routing_change(request_id: str, payload: ReleaseCancel,
    context: TenantContext = Depends(routing_read), service=Depends(routing_changes)):
    return service.cancel(request_id, payload, context)


@router.get("/intent-routing/profile")
def get_routing_profile(
    context: TenantContext = Depends(routing_read), container=Depends(services)
):
    return container.routing.get_profile(context)


@router.put("/intent-routing/profile")
def update_routing_profile(
    payload: RoutingProfileUpdate,
    context: TenantContext = Depends(routing_manage),
    container=Depends(services),
):
    return container.routing.update_profile(payload, context)


@router.post("/intent-routing:resolve", status_code=201)
async def resolve_intent_route(
    payload: IntentRoutingResolve,
    context: TenantContext = Depends(routing_use),
    container=Depends(services),
):
    return await container.routing.resolve(payload, context)


@router.post("/routed-runs", status_code=201)
async def create_routed_run(
    payload: RoutedRunCreate,
    context: TenantContext = Depends(runtime_use),
    container=Depends(services),
):
    return await container.routing.create_routed_run(payload, context)


@router.get("/intent-routing/decisions")
def list_routing_decisions(
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None, max_length=1024),
    context: TenantContext = Depends(routing_read),
    container=Depends(services),
):
    return container.routing.decision_page(context, limit=limit, cursor=cursor)


@router.get("/intent-routing/decisions/{decision_id}")
def get_routing_decision(
    decision_id: str,
    context: TenantContext = Depends(routing_read),
    container=Depends(services),
):
    return container.routing.get_decision(decision_id, context)
