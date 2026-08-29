from __future__ import annotations

import hashlib
from typing import Any, Awaitable, Callable, Dict

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver

from packages.adapters.harness.deepagents.coding_factory import build_coding_graph
from packages.adapters.harness.deepagents.governed_backend import GovernedSandboxBackend
from packages.knowledge.tool import KnowledgeSearchTool
from packages.persistence import Database


class DeepAgentsHarnessAdapter:
    """Stable platform boundary for the Deep Agents harness.

    `build_factory` intentionally receives only an immutable plan. The real SDK
    integration can call `create_deep_agent()` inside the returned async factory;
    API controllers and application services never import the SDK.
    """

    adapter_version = "2.0.0"

    def build_coding_graph(
        self,
        *,
        model: BaseChatModel,
        backend: GovernedSandboxBackend,
        plan: Dict[str, Any],
        skill_paths: list[str],
        checkpointer: BaseCheckpointSaver,
        db: Database,
        run_id: str,
        knowledge_tool: KnowledgeSearchTool | None = None,
        runtime_context: Dict[str, Any] | None = None,
    ):
        self._verified_skills(plan)
        return build_coding_graph(
            model=model,
            backend=backend,
            plan=plan,
            skill_paths=skill_paths,
            checkpointer=checkpointer,
            db=db,
            run_id=run_id,
            knowledge_tool=knowledge_tool,
            runtime_context=runtime_context,
        )

    async def build_factory(
        self, plan: Dict[str, Any]
    ) -> Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]:
        loaded_skills = self._verified_skills(plan)

        async def factory(runtime_context: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "harness": plan["harness_type"],
                "adapter_version": self.adapter_version,
                "plan_hash": plan["plan_hash"],
                "runtime_context": runtime_context,
                "skills": loaded_skills,
                "tools": [
                    {
                        "name": "knowledge_search",
                        "risk_level": "low",
                        "revision_ids": [
                            binding["revision_id"]
                            for binding in plan.get("knowledge_bindings", [])
                        ],
                    }
                ]
                if plan.get("knowledge_bindings")
                and any(
                    binding.get("name") == "knowledge_search"
                    for binding in plan.get("tool_bindings", [])
                )
                else [],
                "builtin_agents": plan.get("builtin_agent_bindings", []),
                "skill_context": "\n\n".join(
                    f"## Skill: {skill['name']} ({skill['version']})\n{skill['instructions']}"
                    for skill in loaded_skills
                ),
            }

        return factory

    @staticmethod
    def _verified_skills(plan: Dict[str, Any]) -> list[Dict[str, Any]]:
        loaded_skills = []
        for skill in plan.get("skill_versions", []):
            instructions = skill.get("instructions", "")
            actual_hash = hashlib.sha256(instructions.encode("utf-8")).hexdigest()
            if actual_hash != skill.get("artifact_hash"):
                raise RuntimeError(
                    f"Skill artifact hash mismatch for {skill.get('slug', skill.get('revision_id'))}"
                )
            loaded_skills.append(
                {
                    "revision_id": skill["revision_id"],
                    "slug": skill["slug"],
                    "name": skill["name"],
                    "version": skill["version"],
                    "artifact_hash": skill["artifact_hash"],
                    "instructions": instructions,
                }
            )

        return loaded_skills
