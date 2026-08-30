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
    PolicyBindings,
    RunLimits,
    TenantContext,
    utc_now,
)
from packages.coding.models import CodingProfileSpec, SandboxProfileSpec
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
        model = self.db.fetch_one(
            """SELECT * FROM model_deployments
               WHERE id=? AND tenant_id=? AND project_id=?""",
            (agent["draft"]["model_deployment_id"], context.tenant_id, context.project_id),
        )
        if not model:
            raise ConflictError("Referenced model deployment does not exist in this project")
        knowledge_snapshots = self._resolve_knowledge_revisions(
            agent["draft"]["capabilities"].get("knowledge_bases", []), context
        )
        try:
            plan = self.compiler.compile(
                revision_id,
                agent["draft"],
                model,
                knowledge_snapshots=knowledge_snapshots,
            )
        except ValueError as exc:
            raise ConflictError(str(exc)) from exc
        plan_id = new_id("plan")
        plan["id"] = plan_id
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

    def _resolve_knowledge_revisions(
        self, references: List[str], context: TenantContext
    ) -> List[Dict[str, Any]]:
        snapshots: List[Dict[str, Any]] = []
        for revision_id in references:
            revision = self.db.fetch_one(
                """SELECT * FROM knowledge_base_revisions
                   WHERE id=? AND tenant_id=? AND project_id=? AND status='ACTIVE'""",
                (revision_id, context.tenant_id, context.project_id),
            )
            if not revision:
                raise ConflictError(
                    f"Knowledge revision {revision_id} is unavailable or not active in this project"
                )
            snapshots.append(revision)
        return snapshots

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
        deployments = self.db.fetch_all(
            """SELECT d.*, a.name AS agent_name FROM agent_deployments d
               JOIN agents a ON a.id=d.agent_id
               WHERE d.tenant_id=? AND d.project_id=? ORDER BY d.created_at DESC""",
            (context.tenant_id, context.project_id),
        )
        for deployment in deployments:
            plan = self.db.fetch_one(
                "SELECT * FROM resolved_execution_plans WHERE id=?",
                (deployment["resolved_plan_id"],),
            )
            coding_profile = (plan or {}).get("plan", {}).get("coding_profile")
            knowledge_bindings = (plan or {}).get("plan", {}).get("knowledge_bindings", [])
            deployment["coding_enabled"] = bool(
                coding_profile and coding_profile.get("enabled")
            )
            deployment["coding_profile"] = coding_profile
            deployment["knowledge_enabled"] = bool(knowledge_bindings)
        return deployments

    def list_models(self, context: TenantContext) -> List[Dict[str, Any]]:
        return self.db.fetch_all(
            "SELECT * FROM model_deployments WHERE tenant_id=? AND project_id=? ORDER BY name",
            (context.tenant_id, context.project_id),
        )


