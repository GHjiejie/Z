from __future__ import annotations

import hashlib
from typing import Any, Awaitable, Callable, Dict


class DeepAgentsHarnessAdapter:
    """Stable platform boundary for the Deep Agents harness.

    `build_factory` intentionally receives only an immutable plan. The real SDK
    integration can call `create_deep_agent()` inside the returned async factory;
    API controllers and application services never import the SDK.
    """

    adapter_version = "1.0.0"

    async def build_factory(
        self, plan: Dict[str, Any]
    ) -> Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]:
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
                else [],
                "skill_context": "\n\n".join(
                    f"## Skill: {skill['name']} ({skill['version']})\n{skill['instructions']}"
                    for skill in loaded_skills
                ),
            }

        return factory
