from __future__ import annotations

from typing import Any, Dict

from packages.domain.models import TenantContext
from packages.knowledge.errors import KnowledgeConflictError
from packages.knowledge.models import KnowledgeSearchRequest
from packages.knowledge.service import KnowledgeService


class KnowledgeSearchTool:
    name = "knowledge_search"
    risk_level = "low"

    def __init__(self, service: KnowledgeService):
        self.service = service

    def invoke(
        self,
        query: str,
        plan: Dict[str, Any],
        runtime_context: Dict[str, Any],
        *,
        top_k: int = 8,
    ) -> Dict[str, Any]:
        required_principal = (
            "tenant_id",
            "project_id",
            "environment_id",
            "user_id",
            "roles",
        )
        if any(key not in runtime_context for key in required_principal) or not isinstance(
            runtime_context.get("roles"), list
        ):
            raise KnowledgeConflictError("Trusted runtime principal is missing or invalid")
        revision_ids = [
            binding["revision_id"] for binding in plan.get("knowledge_bindings", [])
        ]
        context = TenantContext(
            tenant_id=runtime_context["tenant_id"],
            project_id=runtime_context["project_id"],
            environment_id=runtime_context["environment_id"],
            user_id=runtime_context["user_id"],
            roles=runtime_context["roles"],
        )
        return self.service.search(
            KnowledgeSearchRequest(query=query, revision_ids=revision_ids, top_k=top_k),
            context,
            run_id=runtime_context.get("run_id"),
            expected_bindings=plan.get("knowledge_bindings", []),
        )
