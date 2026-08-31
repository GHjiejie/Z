from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from packages.sandbox.cancellation_capture import CancellationCapture

from deepagents.backends.protocol import SandboxBackendProtocol


@dataclass(frozen=True)
class SandboxProvisionRequest:
    sandbox_instance_id: str
    tenant_id: str
    project_id: str
    thread_id: str
    workspace_id: str
    profile: Dict[str, Any]
    source_archive: bytes
    source_sha256: str
    base_commit_sha: str


@dataclass(frozen=True)
class SandboxProvisionResult:
    external_id: str
    backend: SandboxBackendProtocol
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SandboxSnapshot:
    content: bytes
    sha256: str
    size_bytes: int


class SandboxProvider(Protocol):
    name: str

    async def available(self) -> bool: ...

    async def provision(self, request: SandboxProvisionRequest) -> SandboxProvisionResult: ...

    async def resume(
        self, external_id: str, profile: Dict[str, Any]
    ) -> SandboxProvisionResult: ...

    async def snapshot(self, external_id: str) -> SandboxSnapshot: ...

    async def recovery_snapshot(self, external_id: str) -> SandboxSnapshot: ...

    async def capture_cancellation(self, external_id: str, profile: Dict[str, Any]) -> CancellationCapture: ...

    async def restore(self, external_id: str, snapshot: SandboxSnapshot) -> None: ...

    async def interrupt(self, external_id: str) -> None: ...

    async def destroy(self, external_id: str) -> None: ...
