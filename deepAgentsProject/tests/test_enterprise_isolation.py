from __future__ import annotations

import base64
import hashlib
import json
import io
import struct
import tarfile
import secrets

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.sandbox_service.main import create_sandbox_service, normalize_source_archive
from packages.coding.models import SandboxProfileSpec
from packages.sandbox.fake_provider import FakeSandboxProvider
from packages.sandbox.docker_provider import DockerSandboxProvider

from packages.coding.errors import SandboxUnavailableError
from packages.content_security import (
    ClamAVContentScanner,
    ContentRejectedError,
    ContentScanError,
    create_content_scanner,
)
from packages.sandbox.ports import SandboxProvisionRequest
from packages.sandbox.remote_provider import RemoteSandboxProvider
from packages.persistence.fencing import RunWriteFence, execution_scope
from packages.secrets import SecretConfigurationError, read_secret


def _request(profile=None):
    content = b"source archive fixture"
    return SandboxProvisionRequest(
        sandbox_instance_id="sbx_contract",
        tenant_id="tenant_private",
        project_id="project_private",
        thread_id="thread_private",
        workspace_id="workspace_contract",
        profile=profile or {
            "provider": "remote",
            "image": "coding@sha256:" + "a" * 64,
            "network_mode": "deny_by_default",
            "command_timeout_seconds": 60,
        },
        source_archive=content,
        source_sha256=hashlib.sha256(content).hexdigest(),
        base_commit_sha="b" * 40,
    )


@pytest.mark.asyncio
async def test_remote_sandbox_contract_is_secret_free_and_policy_bound(monkeypatch):
    with execution_scope(RunWriteFence("run_contract", "attempt_contract", "worker_contract", "lease_contract")):
        monkeypatch.setenv("OPENAI_API_KEY", "host-model-secret")
        provisioned = {}
        calls = []

        def handle(request):
            assert request.headers["authorization"] == "Bearer sandbox-service-token"
            calls.append((request.method, request.url.path))
            body = json.loads(request.content) if request.content else {}
            path = request.url.path
            if path == "/health":
                return httpx.Response(200, json={"status": "healthy"})
            if path == "/v1/images/resolve":
                return httpx.Response(200, json={"digest": "sha256:" + "a" * 64})
            if path == "/v1/sandboxes":
                provisioned.update(body)
                return httpx.Response(201, json={
                    "sandbox_id": "remote-sbx-1",
                    "enforced_policy_digest": body["policy_digest"],
                    "source_sha256": body["source"]["sha256"],
                })
            if path.endswith("/execute"):
                assert body["timeout_seconds"] == 60
                return httpx.Response(200, json={
                    "output": "test passed", "exit_code": 0,
                    "resource_usage": {"cpu_seconds": 0.1, "memory_bytes": 1024},
                })
            if path.endswith("/files/download"):
                return httpx.Response(200, json={"files": [{
                    "path": body["paths"][0],
                    "content_base64": base64.b64encode(b"report").decode(),
                }]})
            if path.endswith("/files"):
                assert base64.b64decode(body["files"][0]["content_base64"]) == b"fixture"
                return httpx.Response(200, json={"files": [{"path": body["files"][0]["path"]}]})
            if path.endswith(("/snapshot", "/recovery-snapshot")):
                content = b"workspace archive"
                return httpx.Response(200, json={
                    "content_base64": base64.b64encode(content).decode(),
                    "sha256": hashlib.sha256(content).hexdigest(),
                })
            if path.endswith("/restore"):
                assert request.headers["x-execution-token"] == "lease_contract"
                assert hashlib.sha256(base64.b64decode(body["content_base64"])).hexdigest() == body["sha256"]
                return httpx.Response(200, json={"sha256": body["sha256"]})
            return httpx.Response(200, json={
                "state": "running", "enforced_policy_digest": provisioned["policy_digest"]
            })

        provider = RemoteSandboxProvider(
            base_url="https://sandbox.internal",
            service_token="sandbox-service-token",
            transport=httpx.MockTransport(handle),
        )
        assert await provider.available()
        assert provider.resolve_image_digest("coding:1") == "a" * 64
        request = _request()
        result = await provider.provision(request)
        serialized = json.dumps(provisioned)
        assert "host-model-secret" not in serialized
        assert "sandbox-service-token" not in serialized
        assert "tenant_private" not in serialized
        assert "project_private" not in serialized
        assert base64.b64decode(provisioned["source"]["content_base64"]) == request.source_archive
        assert result.backend.execute("pytest").exit_code == 0
        assert result.backend.last_resource_usage["cpu_seconds"] == 0.1
        assert result.backend.upload_files([("/workspace/repo/test.txt", b"fixture")])[0].error is None
        assert result.backend.download_files(["/artifacts/report.txt"])[0].content == b"report"
        assert (await provider.resume(result.external_id, request.profile)).external_id == result.external_id
        assert (await provider.snapshot(result.external_id)).content == b"workspace archive"
        recovery = await provider.recovery_snapshot(result.external_id)
        await provider.restore(result.external_id, recovery)
        await provider.interrupt(result.external_id)
        await provider.destroy(result.external_id)
        assert ("DELETE", "/v1/sandboxes/remote-sbx-1") in calls


