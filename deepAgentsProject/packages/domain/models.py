from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from packages.coding.models import CodingProfileSpec, WorkspaceBinding


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStatus(str, Enum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    PAUSED = "PAUSED"
    ORPHANED = "ORPHANED"
    RESUMING = "RESUMING"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    FAILED = "FAILED"
    FAILED_BUDGET = "FAILED_BUDGET"
    SUCCEEDED = "SUCCEEDED"


TERMINAL_RUN_STATUSES = {
    RunStatus.CANCELLED.value,
    RunStatus.TIMED_OUT.value,
    RunStatus.FAILED.value,
    RunStatus.FAILED_BUDGET.value,
    RunStatus.SUCCEEDED.value,
}


class RunLimits(BaseModel):
    max_duration_seconds: int = Field(default=600, ge=10, le=86400)
    max_model_calls: int = Field(default=20, ge=1, le=1000)
    max_tool_calls: int = Field(default=30, ge=0, le=1000)
    max_subagent_depth: int = Field(default=3, ge=0, le=10)
    max_subagent_concurrency: int = Field(default=4, ge=0, le=50)
    max_sandbox_cpu_seconds: int = Field(default=120, ge=0, le=86400)
    max_output_bytes: int = Field(default=1_000_000, ge=1024)
    max_cost: Optional[float] = Field(default=5.0, ge=0)


class CapabilityBindings(BaseModel):
    tools: List[str] = Field(default_factory=lambda: ["knowledge_search", "artifact_write"])
    mcp_servers: List[str] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    memories: List[str] = Field(default_factory=list)
    knowledge_bases: List[str] = Field(default_factory=list)
    subagents: List[str] = Field(default_factory=lambda: ["researcher"])
    filesystem: bool = True


class PolicyBindings(BaseModel):
    permission_policy: str = "project-default"
    approval_mode: Literal["never", "high_risk", "always"] = "high_risk"
    audit_level: Literal["standard", "strict"] = "strict"


class AgentDraftSpec(BaseModel):
    harness_type: Literal["deepagents", "langchain_agent", "custom_langgraph"] = "deepagents"
    harness_profile_revision_id: str = "deepagents-0.x-adapter-1.0"
    model_deployment_id: str = "model_qwen_prod_v1"
    system_prompt: str = "You are a careful project agent. Plan first, use approved tools, and cite artifacts."
    capabilities: CapabilityBindings = Field(default_factory=CapabilityBindings)
    policies: PolicyBindings = Field(default_factory=PolicyBindings)
    limits: RunLimits = Field(default_factory=RunLimits)
    output_schema: Optional[Dict[str, Any]] = None
    coding: Optional[CodingProfileSpec] = None

    @field_validator("system_prompt")
    @classmethod
    def prompt_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("system_prompt cannot be empty")
        return value.strip()


class AgentCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    description: str = Field(default="", max_length=500)
    draft: AgentDraftSpec = Field(default_factory=AgentDraftSpec)


class AgentDraftUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=80)
    description: Optional[str] = Field(default=None, max_length=500)
    draft: AgentDraftSpec
    version: Optional[int] = None


class DeploymentCreate(BaseModel):
    agent_revision_id: str
    environment: Literal["development", "staging", "production"] = "development"
    name: Optional[str] = None


class ThreadCreate(BaseModel):
    agent_deployment_id: str
    title: str = Field(default="New agent task", min_length=1, max_length=160)
    workspace: Optional[WorkspaceBinding] = None


class RunCreate(BaseModel):
    input: str = Field(min_length=1, max_length=100_000)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class DecisionItem(BaseModel):
    action_id: str
    type: Literal["approve", "edit", "reject", "respond"]
    message: Optional[str] = None
    edited_arguments: Optional[Dict[str, Any]] = None


class DecisionCreate(BaseModel):
    decisions: List[DecisionItem] = Field(min_length=1)


class RuntimeEvent(BaseModel):
    event_id: str
    sequence: int
    schema_version: str = "1.0"
    type: str
    tenant_id: str
    project_id: str
    thread_id: str
    run_id: str
    attempt_id: Optional[str] = None
    span_id: Optional[str] = None
    parent_span_id: Optional[str] = None
    execution_path: List[str] = Field(default_factory=lambda: ["main"])
    occurred_at: str = Field(default_factory=utc_now)
    visibility: Literal["user", "admin", "internal"] = "user"
    payload: Dict[str, Any] = Field(default_factory=dict)


class TenantContext(BaseModel):
    tenant_id: str
    project_id: str
    environment_id: str = "env_development"
    user_id: str = "user_demo"
    roles: List[str] = Field(default_factory=lambda: ["owner"])
    is_super_admin: bool = False
