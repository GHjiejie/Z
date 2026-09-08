from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import hmac
import io
import json
import os
import sqlite3
import ssl
import tarfile
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope as ASGIScope, Send

from packages.coding.errors import SandboxUnavailableError
from packages.coding.models import SandboxProfileSpec
from packages.sandbox.docker_provider import DockerSandboxProvider
from packages.sandbox.ports import SandboxProvider, SandboxProvisionRequest, SandboxSnapshot
from packages.sandbox.recovery_archive import normalize_recovery_archive
from packages.sandbox.remote_provider import _policy_digest
from packages.secrets import read_secret
from packages.persistence.fencing import LeaseLostError
from packages.sandbox.lease_authority import CancellationLease, ExecutionLease, LeaseAuthority, PostgresLeaseAuthority, SandboxExecutionGate


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    project_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    thread_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    workspace_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_base64: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    base_commit_sha: str = Field(min_length=1, max_length=128)


class Provision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
    scope: Scope
    profile: dict[str, Any]
    policy_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: Source


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(min_length=1, max_length=128_000)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)


class UploadFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    content_base64: str = Field(max_length=14_000_000)


class UploadFiles(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: list[UploadFile] = Field(max_length=100)


class DownloadFiles(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paths: list[str] = Field(max_length=100)


class GlobFiles(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pattern: str = Field(min_length=1, max_length=1000)
    path: str = Field(default="/workspace/repo", max_length=4096)


class InterruptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: str | None = Field(default=None, max_length=256)


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_base64: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: ASGIScope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        received = 0
        response_started = False

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(413, "Sandbox request body is too large")
            return message

        async def bounded_send(message: Message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, bounded_receive, bounded_send)
        except HTTPException as exc:
            if response_started:
                raise
            await JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})(
                scope, receive, send
            )


def normalize_source_archive(content: bytes, *, max_unpacked_bytes: int) -> bytes:
    """Create a bounded plain tar containing only normalized regular files.

    Streaming parsing avoids unbounded gzip decompression, and re-serialization
    prevents trailing compressed data or archive metadata from reaching Docker.
    """
    target = io.BytesIO()
    total = 0
    count = 0
    names: set[str] = set()

    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream
            self.remaining = max_unpacked_bytes + 100_000 * 512 + 10_240

        def read(self, size=-1):
            bounded_size = min(size, self.remaining + 1) if size >= 0 else self.remaining + 1
            chunk = self.stream.read(bounded_size)
            self.remaining -= len(chunk)
            if self.remaining < 0:
                raise ValueError("Source archive decompression exceeds the size limit")
            return chunk

    source_stream = (
        gzip.GzipFile(fileobj=io.BytesIO(content), mode="rb")
        if content.startswith(b"\x1f\x8b")
        else io.BytesIO(content)
    )
    try:
        with tarfile.open(fileobj=BoundedReader(source_stream), mode="r|") as source:
            with tarfile.open(fileobj=target, mode="w") as destination:
                for member in source:
                    count += 1
                    if count > 100_000:
                        raise ValueError("Source archive contains too many entries")
                    path = PurePosixPath(member.name)
                    if (
                        path.is_absolute()
                        or ".." in path.parts
                        or ".git" in path.parts
                        or not path.parts
                        or str(path) in names
                    ):
                        raise ValueError("Source archive contains an unsafe path")
                    names.add(str(path))
                    if member.isdir():
                        continue
                    if not member.isfile():
                        raise ValueError("Source archive may contain only regular files")
                    total += member.size
                    if member.size < 0 or total > max_unpacked_bytes:
                        raise ValueError("Source archive exceeds the unpacked size limit")
                    extracted = source.extractfile(member)
                    if extracted is None:
                        raise ValueError("Source archive entry is unavailable")
                    info = tarfile.TarInfo(str(path))
                    info.size = member.size
                    info.mode = 0o755 if member.mode & 0o111 else 0o644
                    info.uid = info.gid = 10001
                    destination.addfile(info, extracted)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ValueError("Source archive is invalid") from exc
    finally:
        source_stream.close()
    return target.getvalue()


def create_sandbox_service(
    *,
    provider: SandboxProvider | None = None,
    state_path: str | None = None,
    service_token: str | None = None,
    image: str | None = None,
    lease_authority: LeaseAuthority | None = None,
) -> FastAPI:
    configured_image = image or os.getenv(
        "DEEPAGENT_CODING_IMAGE", "deepagent/coding-runtime:0.1.0"
    )
    sandbox_provider = provider or DockerSandboxProvider(
        image=configured_image, auto_build=False, workspace_storage="tmpfs"
    )
    path = Path(state_path or os.getenv("SANDBOX_STATE_PATH", "/var/lib/deepagent/sandboxes.db"))
    max_archive_bytes = int(os.getenv("SANDBOX_MAX_ARCHIVE_BYTES", str(100 * 1024 * 1024)))
    max_file_bytes = int(os.getenv("SANDBOX_MAX_FILE_BYTES", "10000000"))
    provision_lock = asyncio.Lock()
    database: sqlite3.Connection | None = None
    token = service_token
    authority = lease_authority
    gate: SandboxExecutionGate | None = None

    def request_lease(request: Request) -> ExecutionLease | None:
        attempt = request.headers.get('x-execution-attempt')
        lease_token = request.headers.get('x-execution-token')
        if bool(attempt) != bool(lease_token) or len(attempt or '') > 256 or len(lease_token or '') > 256:
            raise HTTPException(400, 'Invalid execution lease headers')
        return ExecutionLease(attempt, lease_token) if attempt else None

    def db() -> sqlite3.Connection:
        if database is None:
            raise RuntimeError("Sandbox state is not initialized")
        return database

    def record(external_id: str) -> dict[str, Any]:
        row = db().execute(
            "SELECT * FROM sandboxes WHERE external_id=?", (external_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Sandbox not found")
        result = dict(row)
        if result["expires_at"] <= datetime.now(timezone.utc).isoformat():
            raise HTTPException(410, "Sandbox lease expired")
        result["profile"] = json.loads(result.pop("profile_json"))
        return result

    async def authorize(authorization: str = Header(default="")) -> None:
        expected = f"Bearer {token or ''}"
        if not token or not hmac.compare_digest(authorization, expected):
            raise HTTPException(401, "Sandbox service authentication required")

    async def cleanup() -> None:
        while True:
            rows = db().execute(
                "SELECT external_id FROM sandboxes WHERE expires_at<=?",
                (datetime.now(timezone.utc).isoformat(),),
            ).fetchall()
            for row in rows:
                try:
                    await sandbox_provider.destroy(row["external_id"])
                except SandboxUnavailableError:
                    continue
                gate.states.pop(row["external_id"], None)
                db().execute("DELETE FROM sandboxes WHERE external_id=?", (row["external_id"],))
                db().commit()
            await asyncio.sleep(60)

    async def cancel_background(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal database, token, authority, gate
        async with AsyncExitStack() as resources:
            token = token or read_secret(
                "DEEPAGENT_SANDBOX_SERVICE_TOKEN", required=True, production=True
            )
            if authority is None:
                authority = await asyncio.to_thread(
                    PostgresLeaseAuthority,
                    read_secret("SANDBOX_LEASE_DATABASE_URL", required=True, production=True),
                )
            resources.callback(authority.close)
            gate = SandboxExecutionGate(authority, sandbox_provider)
            path.parent.mkdir(parents=True, exist_ok=True)
            database = sqlite3.connect(path, check_same_thread=False)
            resources.callback(database.close)
            database.row_factory = sqlite3.Row
            database.execute("PRAGMA journal_mode=WAL")
            database.execute("PRAGMA busy_timeout=5000")
            database.execute(
                """CREATE TABLE IF NOT EXISTS sandboxes (
                    request_id TEXT PRIMARY KEY,
                    external_id TEXT NOT NULL UNIQUE,
                    policy_digest TEXT NOT NULL,
                    scope_digest TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    profile_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )"""
            )
            database.commit()
            resources.push_async_callback(gate.close)
            await gate.start()
            cleanup_task = asyncio.create_task(cleanup())
            resources.push_async_callback(cancel_background, cleanup_task)
            yield

    app = FastAPI(
        title="DeepAgent Isolated Sandbox Service",
        lifespan=lifespan,
        dependencies=[Depends(authorize)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(BodyLimitMiddleware, max_bytes=max_archive_bytes * 4 // 3 + 1024 * 1024)

    @app.exception_handler(LeaseLostError)
    async def lease_lost(_, exc):
        return JSONResponse(status_code=409, content={"error": "Execution lease is no longer valid"})

    @app.exception_handler(SandboxUnavailableError)
    async def unavailable(_, exc):
        return JSONResponse(status_code=503, content={"error": "Sandbox unavailable"})

    @app.get("/health")
    async def health():
        available = await sandbox_provider.available()
        return JSONResponse(
            status_code=200 if available else 503,
            content={"status": "healthy" if available else "unavailable"},
        )

    @app.get("/v1/images/resolve")
    async def resolve_image(image: str):
        if image != configured_image:
            raise HTTPException(403, "Image is not allowed on this sandbox host")
        resolver = getattr(sandbox_provider, "resolve_image_digest", None)
        if resolver is None:
            raise HTTPException(503, "Image digest resolver is unavailable")
        digest = await asyncio.to_thread(resolver, image)
        return {"digest": "sha256:" + str(digest).removeprefix("sha256:")}

    @app.post("/v1/sandboxes", status_code=201)
    async def provision(payload: Provision, request: Request):
        lease = request_lease(request)
        if lease is None:
            raise LeaseLostError('Sandbox provisioning requires an execution lease')
        authorized = await gate.validate(payload.request_id, lease)
        if authorized['workspace_id'] != payload.scope.workspace_id:
            raise LeaseLostError('Sandbox workspace scope does not match the lease')
        try:
            unknown = set(payload.profile) - set(SandboxProfileSpec.model_fields)
            if unknown:
                raise ValueError("Unknown sandbox profile fields")
            profile = SandboxProfileSpec.model_validate(payload.profile)
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, "Invalid sandbox profile") from exc
        if _policy_digest(payload.profile) != payload.policy_digest:
            raise HTTPException(422, "Policy digest mismatch")
        if (
            profile.provider != "remote"
            or profile.image != configured_image
            or profile.image_digest == "sha256:unresolved"
            or profile.user != "10001:10001"
            or profile.workspace_root != "/workspace/repo"
            or not profile.read_only_rootfs
            or profile.network_mode != "deny_by_default"
            or profile.network_allowlist
            or profile.cpu_limit > float(os.getenv("SANDBOX_MAX_CPUS", "4"))
            or profile.memory_mb > int(os.getenv("SANDBOX_MAX_MEMORY_MB", "8192"))
            or profile.disk_mb > int(os.getenv("SANDBOX_MAX_DISK_MB", "10240"))
            or profile.pids_limit > int(os.getenv("SANDBOX_MAX_PIDS", "256"))
            or profile.ttl_seconds > int(os.getenv("SANDBOX_MAX_TTL_SECONDS", "86400"))
        ):
            raise HTTPException(403, "Sandbox profile exceeds the host security policy")
        if len(payload.source.content_base64) > (max_archive_bytes * 4 // 3) + 4:
            raise HTTPException(413, "Source archive is too large")
        try:
            source = base64.b64decode(payload.source.content_base64, validate=True)
        except ValueError as exc:
            raise HTTPException(422, "Source archive encoding is invalid") from exc
        if hashlib.sha256(source).hexdigest() != payload.source.sha256:
            raise HTTPException(422, "Source archive digest mismatch")
        try:
            normalized = await asyncio.to_thread(
                normalize_source_archive,
                source,
                max_unpacked_bytes=min(profile.disk_mb * 1024 * 1024, max_archive_bytes * 5),
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        scope_digest = hashlib.sha256(
            json.dumps(payload.scope.model_dump(), sort_keys=True).encode()
        ).hexdigest()
        async with provision_lock:
            existing = db().execute(
                "SELECT * FROM sandboxes WHERE request_id=?", (payload.request_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["policy_digest"] != payload.policy_digest
                    or existing["scope_digest"] != scope_digest
                    or existing["source_sha256"] != payload.source.sha256
                ):
                    raise HTTPException(409, "Provision request identifier is already in use")
                item = record(existing["external_id"])
                await sandbox_provider.resume(item["external_id"], item["profile"])
                return {
                    "sandbox_id": item["external_id"],
                    "enforced_policy_digest": item["policy_digest"],
                    "source_sha256": item["source_sha256"],
                }
            count = db().execute("SELECT COUNT(*) FROM sandboxes").fetchone()[0]
            if count >= int(os.getenv("SANDBOX_MAX_INSTANCES", "32")):
                raise HTTPException(429, "Sandbox host capacity is exhausted")
            result = await sandbox_provider.provision(SandboxProvisionRequest(
                sandbox_instance_id=payload.request_id,
                tenant_id=payload.scope.tenant_hash,
                project_id=payload.scope.project_hash,
                thread_id=payload.scope.thread_hash,
                workspace_id=payload.scope.workspace_id,
                profile=profile.model_dump(),
                source_archive=normalized,
                source_sha256=hashlib.sha256(normalized).hexdigest(),
                base_commit_sha=payload.source.base_commit_sha,
            ))
            now = datetime.now(timezone.utc)
            try:
                await gate.validate(payload.request_id, lease)
                db().execute(
                    "INSERT INTO sandboxes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        payload.request_id, result.external_id, payload.policy_digest,
                        scope_digest, payload.source.sha256,
                        json.dumps(profile.model_dump(), sort_keys=True),
                        now.isoformat(), (now + timedelta(seconds=profile.ttl_seconds)).isoformat(),
                    ),
                )
                db().commit()
            except Exception:
                db().rollback()
                await sandbox_provider.destroy(result.external_id)
                raise
        return {
            "sandbox_id": result.external_id,
            "enforced_policy_digest": payload.policy_digest,
            "source_sha256": payload.source.sha256,
        }

    @app.get("/v1/sandboxes/{external_id}")
    async def status(external_id: str):
        item = record(external_id)
        await sandbox_provider.resume(external_id, item["profile"])
        return {"state": "ready", "enforced_policy_digest": item["policy_digest"]}

    @app.post("/v1/sandboxes/{external_id}/execute")
    async def execute(external_id: str, payload: Command, request: Request):
        item = record(external_id)
        if payload.timeout_seconds > item["profile"]["command_timeout_seconds"]:
            raise HTTPException(422, "Command timeout exceeds the sandbox policy")
        async with gate.operation(external_id, item["request_id"], request_lease(request), required=True):
            result = await sandbox_provider.resume(external_id, item["profile"])
            response = await gate.offload(
                external_id, item["request_id"], result.backend.execute,
                payload.command, timeout=payload.timeout_seconds,
            )
            return {
                "output": response.output, "exit_code": response.exit_code,
                "truncated": response.truncated,
                "resource_usage": getattr(result.backend, "last_resource_usage", {}),
            }

    @app.post("/v1/sandboxes/{external_id}/files")
    async def upload(external_id: str, payload: UploadFiles, request: Request):
        item = record(external_id)
        files = []
        total = 0
        for file in payload.files:
            try:
                content = base64.b64decode(file.content_base64, validate=True)
            except ValueError as exc:
                raise HTTPException(422, "File encoding is invalid") from exc
            total += len(content)
            if total > max_file_bytes:
                raise HTTPException(413, "File batch is too large")
            files.append((file.path, content))
        async with gate.operation(external_id, item["request_id"], request_lease(request), required=True):
            result = await sandbox_provider.resume(external_id, item["profile"])
            responses = await gate.offload(external_id, item["request_id"], result.backend.upload_files, files)
            return {"files": [{"path": entry.path, "error": entry.error} for entry in responses]}

    @app.post("/v1/sandboxes/{external_id}/files/download")
    async def download(external_id: str, payload: DownloadFiles, request: Request):
        item = record(external_id)
        async with gate.operation(external_id, item["request_id"], request_lease(request), required=False):
            result = await sandbox_provider.resume(external_id, item["profile"])
            responses = await gate.offload(external_id, item["request_id"], result.backend.download_files, payload.paths)
            if sum(len(entry.content or b"") for entry in responses) > max_file_bytes:
                raise HTTPException(413, "File batch is too large")
            return {"files": [{
                "path": entry.path,
                "content_base64": base64.b64encode(entry.content).decode() if entry.content is not None else None,
                "error": entry.error,
            } for entry in responses]}

    @app.post("/v1/sandboxes/{external_id}/files/glob")
    async def glob_files(external_id: str, payload: GlobFiles, request: Request):
        item = record(external_id)
        search_path = PurePosixPath(payload.path)
        root = PurePosixPath(item["profile"]["workspace_root"])
        if PurePosixPath(payload.pattern).is_absolute() or ".." in PurePosixPath(payload.pattern).parts:
            raise HTTPException(403, "Inspection pattern is outside the workspace")
        if not search_path.is_absolute() or ".." in search_path.parts or (
            search_path != root and root not in search_path.parents
        ):
            raise HTTPException(403, "Inspection path is outside the workspace")
        async with gate.operation(external_id, item["request_id"], request_lease(request), required=False):
            result = await sandbox_provider.resume(external_id, item["profile"])
            matches = await gate.offload(external_id, item["request_id"], result.backend.glob, payload.pattern, payload.path)
            return {"matches": matches.matches, "error": matches.error, "truncated": matches.truncated}

    @app.get("/v1/sandboxes/{external_id}/snapshot")
    async def snapshot(external_id: str, request: Request):
        item = record(external_id)
        async with gate.operation(external_id, item["request_id"], request_lease(request), required=False):
            # No new operation may enter while all background processes are
            # stopped and the repository archive is captured.
            async with gate.state(external_id, item["request_id"]).transition:
                await sandbox_provider.interrupt(external_id)
                result = await sandbox_provider.snapshot(external_id)
            if result.size_bytes > max_archive_bytes:
                raise HTTPException(413, "Workspace snapshot exceeds the transfer limit")
            return {"content_base64": base64.b64encode(result.content).decode(), "sha256": result.sha256}

    @app.post("/v1/sandboxes/{external_id}/cancel-capture")
    async def cancel_capture(external_id: str, request: Request):
        item = record(external_id)
        attempt = request.headers.get('x-cancellation-attempt')
        token = request.headers.get('x-cancellation-token')
        if not attempt or not token or max(len(attempt), len(token)) > 256 or request_lease(request):
            raise LeaseLostError('Cancellation capture requires a distinct finalization lease')
        lease = CancellationLease(attempt, token)
        async with gate.operation(external_id, item['request_id'], lease, required=True):
            result = await sandbox_provider.capture_cancellation(external_id, item['profile'])
            if result.snapshot.size_bytes > max_archive_bytes:
                raise HTTPException(413, 'Cancellation snapshot exceeds the transfer limit')
            return {'content_base64': base64.b64encode(result.snapshot.content).decode(),
                    'sha256': result.snapshot.sha256, 'changes': result.changes}

    @app.post("/v1/sandboxes/{external_id}/interrupt")
    async def interrupt(external_id: str, payload: InterruptRequest, request: Request):
        item = record(external_id)
        lease = request_lease(request)
        if lease:
            await gate.validate(item["request_id"], lease)
        interrupted = await gate.interrupt(external_id, item["request_id"], attempt_id=payload.attempt_id)
        return {"status": "interrupted" if interrupted else "superseded"}

    @app.get("/v1/sandboxes/{external_id}/recovery-snapshot")
    async def recovery_snapshot(external_id: str, request: Request):
        item = record(external_id)
        async with gate.operation(external_id, item["request_id"], request_lease(request), required=True):
            async with gate.state(external_id, item["request_id"]).transition:
                result = await sandbox_provider.recovery_snapshot(external_id)
            if result.size_bytes > max_archive_bytes:
                raise HTTPException(413, "Recovery snapshot exceeds the transfer limit")
            return {"content_base64": base64.b64encode(result.content).decode(), "sha256": result.sha256}

    @app.post("/v1/sandboxes/{external_id}/restore")
    async def restore(external_id: str, payload: RestoreRequest, request: Request):
        item = record(external_id)
        if len(payload.content_base64) > max_archive_bytes * 4 // 3 + 4:
            raise HTTPException(413, "Recovery snapshot exceeds the transfer limit")
        try:
            content = base64.b64decode(payload.content_base64, validate=True)
        except ValueError as exc:
            raise HTTPException(422, "Invalid recovery snapshot encoding") from exc
        if hashlib.sha256(content).hexdigest() != payload.sha256:
            raise HTTPException(422, "Recovery snapshot digest mismatch")
        await asyncio.to_thread(normalize_recovery_archive, content)
        async with gate.operation(external_id, item["request_id"], request_lease(request), required=True):
            async with gate.state(external_id, item["request_id"]).transition:
                await sandbox_provider.restore(external_id, SandboxSnapshot(content, payload.sha256, len(content)))
        return {"sha256": payload.sha256}

    @app.delete("/v1/sandboxes/{external_id}")
    async def destroy(external_id: str, request: Request):
        row = db().execute("SELECT * FROM sandboxes WHERE external_id=?", (external_id,)).fetchone()
        if row:
            provision_request = request.headers.get('x-provision-request')
            if provision_request is not None and provision_request != row['request_id']:
                raise HTTPException(409, 'Unpublished sandbox cleanup scope does not match')
            lease = request_lease(request)
            if lease:
                await gate.validate(row["request_id"], lease)
            async with gate.state(external_id, row["request_id"]).operation:
                async with gate.state(external_id, row["request_id"]).transition:
                    await sandbox_provider.destroy(external_id)
                    gate.states.pop(external_id, None)
                    db().execute("DELETE FROM sandboxes WHERE external_id=?", (external_id,))
                    db().commit()
        return {"status": "destroyed"}

    return app


def main() -> None:
    import uvicorn
    from packages.operations.logging import LOG_CONFIG, configure_logging
    configure_logging()

    certificate = os.environ["SANDBOX_TLS_CERT_FILE"]
    key = os.environ["SANDBOX_TLS_KEY_FILE"]
    ca = os.environ["SANDBOX_TLS_CLIENT_CA_FILE"]
    # A single controller owns each dedicated sandbox host. Horizontal scale is
    # achieved with additional hosts, not controllers sharing a Docker daemon.
    uvicorn.run(
        create_sandbox_service(),
        host=os.getenv("SANDBOX_LISTEN_HOST", "0.0.0.0"),
        port=int(os.getenv("SANDBOX_LISTEN_PORT", "8443")),
        ssl_certfile=certificate,
        ssl_keyfile=key,
        ssl_ca_certs=ca,
        ssl_cert_reqs=ssl.CERT_REQUIRED,
        proxy_headers=False,
        server_header=False,
        access_log=False,
        log_config=LOG_CONFIG,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import logging
        logging.getLogger(__name__).exception('Sandbox service failed')
        raise SystemExit(1) from None
