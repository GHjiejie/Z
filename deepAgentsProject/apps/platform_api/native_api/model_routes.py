from fastapi import APIRouter, Depends

from apps.platform_api.dependencies import require_permission, services
from packages.auth.permissions import Permission
from packages.domain.models import TenantContext
from packages.runtime.model_registry import ModelRegistration, ModelStatusUpdate


router = APIRouter(prefix="/api/v1",tags=["models"])
model_manage = require_permission(Permission.MODEL_MANAGE)


@router.get("/model-profiles")
def profiles(context:TenantContext=Depends(model_manage),container=Depends(services)):
    return {"items":container.models.list_profiles(context)}


@router.post("/model-deployments",status_code=201)
def register(payload:ModelRegistration,context:TenantContext=Depends(model_manage),container=Depends(services)):
    return container.models.register(payload,context)


@router.put("/model-deployments/{model_id}/status")
def status(model_id:str,payload:ModelStatusUpdate,context:TenantContext=Depends(model_manage),container=Depends(services)):
    return container.models.update_status(model_id,payload,context)
