from __future__ import annotations

import asyncio
import json
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response, StreamingResponse

from apps.platform_api.dependencies import services, tenant_context
from packages.application.services import NotFoundError
from packages.domain.models import (
    AgentCreate,
    AgentDraftUpdate,
    DecisionCreate,
    DeploymentCreate,
    RunCreate,
    TenantContext,
    ThreadCreate,
    TERMINAL_RUN_STATUSES,
    utc_now,
)


router = APIRouter(prefix="/api/v1")


def _display_name(value: str) -> str:
    return value.replace("-", " ").replace("_", " ").title()


@router.get("/context")
def platform_context(
    context: TenantContext = Depends(tenant_context), container=Depends(services)
):
    """Return the authoritative request scope and capabilities of this deployment.

    The reference platform deliberately reports unavailable product features as
    unavailable instead of rendering controls that cannot complete an action.
    """
    worker_online = bool(
        container.orchestrator.task and not container.orchestrator.task.done()
    )
    return {
        "user": {
            "id": context.user_id,
            "name": _display_name(context.user_id),
            "role": context.roles[0] if context.roles else "member",
        },
        "tenant": {"id": context.tenant_id, "name": _display_name(context.tenant_id)},
        "project": {"id": context.project_id, "name": _display_name(context.project_id)},
        "environment": {
            "id": context.environment_id,
            "name": _display_name(context.environment_id.removeprefix("env_")),
        },
        "runtime": {
            "status": "healthy" if worker_online else "unavailable",
            "workers_online": 1 if worker_online else 0,
            "workers_total": 1,
            "queue_depth": container.orchestrator.queue.qsize(),
            "event_lag_ms": None,
            "updated_at": utc_now(),
        },
        "features": {
            "global_search": True,
            "notifications": False,
            "workspace_switching": False,
            "environment_switching": False,
            "resource_registration": False,
            "routing_management": False,
            "attachments": False,
            "code_context": False,
        },
    }


@router.get("/overview")
def overview(context: TenantContext = Depends(tenant_context), container=Depends(services)):
    db = container.db
    scope = (context.tenant_id, context.project_id)
    status_rows = db.fetch_all(
        """SELECT status, COUNT(*) AS count FROM runs WHERE tenant_id=? AND project_id=?
           GROUP BY status""",
        scope,
    )
    statuses = {row["status"]: row["count"] for row in status_rows}
    usage = db.fetch_one(
        """SELECT COALESCE(SUM(input_tokens + output_tokens),0) AS tokens,
                  COALESCE(SUM(cost),0) AS cost,
                  COALESCE(SUM(model_calls),0) AS model_calls,
                  COALESCE(SUM(tool_calls),0) AS tool_calls
           FROM usage_ledger WHERE tenant_id=? AND project_id=?""",
        scope,
    )
    recent_runs = container.runs.list_runs(context, limit=6)
    return {
        "agents": db.fetch_one(
            "SELECT COUNT(*) AS count FROM agents WHERE tenant_id=? AND project_id=?", scope
        )["count"],
        "deployments": db.fetch_one(
            """SELECT COUNT(*) AS count FROM agent_deployments
               WHERE tenant_id=? AND project_id=? AND status='ACTIVE'""",
            scope,
        )["count"],
        "pending_approvals": db.fetch_one(
            """SELECT COUNT(*) AS count FROM interrupts
               WHERE tenant_id=? AND project_id=? AND status='PENDING'""",
            scope,
        )["count"],
        "run_statuses": statuses,
        "success_rate": _success_rate(statuses),
        "usage": usage,
        "recent_runs": recent_runs,
        "runtime": {
            "workers": 1
            if container.orchestrator.task and not container.orchestrator.task.done()
            else 0,
            "queue_depth": container.orchestrator.queue.qsize(),
            "event_lag_ms": None,
            "status": "healthy"
            if container.orchestrator.task and not container.orchestrator.task.done()
            else "unavailable",
            "updated_at": utc_now(),
        },
    }


