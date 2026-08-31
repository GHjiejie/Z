from __future__ import annotations

from typing import Any, Protocol
from copy import copy


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
        self.models = None

    def resolve(self, plan: dict[str, Any]) -> RuntimeExecutor:
        gateway = self.models.gateway(plan) if self.models else None
        coding = plan.get("coding_profile") or {}
        if coding.get("enabled"):
            if plan.get("harness_profile_revision_id") != "coding-agent-v1":
                raise RuntimeError("Unsupported coding harness profile")
            if self.coding is None:
                raise RuntimeError(
                    "Coding Agent runtime is not configured with a tool-calling model"
                )
            if self.models:
                executor = copy(self.coding)
                executor.model = self.models.coding_model(plan,gateway,executor.model)
                if executor.model is None:
                    raise RuntimeError("Coding model is not configured")
                executor.model_identity = gateway.identity()
                return executor
            return self.coding
        if self.models:
            executor = copy(self.reference)
            executor.model_gateway = gateway
            return executor
        return self.reference
