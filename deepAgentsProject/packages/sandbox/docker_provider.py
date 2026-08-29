from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import io
import tarfile
from pathlib import PurePosixPath
from typing import Any, Dict, Optional

import docker
from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox
from docker.errors import DockerException, ImageNotFound, NotFound

from packages.coding.errors import SandboxUnavailableError
from packages.sandbox.ports import (
    SandboxProvisionRequest,
    SandboxProvisionResult,
    SandboxSnapshot,
)


class DockerRawSandboxBackend(BaseSandbox):
    """Deep Agents backend backed by one isolated Docker container.

    This class contains no policy decisions. It is deliberately wrapped by
    GovernedSandboxBackend before being exposed to an agent.
    """

    enable_capture_offload = True

    def __init__(
        self,
        client: docker.DockerClient,
        container_id: str,
        *,
        workspace_root: str,
        default_timeout: int,
        max_output_bytes: int,
        disk_mb: int,
    ):
        self.client = client
        self.container_id = container_id
        self.workspace_root = workspace_root
        self.default_timeout = default_timeout
        self.max_output_bytes = max_output_bytes
        self.disk_mb = disk_mb
        self.last_resource_usage: Dict[str, Any] = {}

    @property
    def id(self) -> str:
        return self.container_id

    @property
    def container(self):
        return self.client.containers.get(self.container_id)

    def resolve_path(self, path: str) -> str:
        normalized = _container_path(path)
        result = self.container.exec_run(
            ["/usr/bin/realpath", "-m", "--", str(normalized)],
            workdir=self.workspace_root,
        )
        if result.exit_code:
            raise ValueError("Unable to resolve sandbox path")
        return result.output.decode("utf-8", errors="strict").strip()

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        requested_timeout = timeout if timeout is not None else self.default_timeout
        if requested_timeout <= 0:
            return ExecuteResponse(output="timeout must be positive", exit_code=2)

        before_usage = self._resource_snapshot()
        try:
            # GNU coreutils `timeout` runs inside the sandbox and kills the
            # command/process group. A client-side future timeout alone would
            # leave a runaway process alive in the container.
            result = self.container.exec_run(
                [
                    "/bin/sh",
                    "-lc",
                    (
                        "ulimit -f \"$((DISK_MB * 2048))\"; "
                        "/usr/bin/timeout --signal=TERM --kill-after=5s "
                        '"${COMMAND_TIMEOUT}s" /bin/sh -lc "$1"'
                    ),
                    "deepagent-command",
                    command,
                ],
                workdir=self.workspace_root,
                demux=True,
                environment={
                    "HOME": "/tmp/home",
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "LANG": "C.UTF-8",
                    "CI": "true",
                    "GIT_TERMINAL_PROMPT": "0",
                    "DISK_MB": str(self.disk_mb),
                    "COMMAND_TIMEOUT": str(requested_timeout),
                },
            )
        except TimeoutError:
            return ExecuteResponse(
                output=f"Command timed out after {requested_timeout} seconds",
                exit_code=124,
            )
        stdout, stderr = result.output if isinstance(result.output, tuple) else (result.output, b"")
        combined = (stdout or b"") + (stderr or b"")
        usage = self.container.exec_run(
            ["/bin/sh", "-lc", "du -sk /workspace/repo | cut -f1"],
            workdir=self.workspace_root,
        )
        try:
            used_kib = int((usage.output or b"0").decode().strip())
        except (ValueError, AttributeError):
            used_kib = 0
        after_usage = self._resource_snapshot()
        self.last_resource_usage = {
            "cpu_seconds": max(
                0.0,
                (after_usage["cpu_ns"] - before_usage["cpu_ns"]) / 1_000_000_000,
            ),
            "memory_bytes": after_usage["memory_bytes"],
            "memory_peak_bytes": after_usage["memory_peak_bytes"],
            "workspace_disk_bytes": used_kib * 1024,
        }
        if used_kib > self.disk_mb * 1024:
            return ExecuteResponse(
                output=(combined[: self.max_output_bytes]).decode("utf-8", errors="replace")
                + f"\nWorkspace disk budget exceeded: {used_kib} KiB > {self.disk_mb * 1024} KiB",
                exit_code=125,
                truncated=len(combined) > self.max_output_bytes,
            )
        truncated = len(combined) > self.max_output_bytes
        if truncated:
            combined = combined[: self.max_output_bytes]
        return ExecuteResponse(
            output=combined.decode("utf-8", errors="replace"),
            exit_code=result.exit_code,
            truncated=truncated,
        )

    def _resource_snapshot(self) -> Dict[str, int]:
        try:
            stats = self.container.stats(stream=False, one_shot=True)
        except (DockerException, NotFound):
            return {"cpu_ns": 0, "memory_bytes": 0, "memory_peak_bytes": 0}
        memory = stats.get("memory_stats") or {}
        return {
            "cpu_ns": int(
                ((stats.get("cpu_stats") or {}).get("cpu_usage") or {}).get(
                    "total_usage", 0
                )
            ),
            "memory_bytes": int(memory.get("usage") or 0),
            "memory_peak_bytes": int(memory.get("max_usage") or memory.get("usage") or 0),
        }

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        responses: list[FileUploadResponse] = []
        container = self.container
        for path, content in files:
            try:
                normalized = _container_path(path)
                if not any(
                    str(normalized).startswith(root + "/")
                    for root in ("/skills", "/artifacts", "/workspace")
                ):
                    raise RuntimeError("destination is not in a writable sandbox mount")
                parent = str(normalized.parent)
                mkdir = container.exec_run(
                    ["/bin/mkdir", "-p", "--", parent], workdir=self.workspace_root
                )
                if mkdir.exit_code:
                    raise RuntimeError("unable to create destination directory")
                # Docker's archive endpoint rejects tmpfs targets when the
                # container rootfs is read-only. Stream bounded base64 chunks
                # through short-lived exec environments instead; no host path
                # or long-lived credential is introduced.
                chunks = [content[offset : offset + 24_000] for offset in range(0, len(content), 24_000)]
                if not chunks:
                    chunks = [b""]
                for index, chunk in enumerate(chunks):
                    result = container.exec_run(
                        [
                            "/usr/local/bin/python",
                            "-c",
                            (
                                "import base64,os;"
                                "open(os.environ['UPLOAD_PATH'],os.environ['UPLOAD_MODE']).write("
                                "base64.b64decode(os.environ['UPLOAD_DATA']))"
                            ),
                        ],
                        workdir=self.workspace_root,
                        environment={
                            "UPLOAD_PATH": str(normalized),
                            "UPLOAD_MODE": "wb" if index == 0 else "ab",
                            "UPLOAD_DATA": base64.b64encode(chunk).decode("ascii"),
                        },
                    )
                    if result.exit_code:
                        raise RuntimeError(
                            "unable to write uploaded file: "
                            + (result.output or b"").decode(errors="replace")[:300]
                        )
                responses.append(FileUploadResponse(path=str(normalized), error=None))
            except Exception as exc:  # provider errors are returned per file by contract
                responses.append(FileUploadResponse(path=path, error=str(exc)))
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses: list[FileDownloadResponse] = []
        container = self.container
        for path in paths:
            try:
                normalized = _container_path(path)
                stream, _ = container.get_archive(str(normalized))
                archive = b"".join(stream)
                with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
                    member = next(item for item in tar.getmembers() if item.isfile())
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        raise RuntimeError("file archive was empty")
                    responses.append(
                        FileDownloadResponse(path=str(normalized), content=extracted.read(), error=None)
                    )
            except Exception as exc:
                responses.append(FileDownloadResponse(path=path, content=None, error=str(exc)))
        return responses