@pytest.mark.asyncio
async def test_remote_sandbox_fails_closed_for_mismatch_redirect_and_unsafe_identifier():
    with execution_scope(RunWriteFence("run_contract", "attempt_contract", "worker_contract", "lease_contract")):
        with pytest.raises(ValueError, match="HTTPS"):
            RemoteSandboxProvider(base_url="http://sandbox.internal", service_token="test")

        provider = RemoteSandboxProvider(
            base_url="https://sandbox.internal",
            service_token="test",
            transport=httpx.MockTransport(lambda request: httpx.Response(
                200, json={"sandbox_id": "remote-sbx-1", "enforced_policy_digest": "wrong"}
            )),
        )
        with pytest.raises(SandboxUnavailableError, match="attest"):
            await provider.provision(_request())
        with pytest.raises(SandboxUnavailableError, match="identifier"):
            await provider.destroy("../different-service")

        redirects = []
        def redirect(request):
            redirects.append(str(request.url))
            return httpx.Response(302, headers={"Location": "https://attacker.example/secret"})
        provider.transport = httpx.MockTransport(redirect)
        assert not await provider.available()
        assert redirects == ["https://sandbox.internal/health"]


def test_file_secrets_have_precedence_and_production_rejects_inline(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMPLE_SECRET", "inline-secret")
    with pytest.raises(SecretConfigurationError, match="EXAMPLE_SECRET_FILE"):
        read_secret("EXAMPLE_SECRET", production=True)
    secret_file = tmp_path / "credential"
    secret_file.write_text("file-secret\n")
    secret_file.chmod(0o400)
    monkeypatch.setenv("EXAMPLE_SECRET_FILE", str(secret_file))
    assert read_secret("EXAMPLE_SECRET", production=True) == "file-secret"
    secret_file.chmod(0o644)
    with pytest.raises(SecretConfigurationError, match="group or other"):
        read_secret("EXAMPLE_SECRET", production=True)
    with pytest.raises(SecretConfigurationError, match="exceeds"):
        read_secret("EXAMPLE_SECRET", production=False, max_bytes=2)


def test_clamav_stream_framing_and_fail_closed(monkeypatch):
    class Socket:
        sent = bytearray()
        reply = b"stream: OK\0"
        closed = False

        def settimeout(self, timeout):
            assert 0 < timeout <= 15

        def sendall(self, data):
            self.sent.extend(data)

        def recv(self, limit):
            return self.reply

        def close(self):
            self.closed = True

    connection = Socket()
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: connection)
    scanner = ClamAVContentScanner(max_bytes=10)
    scanner.scan(b"abc", object_name="fixture")
    assert connection.sent == b"zINSTREAM\0" + struct.pack("!I", 3) + b"abc" + struct.pack("!I", 0)
    assert connection.closed
    connection.reply = b"stream: Eicar-Test-Signature FOUND\0"
    with pytest.raises(ContentRejectedError, match="Eicar"):
        scanner.scan(b"abc", object_name="fixture")
    connection.reply = b"stream: database unavailable ERROR\0"
    with pytest.raises(ContentScanError, match="invalid response"):
        scanner.scan(b"abc", object_name="fixture")
    with pytest.raises(ContentRejectedError, match="limit"):
        scanner.scan(b"x" * 11, object_name="fixture")