def seed_reference_data(
    db: Database,
    compiler: AgentPlanCompiler,
    *,
    coding_sandbox: SandboxProfileSpec | None = None,
) -> Dict[str, Any]:
    """Idempotently install the built-in demo catalog, including Coding Agent.

    This intentionally checks each resource instead of treating a non-empty database
    as fully seeded. That lets upgrades add newly built-in agents to existing local
    installations without duplicating earlier seed data.
    """
    now = utc_now()
    models = [
        (
            "model_qwen_prod_v1",
            "Qwen Production",
            "OpenAI Compatible",
            "qwen3-235b-a22b",
            "cn-shanghai",
            ["tool_calling", "streaming", "structured_output", "reasoning"],
            {"input_per_million": 0.8, "output_per_million": 3.2},
        ),
        (
            "model_deepseek_fast_v1",
            "DeepSeek Fast",
            "OpenAI Compatible",
            "deepseek-v3",
            "cn-beijing",
            ["tool_calling", "streaming"],
            {"input_per_million": 0.27, "output_per_million": 1.1},
        ),
    ]
    for model_id, name, provider, model, region, capabilities, pricing in models:
        if db.fetch_one("SELECT id FROM model_deployments WHERE id=?", (model_id,)):
            continue
        db.execute(
            """INSERT INTO model_deployments
               (id, tenant_id, project_id, name, provider, model, endpoint_region, status,
                capabilities_json, pricing_json, created_at)
               VALUES (?, 'tenant_demo', 'project_atlas', ?, ?, ?, ?, 'healthy', ?, ?, ?)""",
            (
                model_id,
                name,
                provider,
                model,
                region,
                db.encode(capabilities),
                db.encode(pricing),
                now,
            ),
        )

    context = TenantContext(tenant_id="tenant_demo", project_id="project_atlas")
    service = AgentService(db, compiler)
    reference = _ensure_seed_agent(
        db,
        service,
        context,
        AgentCreate(
            name="Release Sentinel",
            description="Plans releases, inspects risk, and pauses production changes for human approval.",
            draft=AgentDraftSpec(
                capabilities=CapabilityBindings(skills=["task-planning", "release-safety"])
            ),
        ),
        deployment_name="release-sentinel-dev",
    )
    coding = _ensure_seed_agent(
        db,
        service,
        context,
        AgentCreate(
            name="Built-in Coding Agent",
            description=(
                "Inspects repositories, makes isolated code changes, runs verification, "
                "and delivers reviewable patches from a governed sandbox."
            ),
            draft=AgentDraftSpec(
                harness_profile_revision_id="coding-agent-v1",
                system_prompt=(
                    "You are a careful coding agent. Inspect first, preserve unrelated "
                    "work, make minimal changes, run real verification, and deliver a "
                    "reviewable patch."
                ),
                capabilities=CapabilityBindings(
                    tools=[],
                    skills=[
                        "coding-workflow",
                        "repository-safety",
                        "test-and-verification",
                        "change-delivery",
                    ],
                    subagents=[
                        "codebase-explorer",
                        "code-reviewer",
                        "test-diagnostician",
                    ],
                    filesystem=True,
                ),
                policies=PolicyBindings(
                    permission_policy="coding-project-default-v1",
                    approval_mode="high_risk",
                    audit_level="strict",
                ),
                limits=RunLimits(
                    max_duration_seconds=1800,
                    max_model_calls=40,
                    max_tool_calls=100,
                    max_subagent_depth=2,
                    max_subagent_concurrency=3,
                    max_sandbox_cpu_seconds=900,
                    max_output_bytes=1_000_000,
                    max_cost=5,
                ),
                coding=CodingProfileSpec(
                    enabled=True,
                    sandbox=coding_sandbox or SandboxProfileSpec(),
                ),
            ),
        ),
        deployment_name="builtin-coding-dev",
    )
    return {"reference_agent": reference, "coding_agent": coding}


def _ensure_seed_agent(
    db: Database,
    service: AgentService,
    context: TenantContext,
    payload: AgentCreate,
    *,
    deployment_name: str,
) -> Dict[str, Any]:
    agent = db.fetch_one(
        """SELECT * FROM agents
           WHERE tenant_id=? AND project_id=? AND name=? ORDER BY created_at LIMIT 1""",
        (context.tenant_id, context.project_id, payload.name),
    )
    if agent is None:
        agent = service.create_agent(payload, context)
    revision = db.fetch_one(
        """SELECT * FROM agent_revisions
           WHERE agent_id=? ORDER BY revision_number DESC LIMIT 1""",
        (agent["id"],),
    )
    if revision is None:
        revision = service.publish(agent["id"], context)["revision"]
    deployment = service.deploy(
        DeploymentCreate(
            agent_revision_id=revision["id"],
            environment="development",
            name=deployment_name,
        ),
        context,
    )
    return {"agent_id": agent["id"], "deployment_id": deployment["id"]}
