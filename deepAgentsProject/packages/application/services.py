from __future__ import annotations

import hashlib
import secrets
from typing import Any, Dict, List, Optional

from packages.compiler import AgentPlanCompiler
from packages.domain.models import (
    AgentCreate,
    AgentDraftSpec,
    AgentDraftUpdate,
    CapabilityBindings,
    DeploymentCreate,
    TenantContext,
    utc_now,
)
from packages.persistence import Database


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class AgentService:
    def __init__(self, db: Database, compiler: AgentPlanCompiler):
        self.db = db
        self.compiler = compiler

    def list_agents(self, context: TenantContext) -> List[Dict[str, Any]]:
        agents = self.db.fetch_all(
            """SELECT * FROM agents WHERE tenant_id=? AND project_id=?
               ORDER BY updated_at DESC""",
            (context.tenant_id, context.project_id),
        )
        for agent in agents:
            agent["revision_count"] = self.db.fetch_one(
                "SELECT COUNT(*) AS count FROM agent_revisions WHERE agent_id=?", (agent["id"],)
            )["count"]
            deployment = self.db.fetch_one(
                "SELECT * FROM agent_deployments WHERE agent_id=? ORDER BY created_at DESC LIMIT 1",
                (agent["id"],),
            )
            agent["latest_deployment"] = deployment
        return agents

    def get_agent(self, agent_id: str, context: TenantContext) -> Dict[str, Any]:
        agent = self.db.fetch_one(
            "SELECT * FROM agents WHERE id=? AND tenant_id=? AND project_id=?",
            (agent_id, context.tenant_id, context.project_id),
        )
        if not agent:
            raise NotFoundError("Agent not found")
        agent["revisions"] = self.db.fetch_all(
            "SELECT * FROM agent_revisions WHERE agent_id=? ORDER BY revision_number DESC", (agent_id,)
        )
        agent["deployments"] = self.db.fetch_all(
            "SELECT * FROM agent_deployments WHERE agent_id=? ORDER BY created_at DESC", (agent_id,)
        )
        return agent

    def create_agent(self, payload: AgentCreate, context: TenantContext) -> Dict[str, Any]:
        agent_id = new_id("agt")
        now = utc_now()
        self.db.execute(
            """INSERT INTO agents
               (id, tenant_id, project_id, name, description, draft_json, status, version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'DRAFT', 1, ?, ?)""",
            (
                agent_id,
                context.tenant_id,
                context.project_id,
                payload.name,
                payload.description,
                self.db.encode(payload.draft.model_dump()),
                now,
                now,
            ),
        )
        return self.get_agent(agent_id, context)

    def update_draft(
        self, agent_id: str, payload: AgentDraftUpdate, context: TenantContext
    ) -> Dict[str, Any]:
        agent = self.get_agent(agent_id, context)
        if payload.version is not None and payload.version != agent["version"]:
            raise ConflictError(f"Draft changed; current version is {agent['version']}")
        now = utc_now()
        self.db.execute(
            """UPDATE agents SET name=?, description=?, draft_json=?, status='DRAFT',
               version=version+1, updated_at=? WHERE id=?""",
            (
                payload.name if payload.name is not None else agent["name"],
                payload.description if payload.description is not None else agent["description"],
                self.db.encode(payload.draft.model_dump()),
                now,
                agent_id,
            ),
        )
        return self.get_agent(agent_id, context)

    def validate_agent(self, agent_id: str, context: TenantContext) -> Dict[str, Any]:
        agent = self.get_agent(agent_id, context)
        issues = self.compiler.validate(agent["draft"])
        return {
            "valid": not any(issue.level == "error" for issue in issues),
            "issues": [issue.as_dict() for issue in issues],
            "checked_at": utc_now(),
        }

    def publish(self, agent_id: str, context: TenantContext) -> Dict[str, Any]:
        agent = self.get_agent(agent_id, context)
        validation = self.validate_agent(agent_id, context)
        if not validation["valid"]:
            raise ConflictError("Agent draft did not pass validation")

        latest = self.db.fetch_one(
            "SELECT MAX(revision_number) AS value FROM agent_revisions WHERE agent_id=?", (agent_id,)
        )
        revision_number = (latest["value"] or 0) + 1
        revision_id = new_id("rev")
        now = utc_now()
        self.db.execute(
            """INSERT INTO agent_revisions
               (id, agent_id, tenant_id, project_id, revision_number, spec_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                revision_id,
                agent_id,
                context.tenant_id,
                context.project_id,
                revision_number,
                self.db.encode(agent["draft"]),
                now,
            ),
        )

        model = self.db.fetch_one(
            """SELECT * FROM model_deployments
               WHERE id=? AND tenant_id=? AND project_id=?""",
            (agent["draft"]["model_deployment_id"], context.tenant_id, context.project_id),
        )
        if not model:
            raise ConflictError("Referenced model deployment does not exist in this project")
        plan = self.compiler.compile(revision_id, agent["draft"], model)
        plan_id = new_id("plan")
        plan["id"] = plan_id
        self.db.execute(
            """INSERT INTO resolved_execution_plans
               (id, agent_revision_id, tenant_id, project_id, plan_hash, plan_json,
                runtime_image_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                plan_id,
                revision_id,
                context.tenant_id,
                context.project_id,
                plan["plan_hash"],
                self.db.encode(plan),
                plan["runtime_image_digest"],
                now,
            ),
        )
        self.db.execute(
            "UPDATE agents SET status='PUBLISHED', updated_at=? WHERE id=?", (now, agent_id)
        )
        return {
            "revision": self.db.fetch_one("SELECT * FROM agent_revisions WHERE id=?", (revision_id,)),
            "resolved_plan": self.db.fetch_one(
                "SELECT * FROM resolved_execution_plans WHERE id=?", (plan_id,)
            ),
            "validation": validation,
        }

    def list_revisions(self, agent_id: str, context: TenantContext) -> List[Dict[str, Any]]:
        self.get_agent(agent_id, context)
        return self.db.fetch_all(
            "SELECT * FROM agent_revisions WHERE agent_id=? ORDER BY revision_number DESC", (agent_id,)
        )

    def get_revision(self, revision_id: str, context: TenantContext) -> Dict[str, Any]:
        revision = self.db.fetch_one(
            "SELECT * FROM agent_revisions WHERE id=? AND tenant_id=? AND project_id=?",
            (revision_id, context.tenant_id, context.project_id),
        )
        if not revision:
            raise NotFoundError("Agent revision not found")
        revision["resolved_plan"] = self.db.fetch_one(
            "SELECT * FROM resolved_execution_plans WHERE agent_revision_id=?", (revision_id,)
        )
        return revision

    def deploy(self, payload: DeploymentCreate, context: TenantContext) -> Dict[str, Any]:
        revision = self.get_revision(payload.agent_revision_id, context)
        plan = revision.get("resolved_plan")
        if not plan:
            raise ConflictError("Revision has no compiled execution plan")
        existing = self.db.fetch_one(
            """SELECT * FROM agent_deployments WHERE agent_revision_id=? AND environment=?
               AND tenant_id=? AND project_id=?""",
            (payload.agent_revision_id, payload.environment, context.tenant_id, context.project_id),
        )
        if existing:
            return existing
        deployment_id = new_id("dep")
        now = utc_now()
        name = payload.name or f"{payload.environment}-{revision['revision_number']}"
        self.db.execute(
            """INSERT INTO agent_deployments
               (id, tenant_id, project_id, agent_id, agent_revision_id, resolved_plan_id,
                name, environment, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)""",
            (
                deployment_id,
                context.tenant_id,
                context.project_id,
                revision["agent_id"],
                revision["id"],
                plan["id"],
                name,
                payload.environment,
                now,
                now,
            ),
        )
        return self.db.fetch_one("SELECT * FROM agent_deployments WHERE id=?", (deployment_id,))

    def list_deployments(self, context: TenantContext) -> List[Dict[str, Any]]:
        return self.db.fetch_all(
            """SELECT d.*, a.name AS agent_name FROM agent_deployments d
               JOIN agents a ON a.id=d.agent_id
               WHERE d.tenant_id=? AND d.project_id=? ORDER BY d.created_at DESC""",
            (context.tenant_id, context.project_id),
        )

    def list_models(self, context: TenantContext) -> List[Dict[str, Any]]:
        return self.db.fetch_all(
            "SELECT * FROM model_deployments WHERE tenant_id=? AND project_id=? ORDER BY name",
            (context.tenant_id, context.project_id),
        )


