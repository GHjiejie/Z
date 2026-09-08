from typing import Literal

from fastapi import APIRouter, Depends, Query

from apps.platform_api.dependencies import require_permission, services
from packages.auth.permissions import Permission
from packages.billing.models import PricePolicy, QuotaPolicy, Reconciliation
from packages.billing.meter import Meter
from packages.domain.models import TenantContext


router = APIRouter(prefix="/api/v1/billing", tags=["billing"])
billing_manage = require_permission(Permission.BILLING_MANAGE)


@router.get("/quotas")
def quotas(context: TenantContext = Depends(billing_manage), container=Depends(services)):
    return {"items":container.billing.quotas(context)}


@router.put("/quotas")
def update_quota(payload: QuotaPolicy, context: TenantContext = Depends(billing_manage), container=Depends(services)):
    return container.billing.update_quota(payload, context)


@router.get("/prices")
def prices(context: TenantContext = Depends(billing_manage), container=Depends(services)):
    return {"items":container.billing.prices(context)}


@router.put("/prices")
def update_price(payload: PricePolicy, context: TenantContext = Depends(billing_manage), container=Depends(services)):
    return container.billing.update_price(payload, context)


@router.get("/providers")
def providers(context: TenantContext = Depends(billing_manage), container=Depends(services)):
    return {"items":[Meter.identity(container.model_gateway.identity()),Meter.identity(container.knowledge.embedding.identity())]}


@router.get("/calls")
def calls(limit: int = Query(default=50, ge=1, le=500), cursor: str | None = Query(default=None, max_length=1024),
          status: Literal["RESERVED","UNCERTAIN","ACTUAL","LEGACY"] | None = None,
          context: TenantContext = Depends(billing_manage), container=Depends(services)):
    return container.billing.calls(context, limit=limit, cursor=cursor, status=status)


@router.post("/calls/{call_id}:reconcile")
def reconcile(call_id: str, payload: Reconciliation, context: TenantContext = Depends(billing_manage), container=Depends(services)):
    return container.billing.reconcile(call_id, payload, context)
