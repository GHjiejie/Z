from __future__ import annotations

import base64
import hashlib
import json
import re
import ssl
from typing import Any, Dict
from urllib.parse import urlparse

import httpx
from deepagents.backends.protocol import (
    ExecuteResponse,
    GlobResult,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

from packages.coding.errors import SandboxUnavailableError
from packages.persistence.fencing import CancellationWriteFence, LeaseLostError, RunWriteFence, current_write_fence
from packages.sandbox.ports import (
    SandboxProvisionRequest,
    SandboxProvisionResult,
    SandboxSnapshot,
)


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def _execution_headers(*, required: bool = False) -> Dict[str, str]:
    fence = current_write_fence()
    if isinstance(fence, RunWriteFence):
        return {"X-Execution-Attempt": fence.attempt_id, "X-Execution-Token": fence.lease_token}
    if required:
        raise LeaseLostError("Remote sandbox mutations require an execution lease")
    return {}


def _policy_digest(profile: Dict[str, Any]) -> str:
    payload = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


class RemoteSandboxBackend(BaseSandbox):
    """Synchronous Deep Agents backend backed by an isolated sandbox service."""

    enable_capture_offload = True

    def __init__(
        self,
        external_id: str,
        *,
        base_url: str,
        headers: Dict[str, str],
        verify: ssl.SSLContext,
        timeout_seconds: float,
        max_response_bytes: int,
        default_command_timeout: int = 300,
        transport: httpx.BaseTransport | None = None,
    ):
        if not _IDENTIFIER.fullmatch(external_id):
            raise SandboxUnavailableError("Remote sandbox returned an invalid identifier")
        self._id = external_id
        self.max_response_bytes = max_response_bytes
        self.default_command_timeout = default_command_timeout
        self.last_resource_usage: Dict[str, Any] = {}
        self.client_options = dict(
            base_url=base_url,
            headers=headers,
            verify=verify,
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    @property
    def id(self) -> str:
        return self._id

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        _execution_headers(required=True)
        requested_timeout = timeout if timeout is not None else self.default_command_timeout
        if requested_timeout <= 0:
            return ExecuteResponse(output="timeout must be positive", exit_code=2)
        payload: Dict[str, Any] = {"command": command, "timeout_seconds": requested_timeout}
        data = self._request(
            "POST",
            f"/v1/sandboxes/{self.id}/execute",
            json=payload,
            timeout=requested_timeout + 15,
        )
        self.last_resource_usage = {
            key: value
            for key, value in (data.get("resource_usage") or {}).items()
            if key in {"cpu_seconds", "memory_bytes", "memory_peak_bytes", "workspace_disk_bytes"}
            and isinstance(value, (int, float))
        }
        output = str(data.get("output", ""))
        encoded = output.encode("utf-8")
        truncated = bool(data.get("truncated", False)) or len(encoded) > self.max_response_bytes
        if len(encoded) > self.max_response_bytes:
            output = encoded[: self.max_response_bytes].decode("utf-8", errors="replace")
        exit_code = data.get("exit_code")
        if exit_code is not None and not isinstance(exit_code, int):
            raise SandboxUnavailableError("Remote sandbox returned an invalid exit code")
        return ExecuteResponse(output=output, exit_code=exit_code, truncated=truncated)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        _execution_headers(required=True)
        if sum(len(content) for _, content in files) > self.max_response_bytes * 5:
            return [
                FileUploadResponse(path=path, error="upload batch exceeds transport limit")
                for path, _ in files
            ]
        data = self._request(
            "POST",
            f"/v1/sandboxes/{self.id}/files",
            json={
                "files": [
                    {"path": path, "content_base64": base64.b64encode(content).decode("ascii")}
                    for path, content in files
                ]
            },
        )
        return [
            FileUploadResponse(path=str(item.get("path", "")), error=item.get("error"))
            for item in self._items(data)
        ]

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        data = self._request(
            "POST", f"/v1/sandboxes/{self.id}/files/download", json={"paths": paths}
        )
        responses: list[FileDownloadResponse] = []
        total = 0
        for item in self._items(data):
            content = None
            encoded = item.get("content_base64")
            if encoded is not None:
                try:
                    content = base64.b64decode(str(encoded), validate=True)
                except ValueError as exc:
                    raise SandboxUnavailableError(
                        "Remote sandbox returned invalid file content"
                    ) from exc
                total += len(content)
                if total > self.max_response_bytes:
                    raise SandboxUnavailableError("Remote file response exceeds transport limit")
            responses.append(
                FileDownloadResponse(
                    path=str(item.get("path", "")),
                    content=content,
                    error=item.get("error"),
                )
            )
        return responses

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        data = self._request(
            "POST", f"/v1/sandboxes/{self.id}/files/glob",
            json={"pattern": pattern, "path": path or "/workspace/repo"},
        )
        return GlobResult(matches=data.get("matches"), error=data.get("error"), truncated=bool(data.get("truncated")))

    @staticmethod
    def _items(data: Dict[str, Any]) -> list[Dict[str, Any]]:
        items = data.get("files")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise SandboxUnavailableError("Remote sandbox returned an invalid file response")
        return items

    def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        chunks = bytearray()
        try:
            options = {**self.client_options, 'headers': {**self.client_options['headers'], **_execution_headers()}}
            with httpx.Client(**options) as client:
                with client.stream(method, path, **kwargs) as response:
                    if response.status_code == 409:
                        raise LeaseLostError('Remote sandbox rejected the execution lease')
                    response.raise_for_status()
                    for chunk in response.iter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > self.max_response_bytes * 2:
                            raise SandboxUnavailableError(
                                "Remote sandbox response exceeds transport limit"
                            )
        except httpx.HTTPError as exc:
            raise SandboxUnavailableError("Remote sandbox request failed") from exc
        try:
            data = json.loads(chunks)
        except ValueError as exc:
            raise SandboxUnavailableError("Remote sandbox returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise SandboxUnavailableError("Remote sandbox returned an invalid response")
        return data


class RemoteSandboxProvider:
    """Control-plane client for a separately deployed, mTLS sandbox service."""

    name = "remote"

    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        ca_file: str | None = None,
        client_cert_file: str | None = None,
        client_key_file: str | None = None,
        timeout_seconds: float = 30.0,
        max_response_bytes: int = 10_000_000,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
        require_https: bool = True,
    ):
        parsed = urlparse(base_url)
        if not parsed.netloc or parsed.scheme not in {"http", "https"}:
            raise ValueError("Remote sandbox URL must be absolute HTTP(S)")
        if require_https and parsed.scheme != "https":
            raise ValueError("Remote sandbox URL must use HTTPS")
        if not service_token:
            raise ValueError("Remote sandbox service token is required")
        if bool(client_cert_file) != bool(client_key_file):
            raise ValueError("Remote sandbox client certificate and key must be configured together")
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {service_token}",
            "Accept": "application/json",
            "User-Agent": "deepagent-control-plane/1",
        }
        self.verify = ssl.create_default_context(cafile=ca_file)
        if client_cert_file and client_key_file:
            self.verify.load_cert_chain(client_cert_file, client_key_file)
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.transport = transport

    async def available(self) -> bool:
        try:
            response = await self._async_request("GET", "/health")
            return response.get("status") in {"ok", "healthy"}
        except SandboxUnavailableError:
            return False

    async def provision(self, request: SandboxProvisionRequest) -> SandboxProvisionResult:
        _execution_headers(required=True)
        if hashlib.sha256(request.source_archive).hexdigest() != request.source_sha256:
            raise SandboxUnavailableError("Repository snapshot hash does not match provision request")
        expected_policy = _policy_digest(request.profile)
        data = await self._async_request(
            "POST",
            "/v1/sandboxes",
            json={
                "request_id": request.sandbox_instance_id,
                "scope": {
                    "tenant_hash": hashlib.sha256(request.tenant_id.encode()).hexdigest(),
                    "project_hash": hashlib.sha256(request.project_id.encode()).hexdigest(),
                    "thread_hash": hashlib.sha256(request.thread_id.encode()).hexdigest(),
                    "workspace_id": request.workspace_id,
                },
                "profile": request.profile,
                "policy_digest": expected_policy,
                "source": {
                    "content_base64": base64.b64encode(request.source_archive).decode("ascii"),
                    "sha256": request.source_sha256,
                    "base_commit_sha": request.base_commit_sha,
                },
            },
        )
        external_id = self._external_id(data)
        if data.get("enforced_policy_digest") != expected_policy:
            raise SandboxUnavailableError("Remote sandbox did not attest the requested policy")
        if data.get("source_sha256") != request.source_sha256:
            raise SandboxUnavailableError("Remote sandbox did not attest the requested source")
        return SandboxProvisionResult(
            external_id=external_id,
            backend=self._backend(external_id, request.profile),
            metadata=dict(data.get("metadata") or {}),
        )

    async def resume(self, external_id: str, profile: Dict[str, Any]) -> SandboxProvisionResult:
        self._validate_id(external_id)
        expected_policy = _policy_digest(profile)
        data = await self._async_request("GET", f"/v1/sandboxes/{external_id}")
        if data.get("state") not in {"ready", "running"}:
            raise SandboxUnavailableError("Remote sandbox is not ready")
        if data.get("enforced_policy_digest") != expected_policy:
            raise SandboxUnavailableError("Remote sandbox policy no longer matches the plan")
        return SandboxProvisionResult(
            external_id=external_id,
            backend=self._backend(external_id, profile),
            metadata={"resumed": True, **dict(data.get("metadata") or {})},
        )

    async def snapshot(self, external_id: str) -> SandboxSnapshot:
        return await self._snapshot(external_id, "snapshot")

    async def recovery_snapshot(self, external_id: str) -> SandboxSnapshot:
        return await self._snapshot(external_id, "recovery-snapshot")

    async def capture_cancellation(self, external_id: str, profile: Dict[str, Any]):
        from packages.sandbox.cancellation_capture import CancellationCapture, validate_capture
        self._validate_id(external_id)
        fence = current_write_fence()
        if not isinstance(fence, CancellationWriteFence):
            raise LeaseLostError('Cancellation capture requires its own finalization lease')
        data = await self._async_request('POST', f'/v1/sandboxes/{external_id}/cancel-capture',
            headers={'X-Cancellation-Attempt': fence.attempt_id, 'X-Cancellation-Token': fence.lease_token},
            timeout=120)
        try:
            content = base64.b64decode(data['content_base64'], validate=True)
            result = CancellationCapture(SandboxSnapshot(content, data['sha256'], len(content)), data['changes'])
            validate_capture(result)
        except (KeyError, ValueError, TypeError) as exc:
            raise SandboxUnavailableError('Remote cancellation capture is invalid') from exc
        return result

    async def _snapshot(self, external_id: str, endpoint: str) -> SandboxSnapshot:
        self._validate_id(external_id)
        data = await self._async_request("GET", f"/v1/sandboxes/{external_id}/{endpoint}")
        try:
            content = base64.b64decode(str(data["content_base64"]), validate=True)
        except (KeyError, ValueError) as exc:
            raise SandboxUnavailableError("Remote sandbox returned an invalid snapshot") from exc
        if len(content) > self.max_response_bytes * 100:
            raise SandboxUnavailableError("Remote sandbox snapshot exceeds transport limit")
        digest = hashlib.sha256(content).hexdigest()
        if data.get("sha256") != digest:
            raise SandboxUnavailableError("Remote sandbox snapshot digest mismatch")
        return SandboxSnapshot(content=content, sha256=digest, size_bytes=len(content))

    async def restore(self, external_id: str, snapshot: SandboxSnapshot) -> None:
        self._validate_id(external_id)
        _execution_headers(required=True)
        if len(snapshot.content) != snapshot.size_bytes or hashlib.sha256(snapshot.content).hexdigest() != snapshot.sha256:
            raise SandboxUnavailableError("Recovery archive digest mismatch")
        result = await self._async_request("POST", f"/v1/sandboxes/{external_id}/restore", json={
            "content_base64": base64.b64encode(snapshot.content).decode("ascii"), "sha256": snapshot.sha256,
        })
        if result.get("sha256") != snapshot.sha256:
            raise SandboxUnavailableError("Remote sandbox did not attest the restored archive")

    async def interrupt(self, external_id: str) -> None:
        self._validate_id(external_id)
        await self._async_request("POST", f"/v1/sandboxes/{external_id}/interrupt", json={})

    async def interrupt_attempt(self, external_id: str, attempt_id: str) -> None:
        self._validate_id(external_id)
        await self._async_request(
            "POST", f"/v1/sandboxes/{external_id}/interrupt", json={"attempt_id": attempt_id},
        )

    async def destroy(self, external_id: str) -> None:
        self._validate_id(external_id)
        await self._async_request("DELETE", f"/v1/sandboxes/{external_id}")

    async def discard_unpublished(self, external_id: str, request_id: str) -> None:
        """Compensate our exact successful create whose platform commit failed.

        The service credential authorizes cleanup even after the execution lease
        is revoked; the immutable provision request must still match the target.
        Never expose this control-plane operation as an agent tool.
        """
        self._validate_id(external_id)
        self._validate_id(request_id)
        await self._async_request('DELETE', f'/v1/sandboxes/{external_id}',
            include_execution_lease=False, headers={'X-Provision-Request': request_id})

    def resolve_image_digest(self, image: str) -> str:
        with self._sync_client() as client:
            try:
                response = client.get("/v1/images/resolve", params={"image": image})
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise SandboxUnavailableError("Unable to resolve remote sandbox image") from exc
        digest = str(data.get("digest", "")).removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SandboxUnavailableError("Remote sandbox returned an invalid image digest")
        return digest

    async def _async_request(self, method: str, path: str, *, include_execution_lease=True, **kwargs: Any) -> Dict[str, Any]:
        limit = self.max_response_bytes * (100 if path.endswith(("/snapshot", "/recovery-snapshot", "/cancel-capture")) else 2)
        chunks = bytearray()
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers={**self.headers, **(_execution_headers() if include_execution_lease else {})},
                verify=self.verify,
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                async with client.stream(method, path, **kwargs) as response:
                    if response.status_code == 409:
                        raise LeaseLostError('Remote sandbox rejected the execution lease')
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > limit:
                            raise SandboxUnavailableError(
                                "Remote sandbox control response is too large"
                            )
        except httpx.HTTPError as exc:
            raise SandboxUnavailableError("Remote sandbox control request failed") from exc
        try:
            data = json.loads(chunks)
        except ValueError as exc:
            raise SandboxUnavailableError("Remote sandbox returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise SandboxUnavailableError("Remote sandbox returned an invalid response")
        return data

    def _sync_client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers=self.headers,
            verify=self.verify,
            timeout=self.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=self.transport if isinstance(self.transport, httpx.BaseTransport) else None,
        )

    def _backend(self, external_id: str, profile: Dict[str, Any]) -> RemoteSandboxBackend:
        return RemoteSandboxBackend(
            external_id,
            base_url=self.base_url,
            headers=self.headers,
            verify=self.verify,
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=self.max_response_bytes,
            default_command_timeout=int(profile.get("command_timeout_seconds", 300)),
            transport=self.transport if isinstance(self.transport, httpx.BaseTransport) else None,
        )

    @staticmethod
    def _external_id(data: Dict[str, Any]) -> str:
        external_id = str(data.get("sandbox_id", ""))
        RemoteSandboxProvider._validate_id(external_id)
        return external_id

    @staticmethod
    def _validate_id(external_id: str) -> None:
        if not _IDENTIFIER.fullmatch(external_id):
            raise SandboxUnavailableError("Remote sandbox identifier is invalid")