def seed_reference_data(db: Database, compiler: AgentPlanCompiler) -> None:
    if db.fetch_one("SELECT id FROM model_deployments LIMIT 1"):
        return
    now = utc_now()
    db.execute_many(
        """INSERT INTO model_deployments
           (id, tenant_id, project_id, name, provider, model, endpoint_region, status,
            capabilities_json, pricing_json, created_at)
           VALUES (?, 'tenant_demo', 'project_atlas', ?, ?, ?, ?, 'healthy', ?, ?, ?)""",
        [
            (
                "model_qwen_prod_v1",
                "Qwen Production",
                "OpenAI Compatible",
                "qwen3-235b-a22b",
                "cn-shanghai",
                db.encode(["tool_calling", "streaming", "structured_output", "reasoning"]),
                db.encode({"input_per_million": 0.8, "output_per_million": 3.2}),
                now,
            ),
            (
                "model_deepseek_fast_v1",
                "DeepSeek Fast",
                "OpenAI Compatible",
                "deepseek-v3",
                "cn-beijing",
                db.encode(["tool_calling", "streaming"]),
                db.encode({"input_per_million": 0.27, "output_per_million": 1.1}),
                now,
            ),
        ],
    )

    context = TenantContext(tenant_id="tenant_demo", project_id="project_atlas")
    service = AgentService(db, compiler)
    created = service.create_agent(
        AgentCreate(
            name="Release Sentinel",
            description="Plans releases, inspects risk, and pauses production changes for human approval.",
            draft=AgentDraftSpec(
                capabilities=CapabilityBindings(skills=["task-planning", "release-safety"])
            ),
        ),
        context,
    )
    published = service.publish(created["id"], context)
    service.deploy(
        DeploymentCreate(
            agent_revision_id=published["revision"]["id"],
            environment="development",
            name="release-sentinel-dev",
        ),
        context,
    )
