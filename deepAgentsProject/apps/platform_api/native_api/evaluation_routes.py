from fastapi import APIRouter, Depends

from apps.platform_api.dependencies import require_permission, services
from packages.auth import Permission
from packages.domain.models import TenantContext
from packages.evaluations.models import EvaluationPolicyUpdate, EvaluationSuiteCreate


router = APIRouter(prefix="/api/v1", tags=["evaluations"])
read = require_permission(Permission.AGENT_READ)
manage = require_permission(Permission.EVALUATION_MANAGE)


@router.post("/evaluation-suites", status_code=201)
def create_suite(payload: EvaluationSuiteCreate, context: TenantContext = Depends(manage), container=Depends(services)):
    return container.evaluations.create_suite(payload, context)


@router.get("/evaluation-suites")
def list_suites(context: TenantContext = Depends(read), container=Depends(services)):
    return {"items": container.evaluations.list_suites(context)}


@router.get("/evaluation-suites/{suite_id}")
def get_suite(suite_id: str, context: TenantContext = Depends(read), container=Depends(services)):
    return container.evaluations.get_suite(suite_id, context)


@router.get("/evaluation-policy")
def get_policy(context: TenantContext = Depends(read), container=Depends(services)):
    return container.evaluations.policy(context)


@router.put("/evaluation-policy")
def update_policy(payload: EvaluationPolicyUpdate, context: TenantContext = Depends(manage), container=Depends(services)):
    return container.evaluations.update_policy(payload, context)


@router.get("/evaluations/{evaluation_id}")
def get_evaluation(evaluation_id: str, context: TenantContext = Depends(read), container=Depends(services)):
    return container.evaluations.get_result(evaluation_id, context)
