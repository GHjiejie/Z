from __future__ import annotations

from typing import Any

__all__ = ["RunOrchestrator", "RunService"]


def __getattr__(name: str) -> Any:
    # Keep the package import-light: harness adapters need the event emitter,
    # while the orchestrator itself depends on those adapters.
    if name == "RunOrchestrator":
        from .orchestrator import RunOrchestrator

        return RunOrchestrator
    if name == "RunService":
        from .run_service import RunService

        return RunService
    raise AttributeError(name)