def test_production_requires_a_content_scanner(monkeypatch):
    monkeypatch.setenv("DEEPAGENT_CONTENT_SCANNER", "disabled")
    with pytest.raises(ContentScanError, match="required in production"):
        create_content_scanner(production=True)


def _tar(name="README.md", content=b"safe source", *, symlink=False):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo(name)
        if symlink:
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
        else:
            member.size = len(content)
        archive.addfile(member, io.BytesIO(content) if not symlink else None)
    return output.getvalue()


class ServiceTestLeaseAuthority:
    def lookup(self, request_id):
        if request_id == "sbx_service_test":
            return {
                "workspace_id": "workspace_test", "attempt_id": "attempt_contract",
                "lease_token": "lease_contract", "lease_live": True, "run_status": "RUNNING",
            }
        return None

    def close(self):
        pass


def test_sandbox_host_enforces_security_policy_and_idempotent_lifecycle(tmp_path):
    provider = FakeSandboxProvider()
    app = create_sandbox_service(
        provider=provider,
        state_path=str(tmp_path / "sandboxes.db"),
        service_token="controller-token",
        image="coding:test",
        lease_authority=ServiceTestLeaseAuthority(),
    )
    profile = SandboxProfileSpec(
        provider="remote", image="coding:test", image_digest="sha256:" + "a" * 64,
        memory_mb=512, disk_mb=1024,
    ).model_dump()
    source = _tar()
    payload = {
        "request_id": "sbx_service_test",
        "scope": {
            "tenant_hash": "a" * 64, "project_hash": "b" * 64,
            "thread_hash": "c" * 64, "workspace_id": "workspace_test",
        },
        "profile": profile,
        "policy_digest": hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "source": {
            "content_base64": base64.b64encode(source).decode(),
            "sha256": hashlib.sha256(source).hexdigest(),
            "base_commit_sha": "b" * 40,
        },
    }
    headers = {"Authorization": "Bearer controller-token", "X-Execution-Attempt": "attempt_contract", "X-Execution-Token": "lease_contract"}
    with TestClient(app) as client:
        assert client.get("/health").status_code == 401
        assert client.get("/health", headers=headers).status_code == 200
        created = client.post("/v1/sandboxes", headers=headers, json=payload)
        assert created.status_code == 201, created.text
        sandbox_id = created.json()["sandbox_id"]
        repeated = client.post("/v1/sandboxes", headers=headers, json=payload)
        assert repeated.status_code == 201, repeated.text
        assert repeated.json()["sandbox_id"] == sandbox_id
        assert len(provider._backends) == 1
        status = client.get(f"/v1/sandboxes/{sandbox_id}", headers=headers)
        assert status.json()["enforced_policy_digest"] == payload["policy_digest"]
        assert client.post(
            f"/v1/sandboxes/{sandbox_id}/execute",
            headers={"Authorization": "Bearer controller-token"},
            json={"command": "pytest", "timeout_seconds": 30},
        ).status_code == 409
        assert client.post(
            f"/v1/sandboxes/{sandbox_id}/execute",
            headers={**headers, "X-Execution-Token": "stale-token"},
            json={"command": "pytest", "timeout_seconds": 30},
        ).status_code == 409
        uploaded = client.post(
            f"/v1/sandboxes/{sandbox_id}/files", headers=headers,
            json={"files": [{"path": "/workspace/repo/test.txt", "content_base64": "dGVzdA=="}]},
        )
        assert uploaded.status_code == 200, uploaded.text
        downloaded = client.post(
            f"/v1/sandboxes/{sandbox_id}/files/download", headers=headers,
            json={"paths": ["/workspace/repo/test.txt"]},
        )
        assert downloaded.json()["files"][0]["content_base64"] == "dGVzdA=="
        invalid_timeout = client.post(
            f"/v1/sandboxes/{sandbox_id}/execute", headers=headers,
            json={"command": "pytest", "timeout_seconds": 301},
        )
        assert invalid_timeout.status_code == 422
        snapshot = client.get(f"/v1/sandboxes/{sandbox_id}/snapshot", headers=headers)
        assert snapshot.status_code == 200
        content = base64.b64decode(snapshot.json()["content_base64"])
        assert hashlib.sha256(content).hexdigest() == snapshot.json()["sha256"]
        paired = client.get(f"/v1/sandboxes/{sandbox_id}/recovery-snapshot", headers=headers)
        assert paired.status_code == 200, paired.text
        assert client.get(f"/v1/sandboxes/{sandbox_id}/recovery-snapshot", headers={
            "Authorization": "Bearer controller-token",
        }).status_code == 409
        client.post(f"/v1/sandboxes/{sandbox_id}/files", headers=headers, json={"files": [
            {"path": "/workspace/repo/test.txt", "content_base64": "bGF0ZXI="},
            {"path": "/workspace/repo/partial.txt", "content_base64": "cGFydGlhbA=="},
        ]})
        assert client.post(f"/v1/sandboxes/{sandbox_id}/restore", headers={
            **headers, "X-Execution-Token": "expired",
        }, json=paired.json()).status_code == 409
        assert client.post(f"/v1/sandboxes/{sandbox_id}/restore", headers=headers,
                           json={**paired.json(), "sha256": "0" * 64}).status_code == 422
        restored = client.post(f"/v1/sandboxes/{sandbox_id}/restore", headers=headers, json=paired.json())
        assert restored.status_code == 200, restored.text
        assert restored.json()["sha256"] == paired.json()["sha256"]
        files = client.post(f"/v1/sandboxes/{sandbox_id}/files/download", headers=headers, json={
            "paths": ["/workspace/repo/test.txt", "/workspace/repo/partial.txt"],
        }).json()["files"]
        assert files[0]["content_base64"] == "dGVzdA=="
        assert files[1]["error"]
        unsafe = json.loads(json.dumps(payload))
        unsafe["profile"]["user"] = "0:0"
        unsafe["policy_digest"] = hashlib.sha256(json.dumps(unsafe["profile"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert client.post("/v1/sandboxes", headers=headers, json=unsafe).status_code == 403
        assert client.delete(f"/v1/sandboxes/{sandbox_id}", headers=headers).status_code == 200
        assert client.get(f"/v1/sandboxes/{sandbox_id}", headers=headers).status_code == 404
    assert not provider._backends


@pytest.mark.parametrize("archive", [_tar("../escape"), _tar("link", symlink=True), _tar(".git/config")])
def test_sandbox_host_rejects_unsafe_source_archives(archive):
    with pytest.raises(ValueError):
        normalize_source_archive(archive, max_unpacked_bytes=1024)


def test_sandbox_host_bounds_unpacked_archive_size():
    with pytest.raises(ValueError, match="unpacked size"):
        normalize_source_archive(_tar(content=b"x" * 1024), max_unpacked_bytes=512)


@pytest.mark.asyncio
async def test_docker_tmpfs_workspace_is_capacity_limited_and_survives_interrupt():
    provider = DockerSandboxProvider(
        image="deepagent/coding-runtime:0.1.0", auto_build=False, workspace_storage="tmpfs"
    )
    if not await provider.available():
        pytest.skip("Docker daemon is unavailable")
    source = _tar()
    profile = SandboxProfileSpec(
        image="deepagent/coding-runtime:0.1.0", memory_mb=512, disk_mb=128,
        cpu_limit=1, command_timeout_seconds=30,
    ).model_dump()
    profile["image_digest"] = "sha256:" + provider.resolve_image_digest(profile["image"])
    result = await provider.provision(SandboxProvisionRequest(
        sandbox_instance_id="sbx_pytest_tmpfs_capacity", tenant_id="tenant_test",
        project_id="project_test", thread_id="thread_test", workspace_id="workspace_test",
        profile=profile, source_archive=source,
        source_sha256=hashlib.sha256(source).hexdigest(), base_commit_sha="a" * 40,
    ))
    try:
        bounded = result.backend.execute(
            "dd if=/dev/zero of=/workspace/repo/quota-test.bin bs=1048576 count=200", timeout=20
        )
        assert bounded.exit_code != 0
        assert "space" in bounded.output.lower() or "file size" in bounded.output.lower()
        result.backend.execute("rm -f /workspace/repo/quota-test.bin")
        assert result.backend.upload_files([
            ("/workspace/repo/saved.txt", b"preserve across interrupt")
        ])[0].error is None
        await provider.interrupt(result.external_id)
        resumed = await provider.resume(result.external_id, profile)
        restored = resumed.backend.download_files(["/workspace/repo/saved.txt"])[0]
        assert restored.content == b"preserve across interrupt"
    finally:
        await provider.destroy(result.external_id)


@pytest.mark.asyncio
async def test_real_docker_paired_restore_preserves_files_scratch_and_git_baseline():
    provider = DockerSandboxProvider(
        image="deepagent/coding-runtime:0.1.0", auto_build=False, workspace_storage="tmpfs",
    )
    if not await provider.available():
        pytest.skip("Docker daemon is unavailable")
    profile = SandboxProfileSpec(
        image="deepagent/coding-runtime:0.1.0", memory_mb=512, disk_mb=128,
        cpu_limit=1, command_timeout_seconds=20,
    ).model_dump()
    profile["image_digest"] = "sha256:" + provider.resolve_image_digest(profile["image"])
    source = _tar()
    created = []

    async def provision():
        result = await provider.provision(SandboxProvisionRequest(
            sandbox_instance_id="sbx_recovery_" + secrets.token_hex(8), tenant_id="tenant_test",
            project_id="project_test", thread_id="thread_test", workspace_id="workspace_test",
            profile=profile, source_archive=source, source_sha256=hashlib.sha256(source).hexdigest(),
            base_commit_sha="a" * 40,
        ))
        created.append(result.external_id)
        return result

    try:
        original = await provision()
        mounts = {mount['Destination']: mount for mount in provider.client.containers.get(original.external_id).attrs['Mounts']}
        for directory, size in (('/tmp', 512), ('/skills', 16), ('/artifacts', 64)):
            assert mounts[directory]['Type'] == 'volume'
            options = provider.client.volumes.get(mounts[directory]['Name']).attrs['Options']
            assert options['type'] == 'tmpfs' and f'size={size}m' in options['o']
        changed = original.backend.execute("rm README.md && printf changed > changed.txt", timeout=10)
        assert changed.exit_code == 0, changed.output
        uploads = original.backend.upload_files([
            ("/artifacts/proof.bin", b"\x00\xffbinary-proof"), ("/tmp/scratch.txt", b"scratch-proof"),
        ])
        assert all(item.error is None for item in uploads), [(item.path, item.error) for item in uploads]
        paired = await provider.recovery_snapshot(original.external_id)
        background = original.backend.execute("sleep 30 >/dev/null 2>&1 &", timeout=5)
        assert background.exit_code == 0, background.output
        with pytest.raises(SandboxUnavailableError, match="Background processes"):
            await provider.recovery_snapshot(original.external_id)
        await provider.destroy(original.external_id)
        created.remove(original.external_id)
        replacement = await provision()
        await provider.restore(replacement.external_id, paired)
        files = replacement.backend.download_files([
            "/workspace/repo/README.md", "/workspace/repo/changed.txt", "/artifacts/proof.bin", "/tmp/scratch.txt",
        ])
        assert files[0].error
        assert [item.content for item in files[1:]] == [b"changed", b"\x00\xffbinary-proof", b"scratch-proof"]
        baseline = replacement.backend.execute("git show HEAD:README.md", timeout=10)
        assert baseline.exit_code == 0 and baseline.output == "safe source"
    finally:
        for external_id in reversed(created):
            await provider.destroy(external_id)
