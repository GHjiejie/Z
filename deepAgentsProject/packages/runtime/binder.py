from __future__ import annotations

import secrets
from typing import Any, Dict


class RuntimeBinder:
    """Binds per-run resources without mutating the immutable execution plan.

    The reference build returns opaque handles. Production adapters can exchange
    them for short-lived credentials, MCP sessions, PostgreSQL checkpointers, and
    isolated sandboxes.
    """

    def bind(self, run: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "tenant_id": run["tenant_id"],
            "project_id": run["project_id"],
            "thread_id": run["thread_id"],
            "run_id": run["id"],
            "attempt_id": run["current_attempt_id"],
            "resolved_plan_id": run["resolved_plan_id"],
            "credential_handle": f"cred_ephemeral_{secrets.token_hex(4)}",
            "checkpoint_namespace": f"{run['tenant_id']}/{run['project_id']}/{run['thread_id']}",
            "store_namespace": f"{run['tenant_id']}/{run['project_id']}/user_demo/{plan['agent_revision_id']}/memory",
            "model_endpoint_id": plan["model_deployment_revision_id"],
            "sandbox_instance_id": None,
            "feature_flags": {
                "reference_harness": True,
                "skills_enabled": bool(plan.get("skill_versions")),
            },
        }

