from __future__ import annotations

import secrets
from typing import Any, Dict
from packages.auth.resource_access import ResourceAccess


class RuntimeBinder:
    """Binds per-run resources without mutating the immutable execution plan.

    Provider credentials remain inside the model gateway. No credential or
    exchangeable credential token is placed in the agent context or sandbox.
    """

    def __init__(self, db):
        self.db = db

    def bind(self, run: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
        if run.get("principal_verified") != 1 or not run.get("principal_user_id"):
            raise RuntimeError("Run has no verified runtime principal")
        user_id = run["principal_user_id"]
        roles = ResourceAccess(self.db).require_execution(run["id"]).roles
        if not isinstance(roles, list):
            raise RuntimeError("Run principal roles are invalid")
        return {
            "tenant_id": run["tenant_id"],
            "project_id": run["project_id"],
            "thread_id": run["thread_id"],
            "run_id": run["id"],
            "attempt_id": run["current_attempt_id"],
            "resolved_plan_id": run["resolved_plan_id"],
            "environment_id": run.get("principal_environment_id") or "env_development",
            "user_id": user_id,
            "roles": roles,
            "credential_handle": None,
            "credential_binding": "gateway_managed",
            "checkpoint_namespace": f"{run['tenant_id']}/{run['project_id']}/{run['thread_id']}",
            "store_namespace": f"{run['tenant_id']}/{run['project_id']}/{user_id}/{plan['agent_revision_id']}/memory",
            "model_endpoint_id": plan["model_deployment_revision_id"],
            "knowledge_handles": [
                {
                    "handle": f"retriever_{secrets.token_hex(6)}",
                    "revision_id": binding["revision_id"],
                    "access": binding.get("access", "read_only"),
                }
                for binding in plan.get("knowledge_bindings", [])
            ],
            "sandbox_instance_id": None,
            "feature_flags": {
                "reference_harness": True,
                "skills_enabled": bool(plan.get("skill_versions")),
                "knowledge_enabled": bool(plan.get("knowledge_bindings")),
            },
        }
