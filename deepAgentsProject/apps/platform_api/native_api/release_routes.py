from fastapi import APIRouter, Depends, Header, Query

from apps.platform_api.dependencies import require_permission, services
from packages.auth.permissions import Permission
from packages.domain.models import TenantContext
from packages.releases.models import EnvironmentGrantUpdate, ReleaseCreate, ReleaseDecision, ReleaseCancel
from packages.releases.service import ReleaseService


router = APIRouter(prefix="/api/v1", tags=["releases"])
read = require_permission(Permission.DEPLOYMENT_READ)
manage = require_permission(Permission.DEPLOYMENT_MANAGE)
approve = require_permission(Permission.RELEASE_APPROVE)
grant = require_permission(Permission.RELEASE_GRANT_MANAGE)


def releases(container=Depends(services)):
    return ReleaseService(container.db, container.models)


@router.get("/deployment-environment-grants")
def list_grants(context: TenantContext = Depends(read), service=Depends(releases)):
    return {"items": service.grants(context)}


@router.put("/deployment-environment-grants")
def update_grant(payload: EnvironmentGrantUpdate, context: TenantContext = Depends(grant), service=Depends(releases)):
    return service.update_grant(payload, context)


@router.get("/agents/{agent_id}/release-channel")
def channel(agent_id: str, context: TenantContext = Depends(read), service=Depends(releases)):
    return service.channel(agent_id, context)


@router.post("/release-requests", status_code=202)
def request_release(payload: ReleaseCreate, context: TenantContext = Depends(manage), service=Depends(releases),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=200)):
    return service.create(payload, context, idempotency_key)


@router.get("/release-requests")
def list_requests(limit: int = Query(default=50, ge=1, le=500), cursor: str | None = Query(default=None, max_length=1024),
    context: TenantContext = Depends(read), service=Depends(releases)):
    return service.list(context, limit=limit, cursor=cursor)


@router.get("/release-requests/{request_id}")
def get_request(request_id: str, context: TenantContext = Depends(read), service=Depends(releases)):
    return service.get(request_id, context)


@router.post("/release-requests/{request_id}:decide")
def decide(request_id: str, payload: ReleaseDecision, context: TenantContext = Depends(approve), service=Depends(releases)):
    return service.decide(request_id, payload, context)


@router.post("/release-requests/{request_id}:cancel")
def cancel(request_id: str, payload: ReleaseCancel, context: TenantContext = Depends(read), service=Depends(releases)):
    return service.cancel(request_id, payload, context)