class DockerSandboxProvider:
    name = "docker"

    def __init__(
        self,
        *,
        image: str,
        dockerfile_root: Optional[str] = None,
        auto_build: bool = True,
        client: Optional[docker.DockerClient] = None,
    ):
        self.image = image
        self.dockerfile_root = dockerfile_root
        self.auto_build = auto_build
        self._client = client

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    async def available(self) -> bool:
        try:
            return bool(await asyncio.to_thread(self.client.ping))
        except DockerException:
            return False

    async def provision(self, request: SandboxProvisionRequest) -> SandboxProvisionResult:
        if not await self.available():
            raise SandboxUnavailableError("Docker daemon is unavailable")
        requested_image = request.profile.get("image", self.image)
        if requested_image != self.image:
            raise SandboxUnavailableError(
                f"Sandbox profile image {requested_image} is not configured on this provider"
            )
        await asyncio.to_thread(self._ensure_image)
        await asyncio.to_thread(
            self._verify_image_digest, request.profile.get("image_digest")
        )
        return await asyncio.to_thread(self._provision_sync, request)

    def resolve_image_digest(self, image: str) -> str:
        if image != self.image:
            raise SandboxUnavailableError(
                f"Sandbox image {image} is not configured on this provider"
            )
        self._ensure_image()
        return str(self.client.images.get(self.image).id).removeprefix("sha256:")

    def _ensure_image(self) -> None:
        try:
            self.client.images.get(self.image)
        except ImageNotFound:
            if not self.auto_build or not self.dockerfile_root:
                raise SandboxUnavailableError(f"Coding runtime image is unavailable: {self.image}")
            try:
                self.client.images.build(
                    path=self.dockerfile_root,
                    tag=self.image,
                    rm=True,
                    forcerm=True,
                    labels={"io.deepagent.runtime": "coding"},
                )
            except DockerException as exc:
                raise SandboxUnavailableError(f"Unable to build coding runtime image: {exc}") from exc

    def _verify_image_digest(self, expected: Any) -> None:
        if not expected:
            return
        expected_value = str(expected).removeprefix("sha256:")
        if expected_value == "unresolved":
            raise SandboxUnavailableError("Sandbox image digest was not resolved at publish time")
        image = self.client.images.get(self.image)
        accepted = {str(image.id).removeprefix("sha256:")}
        for repo_digest in image.attrs.get("RepoDigests") or []:
            if "@sha256:" in repo_digest:
                accepted.add(repo_digest.rsplit("@sha256:", 1)[1])
        if expected_value not in accepted:
            raise SandboxUnavailableError(
                "Configured sandbox image does not match the immutable execution plan"
            )

    def _provision_sync(self, request: SandboxProvisionRequest) -> SandboxProvisionResult:
        if hashlib.sha256(request.source_archive).hexdigest() != request.source_sha256:
            raise SandboxUnavailableError("Repository snapshot hash does not match provision request")
        profile = request.profile
        labels = {
            "io.deepagent.sandbox": "true",
            "io.deepagent.sandbox-id": request.sandbox_instance_id,
            "io.deepagent.workspace-id": request.workspace_id,
            "io.deepagent.tenant-hash": hashlib.sha256(request.tenant_id.encode()).hexdigest()[:16],
            "io.deepagent.project-hash": hashlib.sha256(request.project_id.encode()).hexdigest()[:16],
        }
        container = None
        volume = self.client.volumes.create(
            name=f"deepagent-{request.sandbox_instance_id}", labels=labels
        )
        try:
            self.client.containers.run(
                self.image,
                [
                    "/bin/sh",
                    "-lc",
                    "mkdir -p /workspace/repo && chown -R 10001:10001 /workspace",
                ],
                remove=True,
                user="0:0",
                network_disabled=True,
                read_only=True,
                cap_drop=["ALL"],
                cap_add=["CHOWN"],
                security_opt=["no-new-privileges:true"],
                volumes={volume.name: {"bind": "/workspace", "mode": "rw"}},
                labels=labels,
            )
            container = self.client.containers.run(
                self.image,
                [
                    "/bin/sh",
                    "-lc",
                    (
                        "mkdir -p /tmp/home /workspace/repo && "
                        "git config --global --add safe.directory /workspace/repo && "
                        "exec sleep infinity"
                    ),
                ],
                detach=True,
                name=f"deepagent-{request.sandbox_instance_id}",
                user=profile.get("user", "10001:10001"),
                working_dir=profile.get("workspace_root", "/workspace/repo"),
                environment={
                    "HOME": "/tmp/home",
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "LANG": "C.UTF-8",
                    "CI": "true",
                    "GIT_TERMINAL_PROMPT": "0",
                },
                network_disabled=profile.get("network_mode") == "deny_by_default",
                read_only=bool(profile.get("read_only_rootfs", True)),
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                pids_limit=int(profile.get("pids_limit", 256)),
                mem_limit=f"{int(profile.get('memory_mb', 4096))}m",
                nano_cpus=int(float(profile.get("cpu_limit", 2.0)) * 1_000_000_000),
                volumes={volume.name: {"bind": "/workspace", "mode": "rw"}},
                tmpfs={
                    "/tmp": "rw,nosuid,nodev,size=512m",
                    "/skills": "rw,nosuid,nodev,noexec,size=16m,uid=10001,gid=10001,mode=0755",
                    "/artifacts": "rw,nosuid,nodev,noexec,size=64m,uid=10001,gid=10001,mode=0755",
                },
                labels=labels,
            )
            archive = request.source_archive
            if archive[:2] == b"\x1f\x8b":
                archive = gzip.decompress(archive)
            if not container.put_archive("/workspace/repo", archive):
                raise SandboxUnavailableError("Unable to seed repository snapshot")
            backend = self._backend(container.id, profile)
            initialized = backend.execute(
                "git init -q && git config user.name 'DeepAgent Workspace' "
                "&& git config user.email 'workspace@deepagent.invalid' "
                "&& git add -A && git commit -q --allow-empty -m 'Repository snapshot'"
            )
            if initialized.exit_code:
                raise SandboxUnavailableError(
                    f"Unable to initialize sandbox repository: {initialized.output[:500]}"
                )
            # The agent needs Git metadata for status and diff, but patch_only
            # delivery must not let an arbitrary shell command rewrite HEAD,
            # refs, config, or the repository index. A short-lived privileged
            # initializer locks only .git; the long-running container keeps no
            # capabilities and remains non-root.
            self.client.containers.run(
                self.image,
                [
                    "/bin/sh",
                    "-lc",
                    "chown -R 0:0 /workspace/repo/.git && chmod -R a-w /workspace/repo/.git",
                ],
                remove=True,
                user="0:0",
                network_disabled=True,
                read_only=True,
                cap_drop=["ALL"],
                cap_add=["CHOWN", "FOWNER", "DAC_OVERRIDE"],
                security_opt=["no-new-privileges:true"],
                volumes={volume.name: {"bind": "/workspace", "mode": "rw"}},
                labels=labels,
            )
            image = self.client.images.get(self.image)
            return SandboxProvisionResult(
                external_id=container.id,
                backend=backend,
                metadata={
                    "volume": volume.name,
                    "image_id": image.id,
                    "source_sha256": request.source_sha256,
                    "source_commit_sha": request.base_commit_sha,
                },
            )
        except Exception:
            try:
                if container is not None:
                    container.remove(force=True)
            except Exception:
                pass
            try:
                volume.remove(force=True)
            except Exception:
                pass
            raise

    async def resume(self, external_id: str, profile: Dict[str, Any]) -> SandboxProvisionResult:
        try:
            container = await asyncio.to_thread(self.client.containers.get, external_id)
            container.reload()
            if container.status != "running":
                await asyncio.to_thread(container.start)
            return SandboxProvisionResult(
                external_id=container.id,
                backend=self._backend(container.id, profile),
                metadata={"resumed": True},
            )
        except (DockerException, NotFound) as exc:
            raise SandboxUnavailableError("Docker sandbox no longer exists") from exc

    async def snapshot(self, external_id: str) -> SandboxSnapshot:
        try:
            container = await asyncio.to_thread(self.client.containers.get, external_id)
            stream, _ = await asyncio.to_thread(container.get_archive, "/workspace/repo")
            content = await asyncio.to_thread(lambda: b"".join(stream))
            digest = hashlib.sha256(content).hexdigest()
            return SandboxSnapshot(content=content, sha256=digest, size_bytes=len(content))
        except (DockerException, NotFound) as exc:
            raise SandboxUnavailableError("Unable to snapshot Docker sandbox") from exc

    async def interrupt(self, external_id: str) -> None:
        """Terminate active exec processes without discarding workspace state."""
        try:
            container = await asyncio.to_thread(self.client.containers.get, external_id)
            await asyncio.to_thread(container.stop, timeout=1)
            await asyncio.to_thread(container.start)
        except (DockerException, NotFound) as exc:
            raise SandboxUnavailableError("Unable to interrupt Docker sandbox") from exc

    async def destroy(self, external_id: str) -> None:
        try:
            container = await asyncio.to_thread(self.client.containers.get, external_id)
        except NotFound:
            return
        mounts = container.attrs.get("Mounts", [])
        await asyncio.to_thread(container.remove, force=True)
        for mount in mounts:
            if mount.get("Type") == "volume" and str(mount.get("Name", "")).startswith("deepagent-"):
                try:
                    volume = await asyncio.to_thread(self.client.volumes.get, mount["Name"])
                    await asyncio.to_thread(volume.remove, force=True)
                except NotFound:
                    pass

    def _backend(self, container_id: str, profile: Dict[str, Any]) -> DockerRawSandboxBackend:
        return DockerRawSandboxBackend(
            self.client,
            container_id,
            workspace_root=profile.get("workspace_root", "/workspace/repo"),
            default_timeout=int(profile.get("command_timeout_seconds", 300)),
            max_output_bytes=int(profile.get("max_output_bytes", 200_000)),
            disk_mb=int(profile.get("disk_mb", 10240)),
        )


def _container_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Sandbox path must be absolute and normalized")
    return path
