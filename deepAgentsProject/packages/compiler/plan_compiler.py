from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol

from packages.domain.models import AgentDraftSpec
from packages.coding.errors import SandboxPolicyError
from packages.sandbox.policy import SandboxPolicy


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

    ADAPTER_VERSION = "2.0.0"
    RUNTIME_IMAGE = "deepagent/runtime@sha256:phase1-reference-20260824"
    PACKAGE_VERSIONS = {
        "deepagents": "0.7.11",
        "langchain": "1.3.18",
        "langgraph": "1.2.11",
        "langchain-openai": "1.6.0",
        "langchain-anthropic": "1.7.0",
        "langgraph-checkpoint-sqlite": "3.1.1",
    }
    SUPPORTED_FEATURES = {
        "filesystem",
        "skills",
        "memory",
        "sync_subagents",
        "hitl",
        "streaming",
        "sandbox",
        "coding",
    }
    TOOL_SCHEMAS = {
        "ls": {"path": "string"},
        "glob": {"pattern": "string", "path": "string|null"},
        "grep": {"pattern": "string", "path": "string|null", "glob": "string|null"},
        "read_file": {"file_path": "string", "offset": "integer", "limit": "integer"},
        "write_file": {"file_path": "string", "content": "string"},
        "edit_file": {
            "file_path": "string",
            "old_string": "string",
            "new_string": "string",
            "replace_all": "boolean",
        },
        "execute": {"command": "string", "timeout": "integer|null"},
        "git_status": {},
        "git_diff": {"path": "string|null"},
        "verification_report": {},
    }

    def __init__(
        self,
        skill_registry: Optional[SkillResolver] = None,
        *,
        allow_test_sandbox: bool = False,
        sandbox_image_resolver: Optional[Callable[[str, str], str]] = None,
    ):
        self.skill_registry = skill_registry
        self.allow_test_sandbox = allow_test_sandbox
        self.sandbox_image_resolver = sandbox_image_resolver

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
        coding = draft.coding if draft.coding and draft.coding.enabled else None
        if coding:
            if draft.harness_type != "deepagents":
                issues.append(
                    ValidationIssue(
                        "error",
                        "CODING_HARNESS_INVALID",
                        "Coding Agent requires the deepagents harness",
                        "harness_type",
                    )
                )
            if draft.harness_profile_revision_id != "coding-agent-v1":
                issues.append(
                    ValidationIssue(
                        "error",
                        "CODING_PROFILE_INVALID",
                        "Coding Agent must use harness profile coding-agent-v1",
                        "harness_profile_revision_id",
                    )
                )
            if not draft.capabilities.filesystem:
                issues.append(
                    ValidationIssue(
                        "error",
                        "CODING_FILESYSTEM_REQUIRED",
                        "Coding Agent requires filesystem capability",
                        "capabilities.filesystem",
                    )
                )
            if coding.delivery_mode != "patch_only":
                issues.append(
                    ValidationIssue(
                        "error",
                        "CODING_DELIVERY_UNAVAILABLE",
                        "The current Coding Agent runtime supports patch_only delivery",
                        "coding.delivery_mode",
                    )
                )
            if coding.sandbox.network_mode != "deny_by_default":
                issues.append(
                    ValidationIssue(
                        "error",
                        "CODING_NETWORK_POLICY_UNAVAILABLE",
                        "The current sandbox runtime supports deny_by_default networking only",
                        "coding.sandbox.network_mode",
                    )
                )
            if coding.sandbox.provider == "fake" and not self.allow_test_sandbox:
                issues.append(
                    ValidationIssue(
                        "error",
                        "FAKE_SANDBOX_TEST_ONLY",
                        "The fake sandbox provider is available only in tests",
                        "coding.sandbox.provider",
                    )
                )
            command_policy = SandboxPolicy(
                workspace_root=coding.sandbox.workspace_root,
                protected_paths=tuple(coding.protected_paths),
                delivery_mode=coding.delivery_mode,
                approval_mode=draft.policies.approval_mode,
            )
            for index, command in enumerate(
                coding.verification_policy.required_commands
            ):
                try:
                    command_policy.authorize_command(command)
                except SandboxPolicyError as exc:
                    issues.append(
                        ValidationIssue(
                            "error",
                            "CODING_VERIFICATION_COMMAND_DENIED",
                            str(exc),
                            f"coding.verification_policy.required_commands.{index}",
                        )
                    )
            supported_subagents = {
                "codebase-explorer",
                "code-reviewer",
                "test-diagnostician",
            }
            unknown_subagents = set(draft.capabilities.subagents) - supported_subagents
            if unknown_subagents:
                issues.append(
                    ValidationIssue(
                        "error",
                        "CODING_SUBAGENT_INVALID",
                        "Unsupported Coding SubAgent: " + ", ".join(sorted(unknown_subagents)),
                        "capabilities.subagents",
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
        coding = draft.coding if draft.coding and draft.coding.enabled else None
        model_capabilities = set(model_snapshot.get("capabilities") or [])
        if coding and "tool_calling" not in model_capabilities:
            raise ValueError("Coding Agent requires a model deployment with tool_calling")
        if coding and "streaming" not in model_capabilities:
            raise ValueError("Coding Agent requires a model deployment with streaming")
        if coding and int(model_snapshot.get("context_window_tokens") or 0) < 32_768:
            raise ValueError(
                "Coding Agent requires a model deployment context window of at least 32768 tokens"
            )
        skill_versions = (
            self.skill_registry.resolve_many(draft.capabilities.skills)
            if draft.capabilities.skills and self.skill_registry
            else []
        )
        prompt_hash = hashlib.sha256(draft.system_prompt.encode("utf-8")).hexdigest()
        coding_profile = coding.model_dump() if coding else None
        runtime_image = self.RUNTIME_IMAGE
        if coding_profile:
            sandbox = coding_profile["sandbox"]
            if sandbox.get("image_digest") == "sha256:unresolved":
                if self.sandbox_image_resolver is None:
                    raise ValueError(
                        "Coding sandbox image digest cannot be resolved by this control plane"
                    )
                resolved_digest = self.sandbox_image_resolver(
                    sandbox["provider"], sandbox["image"]
                ).removeprefix("sha256:")
                if len(resolved_digest) != 64 or any(
                    character not in "0123456789abcdef" for character in resolved_digest.lower()
                ):
                    raise ValueError("Sandbox provider returned an invalid image digest")
                sandbox["image_digest"] = "sha256:" + resolved_digest.lower()
            runtime_image = f"{sandbox['image']}@{sandbox['image_digest']}"
        tool_names = list(draft.capabilities.tools)
        if coding:
            tool_names = list(
                dict.fromkeys(
                    tool_names
                    + [
                        "ls",
                        "glob",
                        "grep",
                        "read_file",
                        "write_file",
                        "edit_file",
                        "execute",
                        "git_status",
                        "git_diff",
                        "verification_report",
                    ]
                )
            )
        resolved = {
            "agent_revision_id": revision_id,
            "harness_type": draft.harness_type,
            "harness_adapter_version": self.ADAPTER_VERSION,
            "harness_profile_revision_id": draft.harness_profile_revision_id,
            "package_versions": self.PACKAGE_VERSIONS,
            "runtime_image_digest": runtime_image,
            "model_deployment_revision_id": draft.model_deployment_id,
            "model_snapshot": model_snapshot,
            "prompt": draft.system_prompt,
            "prompt_hash": prompt_hash,
            "tool_bindings": [
                {
                    "name": name,
                    "schema_hash": hashlib.sha256(
                        json.dumps(
                            self.TOOL_SCHEMAS.get(name, {"tool": name}),
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                    "risk_level": (
                        "high"
                        if name in {"artifact_write", "shell_execute"}
                        else "medium"
                        if name in {"write_file", "edit_file", "execute"}
                        else "low"
                    ),
                }
                for name in tool_names
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
            "coding_profile": coding_profile,
            "sandbox_profile_revision": coding_profile.get("sandbox") if coding_profile else None,
            "repository_access_policy_revision": (
                coding_profile.get("repository_policy_revision_id") if coding_profile else None
            ),
            "event_adapter_version": "deepagents-events-1.0.0",
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
