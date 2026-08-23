from __future__ import annotations

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
        async def factory(runtime_context: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "harness": plan["harness_type"],
                "adapter_version": self.adapter_version,
                "plan_hash": plan["plan_hash"],
                "runtime_context": runtime_context,
            }

        return factory