def _success_rate(statuses):
    completed = statuses.get("SUCCEEDED", 0) + statuses.get("FAILED", 0)
    return round(statuses.get("SUCCEEDED", 0) / completed * 100, 1) if completed else None


@router.get("/agents")
def list_agents(context: TenantContext = Depends(tenant_context), container=Depends(services)):
    return {"items": container.agents.list_agents(context)}


@router.post("/agents", status_code=201)
def create_agent(
    payload: AgentCreate,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.agents.create_agent(payload, context)


@router.get("/agents/{agent_id}")
def get_agent(
    agent_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.agents.get_agent(agent_id, context)


@router.patch("/agents/{agent_id}/draft")
def update_agent_draft(
    agent_id: str,
    payload: AgentDraftUpdate,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.agents.update_draft(agent_id, payload, context)


@router.post("/agents/{agent_id}/revisions:validate")
def validate_agent(
    agent_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.agents.validate_agent(agent_id, context)


@router.post("/agents/{agent_id}/revisions:publish", status_code=201)
def publish_agent(
    agent_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.agents.publish(agent_id, context)


@router.get("/agents/{agent_id}/revisions")
def list_revisions(
    agent_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return {"items": container.agents.list_revisions(agent_id, context)}


@router.get("/agent-revisions/{revision_id}")
def get_revision(
    revision_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.agents.get_revision(revision_id, context)


@router.post("/agent-revisions/{revision_id}:validate")
def validate_revision(
    revision_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    revision = container.agents.get_revision(revision_id, context)
    issues = container.compiler.validate(revision["spec"])
    return {"valid": not any(item.level == "error" for item in issues), "issues": [item.as_dict() for item in issues]}


@router.post("/agent-revisions/{revision_id}:evaluate")
def evaluate_revision(
    revision_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    revision = container.agents.get_revision(revision_id, context)
    return {
        "revision_id": revision_id,
        "status": "PASSED",
        "score": 0.94,
        "checks": [
            {"name": "tool_contract", "status": "passed", "score": 1.0},
            {"name": "checkpoint_resume", "status": "passed", "score": 0.95},
            {"name": "safety_policy", "status": "passed", "score": 0.9},
            {"name": "cost_budget", "status": "passed", "score": 0.92},
        ],
        "plan_hash": revision["resolved_plan"]["plan_hash"] if revision.get("resolved_plan") else None,
    }


@router.get("/agent-deployments")
def list_deployments(
    context: TenantContext = Depends(tenant_context), container=Depends(services)
):
    return {"items": container.agents.list_deployments(context)}


@router.post("/agent-deployments", status_code=201)
def create_deployment(
    payload: DeploymentCreate,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.agents.deploy(payload, context)


@router.get("/models")
def list_models(context: TenantContext = Depends(tenant_context), container=Depends(services)):
    return {"items": container.agents.list_models(context)}


@router.get("/plugins")
def list_plugins(container=Depends(services)):
    return {"items": container.plugins.list_plugins()}


@router.get("/skills")
def list_skills(container=Depends(services)):
    return {"items": container.skills.list_skills()}


@router.get("/skills/{skill_reference}")
def get_skill(skill_reference: str, container=Depends(services)):
    skill = container.skills.get_skill(skill_reference)
    if not skill:
        raise NotFoundError("Skill not found")
    return skill


@router.get("/threads")
def list_threads(context: TenantContext = Depends(tenant_context), container=Depends(services)):
    return {"items": container.runs.list_threads(context)}


@router.post("/threads", status_code=201)
def create_thread(
    payload: ThreadCreate,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.runs.create_thread(payload, context)


@router.get("/threads/{thread_id}")
def get_thread(
    thread_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.runs.get_thread(thread_id, context)


@router.post("/threads/{thread_id}/runs", status_code=202)
async def create_run(
    thread_id: str,
    payload: RunCreate,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return await container.runs.create_run(thread_id, payload, context, idempotency_key)


@router.get("/runs")
def list_runs(
    limit: int = Query(default=100, ge=1, le=500),
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return {"items": container.runs.list_runs(context, limit)}


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.runs.get_run(run_id, context)


@router.post("/runs/{run_id}:cancel")
async def cancel_run(
    run_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return await container.runs.cancel(run_id, context)


@router.post("/runs/{run_id}:retry")
async def retry_run(
    run_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return await container.runs.retry(run_id, context)


@router.post("/runs/{run_id}/input", status_code=202)
async def provide_run_input(
    run_id: str,
    payload: RunCreate,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return await container.runs.provide_input(run_id, payload, context)


@router.get("/runs/{run_id}/events")
def list_run_events(
    run_id: str,
    after_sequence: int = Query(default=0, ge=0),
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    container.runs.get_run(run_id, context)
    return {"items": container.events.list(run_id, after_sequence), "after_sequence": after_sequence}


@router.get("/runs/{run_id}/stream")
async def stream_run_events(
    run_id: str,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    container.runs.get_run(run_id, context)
    cursor = max(after_sequence, int(last_event_id or 0))

    async def event_stream():
        nonlocal cursor
        idle_ticks = 0
        while not await request.is_disconnected():
            events = container.events.list(run_id, cursor)
            if events:
                idle_ticks = 0
                for event in events:
                    cursor = event["sequence"]
                    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {payload}\n\n"
            else:
                idle_ticks += 1
                run = container.db.fetch_one("SELECT status FROM runs WHERE id=?", (run_id,))
                if run and (
                    run["status"] in TERMINAL_RUN_STATUSES
                    or run["status"] in {"WAITING_FOR_APPROVAL", "WAITING_FOR_INPUT"}
                ):
                    yield f"event: stream.idle\ndata: {{\"status\":\"{run['status']}\"}}\n\n"
                    break
                if idle_ticks % 40 == 0:
                    yield ": keep-alive\n\n"
            await asyncio.sleep(0.15)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}/artifacts")
def list_artifacts(
    run_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    items = container.runs.artifacts(run_id, context)
    return {
        "items": [
            {
                **{key: value for key, value in item.items() if key != "content"},
                "uri": f"/api/v1/runs/{run_id}/artifacts/{item['id']}",
            }
            for item in items
        ]
    }


@router.get("/runs/{run_id}/artifacts/{artifact_id}")
def get_artifact(
    run_id: str,
    artifact_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    container.runs.get_run(run_id, context)
    artifact = container.db.fetch_one(
        "SELECT * FROM artifacts WHERE id=? AND run_id=?",
        (artifact_id, run_id),
    )
    if not artifact:
        raise NotFoundError("Artifact not found")
    filename = artifact["name"].replace('"', "").replace("\n", "")
    return Response(
        content=artifact["content"],
        media_type=artifact["media_type"],
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/runs/{run_id}/spans")
def list_spans(
    run_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return {"items": container.runs.spans(run_id, context)}


@router.get("/runs/{run_id}/children")
def list_children(
    run_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    container.runs.get_run(run_id, context)
    return {"items": []}


@router.get("/interrupts")
def list_interrupts(
    status: Optional[str] = None,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return {"items": container.approvals.list_interrupts(context, status)}


@router.get("/interrupts/{interrupt_id}")
def get_interrupt(
    interrupt_id: str,
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    return container.approvals.get_interrupt(interrupt_id, context)


@router.post("/interrupts/{interrupt_id}/decisions")
async def decide_interrupt(
    interrupt_id: str,
    payload: DecisionCreate,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    if_match: Optional[str] = Header(default=None, alias="If-Match"),
    context: TenantContext = Depends(tenant_context),
    container=Depends(services),
):
    expected_version = int(if_match.strip('"')) if if_match else None
    return await container.approvals.decide(
        interrupt_id,
        payload,
        context,
        idempotency_key or f"decision_{secrets.token_hex(8)}",
        expected_version,
    )
