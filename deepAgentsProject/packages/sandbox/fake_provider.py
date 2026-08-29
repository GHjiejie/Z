from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol

from packages.coding.errors import SandboxUnavailableError
from packages.sandbox.ports import (
    SandboxProvisionRequest,
    SandboxProvisionResult,
    SandboxSnapshot,
)


class FakeSandboxBackend(FilesystemBackend, SandboxBackendProtocol):
    """Test-only backend. Commands return injected results and are never executed."""

    def __init__(
        self,
        root_dir: Path,
        command_handler: Optional[Callable[[str], ExecuteResponse]] = None,
    ):
        super().__init__(root_dir=root_dir, virtual_mode=True)
        self._id = f"fake-{uuid.uuid4().hex[:12]}"
        self.command_handler = command_handler or (
            lambda command: ExecuteResponse(
                output=f"FAKE_SANDBOX: command not executed: {command}", exit_code=126
            )
        )

    @property
    def id(self) -> str:
        return self._id

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return self.command_handler(command)


class FakeSandboxProvider:
    name = "fake"

    def __init__(self, command_handler: Optional[Callable[[str], ExecuteResponse]] = None):
        self.command_handler = command_handler
        self._directories: Dict[str, tempfile.TemporaryDirectory[str]] = {}
        self._backends: Dict[str, FakeSandboxBackend] = {}

    async def available(self) -> bool:
        return True

    async def provision(self, request: SandboxProvisionRequest) -> SandboxProvisionResult:
        if hashlib.sha256(request.source_archive).hexdigest() != request.source_sha256:
            raise SandboxUnavailableError("Repository snapshot hash does not match provision request")
        temporary = tempfile.TemporaryDirectory(prefix="deepagent-fake-sandbox-")
        root = Path(temporary.name)
        workspace = root / "workspace" / "repo"
        workspace.mkdir(parents=True, exist_ok=True)
        archive = request.source_archive
        if archive[:2] == b"\x1f\x8b":
            archive = gzip.decompress(archive)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            for member in tar.getmembers():
                destination = (workspace / member.name).resolve()
                if not member.isfile() or workspace not in destination.parents:
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                extracted = tar.extractfile(member)
                if extracted is not None:
                    destination.write_bytes(extracted.read())
        backend = FakeSandboxBackend(root, self.command_handler)
        self._directories[backend.id] = temporary
        self._backends[backend.id] = backend
        return SandboxProvisionResult(external_id=backend.id, backend=backend)

    async def resume(self, external_id: str, profile: Dict[str, Any]) -> SandboxProvisionResult:
        backend = self._backends.get(external_id)
        if backend is None:
            raise SandboxUnavailableError("Fake sandbox no longer exists")
        return SandboxProvisionResult(external_id=external_id, backend=backend, metadata={"resumed": True})

    async def snapshot(self, external_id: str) -> SandboxSnapshot:
        temporary = self._directories.get(external_id)
        if temporary is None:
            raise SandboxUnavailableError("Fake sandbox no longer exists")
        workspace = Path(temporary.name) / "workspace" / "repo"
        target = io.BytesIO()
        with gzip.GzipFile(fileobj=target, mode="wb", mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as archive:
                for source in sorted(workspace.rglob("*")):
                    if not source.is_file():
                        continue
                    content_bytes = source.read_bytes()
                    info = tarfile.TarInfo(str(source.relative_to(workspace)))
                    info.size = len(content_bytes)
                    info.mode = source.stat().st_mode & 0o777
                    info.uid = 10001
                    info.gid = 10001
                    info.mtime = 0
                    archive.addfile(info, io.BytesIO(content_bytes))
        content = target.getvalue()
        return SandboxSnapshot(content, hashlib.sha256(content).hexdigest(), len(content))

    async def interrupt(self, external_id: str) -> None:
        if external_id not in self._backends:
            raise SandboxUnavailableError("Fake sandbox no longer exists")

    async def destroy(self, external_id: str) -> None:
        self._backends.pop(external_id, None)
        temporary = self._directories.pop(external_id, None)
        if temporary is not None:
            temporary.cleanup()
