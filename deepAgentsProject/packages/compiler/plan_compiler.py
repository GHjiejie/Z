from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

from packages.domain.models import AgentDraftSpec


class SkillResolver(Protocol):
    def resolve_many(self, references: List[str]) -> List[Dict[str, Any]]: ...


@dataclass
class ValidationIssue:
    level: str
    code: str
    message: str
    path: str

    def as_dict(self) -> Dict[str, str]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "path": self.path,
        }


class AgentPlanCompiler:
    """Publish-time compiler: resolves and locks config without binding live resources."""

    ADAPTER_VERSION = "1.0.0"
    RUNTIME_IMAGE = "deepagent/runtime@sha256:phase1-reference-20260824"
    PACKAGE_VERSIONS = {
        "deepagents": "0.x-compatible",
        "langchain": "1.x-compatible",
        "langgraph": "1.x-compatible",
    }
    SUPPORTED_FEATURES = {
        "filesystem",
        "skills",
        "memory",
        "sync_subagents",
        "hitl",
        "streaming",
    }

    def __init__(self, skill_registry: Optional[SkillResolver] = None):
        self.skill_registry = skill_registry

    def validate(self, draft_data: Dict[str, Any]) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        try:
            draft = AgentDraftSpec.model_validate(draft_data)
        except Exception as exc:
            return [ValidationIssue("error", "SCHEMA_INVALID", str(exc), "draft")]

        if draft.harness_type != "deepagents":
            issues.append(
                ValidationIssue(
                    "warning",
                    "HARNESS_REFERENCE_ONLY",
                    f"{draft.harness_type} uses the reference executor in Phase 1",
                    "harness_type",
                )
            )
        if not draft.capabilities.tools:
            issues.append(
                ValidationIssue(
                    "warning",
                    "NO_TOOLS",
                    "Agent will run in model-only mode",
                    "capabilities.tools",
                )
            )
        if draft.capabilities.subagents and draft.limits.max_subagent_concurrency == 0:
            issues.append(
                ValidationIssue(
                    "error",
                    "SUBAGENT_LIMIT_CONFLICT",
                    "SubAgents are bound but max_subagent_concurrency is zero",
                    "limits.max_subagent_concurrency",
                )
            )
        if (
            draft.capabilities.knowledge_bases
            and "knowledge_search" in draft.capabilities.tools
            and draft.limits.max_tool_calls == 0
        ):
            issues.append(
                ValidationIssue(
                    "error",
                    "KNOWLEDGE_TOOL_BUDGET_CONFLICT",
                    "The built-in RAG agent requires at least one tool call",
                    "limits.max_tool_calls",
                )
            )
        if draft.capabilities.skills:
            if not self.skill_registry:
                issues.append(
                    ValidationIssue(
                        "error",
                        "SKILL_REGISTRY_UNAVAILABLE",
                        "Skills are bound but no Skill Registry is available",
                        "capabilities.skills",
                    )
                )
            else:
                try:
                    self.skill_registry.resolve_many(draft.capabilities.skills)
                except LookupError as exc:
                    issues.append(
                        ValidationIssue(
                            "error",
                            "SKILL_NOT_FOUND",
                            str(exc),
                            "capabilities.skills",
                        )
                    )
        if draft.policies.approval_mode == "never":
            issues.append(
                ValidationIssue(
                    "warning",
                    "HITL_DISABLED",
                    "High-risk tools will be denied instead of paused for approval",
                    "policies.approval_mode",
                )
            )
        return issues

    def compile(
        self,
        revision_id: str,
        draft_data: Dict[str, Any],
        model_snapshot: Dict[str, Any],
        knowledge_snapshots: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        issues = self.validate(draft_data)
        errors = [issue for issue in issues if issue.level == "error"]
        if errors:
            raise ValueError("; ".join(issue.message for issue in errors))

        draft = AgentDraftSpec.model_validate(draft_data)
        skill_versions = (
            self.skill_registry.resolve_many(draft.capabilities.skills)
            if draft.capabilities.skills and self.skill_registry
            else []
        )
        prompt_hash = hashlib.sha256(draft.system_prompt.encode("utf-8")).hexdigest()
        resolved = {
            "agent_revision_id": revision_id,
            "harness_type": draft.harness_type,
            "harness_adapter_version": self.ADAPTER_VERSION,
            "harness_profile_revision_id": draft.harness_profile_revision_id,
            "package_versions": self.PACKAGE_VERSIONS,
            "runtime_image_digest": self.RUNTIME_IMAGE,
            "model_deployment_revision_id": draft.model_deployment_id,
            "model_snapshot": model_snapshot,
            "prompt": draft.system_prompt,
            "prompt_hash": prompt_hash,
            "tool_bindings": [
                {
                    "name": name,
                    "schema_hash": hashlib.sha256(name.encode()).hexdigest()[:16],
                    "risk_level": "high" if name in {"artifact_write", "shell_execute"} else "low",
                }
                for name in draft.capabilities.tools
            ],
            "mcp_bindings": [{"revision_id": item} for item in draft.capabilities.mcp_servers],
            "skill_versions": skill_versions,
            "memory_versions": [{"revision_id": item} for item in draft.capabilities.memories],
            "knowledge_bindings": [
                {
                    "revision_id": item["id"],
                    "knowledge_base_id": item["knowledge_base_id"],
                    "index_hash": item["index_hash"],
                    "embedding_model": item["embedding_model"],
                    "embedding_dimensions": item["embedding_dimensions"],
                    "retrieval_profile": item["retrieval_profile"],
                    "access": "read_only",
                }
                for item in (knowledge_snapshots or [])
            ],
            "builtin_agent_bindings": [
                {
                    "name": "builtin_rag",
                    "version": "1.0.0",
                    "routing": "auto_evidence",
                    "tool": "knowledge_search",
                }
            ]
            if knowledge_snapshots and "knowledge_search" in draft.capabilities.tools
            else [],
            "subagent_bindings": [
                {
                    "name": name,
                    "execution_mode": "sync",
                    "orchestration_mode": "model_tool_call",
                    "transport": "in_process",
                }
                for name in draft.capabilities.subagents
            ],
            "filesystem_enabled": draft.capabilities.filesystem,
            "permission_policy_revision_id": draft.policies.permission_policy,
            "approval_mode": draft.policies.approval_mode,
            "audit_level": draft.policies.audit_level,
            "output_schema": draft.output_schema,
            "limits": draft.limits.model_dump(),
            "validation": [issue.as_dict() for issue in issues],
        }
        canonical = json.dumps(resolved, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        resolved["plan_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return resolved
