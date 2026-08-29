from __future__ import annotations

import gzip
import io
import tarfile

import pytest

from packages.coding.errors import SandboxPolicyError
from packages.coding.redaction import redact_text
from packages.adapters.harness.deepagents.governed_backend import GovernedSandboxBackend
from packages.sandbox.docker_provider import DockerSandboxProvider
from packages.sandbox.policy import SandboxPolicy
from packages.sandbox.ports import SandboxProvisionRequest


def test_path_and_command_policy_rejects_escape_and_delivery_side_effects():
    policy = SandboxPolicy(
        workspace_root="/workspace/repo",
        protected_paths=("/workspace/repo/.github/workflows/**",),
    )
    assert policy.authorize_path("/workspace/repo/src/app.py", "write").endswith("app.py")
    with pytest.raises(SandboxPolicyError):
        policy.authorize_path("/workspace/repo/../../etc/passwd", "read")
    with pytest.raises(SandboxPolicyError):
        policy.authorize_path("/etc/passwd", "read")
    with pytest.raises(SandboxPolicyError):
        policy.authorize_path("/skills/coding/SKILL.md", "write")
    with pytest.raises(SandboxPolicyError):
        policy.authorize_path("/workspace/repo/.git/config", "read")
    for command in [
        "git push origin main",
        "/usr/bin/git commit -m unsafe",
        "docker run alpine",
        "/usr/bin/curl https://example.com",
        "cat /proc/self/environ",
        "git -c user.name=agent commit --allow-empty -m unsafe",
        "/usr/bin/git --git-dir=/workspace/repo/.git push origin main",
        "gh pr create --fill",
        "python -m pip install untrusted-package",
        "npm install untrusted-package",
    ]:
        with pytest.raises(SandboxPolicyError):
            policy.authorize_command(command)
    no_approval = SandboxPolicy(
        workspace_root="/workspace/repo",
        protected_paths=("/workspace/repo/.github/workflows/**",),
        approval_mode="never",
    )
    with pytest.raises(SandboxPolicyError):
        no_approval.authorize_path(
            "/workspace/repo/.github/workflows/release.yml", "write"
        )


def test_redaction_removes_common_credentials():
    redacted = redact_text(
        "api_key=super-secret-value Authorization: Bearer abcdefghijklmnop "
        "AWS=AKIA1234567890ABCDEF openai=sk-abcdefghijklmnopqrstuvwxyz"
    )
    assert "super-secret-value" not in redacted
    assert "abcdefghijklmnop" not in redacted
    assert "AKIA1234567890ABCDEF" not in redacted
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in redacted
    assert redacted.count("[REDACTED]") == 4


def _source_archive() -> bytes:
    target = io.BytesIO()
    with gzip.GzipFile(fileobj=target, mode="wb", mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w") as archive:
            content = b"sandbox fixture\n"
            info = tarfile.TarInfo("README.md")
            info.size = len(content)
            info.mode = 0o644
            info.uid = 10001
            info.gid = 10001
            archive.addfile(info, io.BytesIO(content))
    return target.getvalue()


@pytest.mark.asyncio
async def test_docker_sandbox_is_non_root_secret_free_offline_and_bounded(monkeypatch):
    provider = DockerSandboxProvider(
        image="deepagent/coding-runtime:0.1.0",
        dockerfile_root="docker/coding-runtime",
        auto_build=True,
    )
    if not await provider.available():
        pytest.skip("Docker daemon is unavailable")
    monkeypatch.setenv("DEEPAGENT_HOST_SECRET", "must-not-enter-sandbox")
    archive = _source_archive()
    profile = {
        "provider": "docker",
        "image": "deepagent/coding-runtime:0.1.0",
        "user": "10001:10001",
        "cpu_limit": 1,
        "memory_mb": 512,
        "disk_mb": 8,
        "pids_limit": 64,
        "command_timeout_seconds": 10,
        "max_output_bytes": 20_000,
        "network_mode": "deny_by_default",
        "workspace_root": "/workspace/repo",
        "read_only_rootfs": True,
    }
    result = await provider.provision(
        SandboxProvisionRequest(
            sandbox_instance_id="sbx_pytest_isolation",
            tenant_id="tenant_test",
            project_id="project_test",
            thread_id="thread_test",
            workspace_id="workspace_test",
            profile=profile,
            source_archive=archive,
            source_sha256=__import__("hashlib").sha256(archive).hexdigest(),
            base_commit_sha="0" * 40,
        )
    )
    try:
        container = provider.client.containers.get(result.external_id)
        host_config = container.attrs["HostConfig"]
        assert host_config["ReadonlyRootfs"] is True
        assert host_config["PidsLimit"] == 64
        assert host_config["CapDrop"] == ["ALL"]
        assert container.attrs["Config"]["NetworkDisabled"] is True
        identity = result.backend.execute("id -u && id -g")
        assert identity.exit_code == 0
        assert identity.output.splitlines() == ["10001", "10001"]
        assert result.backend.execute("printenv DEEPAGENT_HOST_SECRET").exit_code != 0
        assert result.backend.execute("test ! -e /var/run/docker.sock").exit_code == 0
        assert result.backend.execute("test ! -e /Users").exit_code == 0
        assert result.backend.execute("git config agent.tamper true").exit_code != 0
        assert result.backend.execute("git commit --allow-empty -m tamper").exit_code != 0
        assert result.backend.execute("ln -s /etc outside").exit_code == 0

        class NoopEvents:
            def append(self, *args, **kwargs):
                return None

        governed = GovernedSandboxBackend(
            result.backend,
            policy=SandboxPolicy(
                workspace_root="/workspace/repo", protected_paths=()
            ),
            db=None,
            events=NoopEvents(),
            run={"id": "run", "tenant_id": "tenant", "project_id": "project"},
            workspace={"id": "workspace"},
        )
        with pytest.raises(SandboxPolicyError):
            governed.read("/workspace/repo/outside/passwd")
        network = result.backend.execute(
            "python -c \"import socket; socket.create_connection(('1.1.1.1', 80), 1)\"",
            timeout=4,
        )
        assert network.exit_code != 0
        timed_out = result.backend.execute("sleep 3", timeout=1)
        assert timed_out.exit_code == 124
        noisy = result.backend.execute("python -c \"print('x' * 30000)\"")
        assert noisy.truncated is True
        assert len(noisy.output.encode()) <= 20_000
        oversized = result.backend.execute(
            "dd if=/dev/zero of=large.bin bs=1M count=12 status=none", timeout=5
        )
        assert oversized.exit_code != 0
        snapshot = await provider.snapshot(result.external_id)
        assert snapshot.size_bytes > 0
        assert len(snapshot.sha256) == 64
    finally:
        await provider.destroy(result.external_id)
