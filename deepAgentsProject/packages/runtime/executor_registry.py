from __future__ import annotations

from typing import Any, Protocol


class RuntimeExecutor(Protocol):
    async def execute(self, run_id: str) -> None: ...

    async def cancel(self, run_id: str) -> None: ...


class ExecutorRegistry:
    def __init__(
        self,
        reference: RuntimeExecutor,
        coding: RuntimeExecutor | None = None,
    ):
        self.reference = reference
        self.coding = coding

    def resolve(self, plan: dict[str, Any]) -> RuntimeExecutor:
        coding = plan.get("coding_profile") or {}
        if coding.get("enabled"):
            if plan.get("harness_profile_revision_id") != "coding-agent-v1":
                raise RuntimeError("Unsupported coding harness profile")
            if self.coding is None:
                raise RuntimeError(
                    "Coding Agent runtime is not configured with a tool-calling model"
                )
            return self.coding
        return self.reference
