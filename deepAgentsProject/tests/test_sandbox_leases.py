from __future__ import annotations

import asyncio
import threading

import pytest

from packages.coding.errors import SandboxUnavailableError
from packages.persistence.fencing import LeaseLostError
from packages.sandbox.lease_authority import ExecutionLease, SandboxExecutionGate


class Authority:
    def __init__(self):
        self.row = {
            "attempt_id": "attempt-one", "lease_token": "secret-one",
            "lease_live": True, "run_status": "RUNNING", "workspace_id": "workspace",
        }
        self.unavailable = False

    def lookup(self, request_id):
        if self.unavailable:
            raise SandboxUnavailableError("authority unavailable")
        return dict(self.row) if request_id == "request" else None

    def close(self):
        pass


class ControllableProvider:
    def __init__(self):
        self.entered = threading.Event()
        self.stopped = threading.Event()
        self.finish = threading.Event()
        self.running = False
        self.interrupts = 0
        self.new_executions = 0

    async def interrupt(self, external_id):
        self.interrupts += 1
        if self.running:
            self.stopped.set()

    def old_command(self):
        self.running = True
        self.entered.set()
        assert self.stopped.wait(3), "old command was not stopped"
        assert self.finish.wait(3), "simulated transport did not drain"
        self.running = False
        return "old response"

    def new_command(self):
        assert not self.running, "replacement overlapped stale IO"
        self.new_executions += 1
        return "new response"


@pytest.mark.asyncio
async def test_new_owner_waits_for_revoked_command_io_to_drain():
    authority, provider = Authority(), ControllableProvider()
    gate = SandboxExecutionGate(authority, provider, interval=0.02)
    old = ExecutionLease("attempt-one", "secret-one")
    new = ExecutionLease("attempt-two", "secret-two")

    async def execute(lease, function):
        async with gate.operation("sandbox", "request", lease, required=True):
            return await gate.offload("sandbox", "request", function)

    await gate.start()
    old_task = asyncio.create_task(execute(old, provider.old_command))
    new_task = None
    try:
        assert await asyncio.to_thread(provider.entered.wait, 2)
        authority.row.update(attempt_id="attempt-two", lease_token="secret-two")
        new_task = asyncio.create_task(execute(new, provider.new_command))
        assert await asyncio.to_thread(provider.stopped.wait, 2)
        assert provider.new_executions == 0 and not new_task.done()
        provider.finish.set()
        with pytest.raises(LeaseLostError):
            await asyncio.wait_for(old_task, 2)
        assert await asyncio.wait_for(new_task, 2) == "new response"
        with pytest.raises(LeaseLostError):
            async with gate.operation("sandbox", "request", old, required=True):
                pytest.fail("stale execution was admitted")
        interruptions = provider.interrupts
        assert await gate.interrupt("sandbox", "request", attempt_id=old.attempt_id) is False
        assert provider.interrupts == interruptions
    finally:
        provider.finish.set()
        provider.stopped.set()
        await asyncio.gather(old_task, *( [new_task] if new_task else []), return_exceptions=True)
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["expired", "revoked", "authority_unavailable"])
async def test_lease_failure_stops_background_processes_and_fails_closed(failure):
    authority, provider = Authority(), ControllableProvider()
    gate = SandboxExecutionGate(authority, provider, interval=0.02)
    lease = ExecutionLease("attempt-one", "secret-one")
    await gate.start()
    try:
        async with gate.operation("sandbox", "request", lease, required=True):
            pass
        baseline = provider.interrupts
        if failure == "expired":
            authority.row["lease_live"] = False
        elif failure == "revoked":
            authority.row["lease_token"] = None
        else:
            authority.unavailable = True
        async with asyncio.timeout(2):
            while provider.interrupts == baseline:
                await asyncio.sleep(0.01)
        with pytest.raises((LeaseLostError, SandboxUnavailableError)):
            async with gate.operation("sandbox", "request", lease, required=True):
                pytest.fail("invalid execution was admitted")
    finally:
        await gate.close()


@pytest.mark.asyncio
async def test_cancelled_http_handler_still_drains_uncancellable_thread_io():
    authority, provider = Authority(), ControllableProvider()
    gate = SandboxExecutionGate(authority, provider)
    lease = ExecutionLease("attempt-one", "secret-one")

    async def execute(function):
        async with gate.operation("sandbox", "request", lease, required=True):
            return await gate.offload("sandbox", "request", function)

    old_task = asyncio.create_task(execute(provider.old_command))
    new_task = None
    try:
        assert await asyncio.to_thread(provider.entered.wait, 2)
        old_task.cancel()
        assert await asyncio.to_thread(provider.stopped.wait, 2)
        new_task = asyncio.create_task(execute(provider.new_command))
        await asyncio.sleep(0.03)
        assert not new_task.done()
        provider.finish.set()
        with pytest.raises(asyncio.CancelledError):
            await old_task
        assert await asyncio.wait_for(new_task, 2) == "new response"
    finally:
        provider.finish.set()
        provider.stopped.set()
        await asyncio.gather(old_task, *( [new_task] if new_task else []), return_exceptions=True)
        await gate.close()


@pytest.mark.asyncio
async def test_mutations_require_lease_and_restart_drains_previous_owner():
    authority, provider = Authority(), ControllableProvider()
    gate = SandboxExecutionGate(authority, provider)
    with pytest.raises(LeaseLostError):
        async with gate.operation("sandbox", "request", None, required=True):
            pytest.fail("unowned mutation was admitted")
    async with gate.operation("sandbox", "request", None, required=False):
        pass
    assert provider.interrupts == 0
    async with gate.operation("sandbox", "request", ExecutionLease("attempt-one", "secret-one"), required=True):
        pass
    assert provider.interrupts == 1
    await gate.close()


def mount_archive(root, filename, content=b"preserved", uid=10001):
    import io
    import tarfile

    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        directory = tarfile.TarInfo(root)
        directory.type = tarfile.DIRTYPE
        directory.uid = directory.gid = uid
        archive.addfile(directory)
        file = tarfile.TarInfo(f"{root}/{filename}")
        file.uid = file.gid = uid
        file.mode = 0o444
        file.size = len(content)
        archive.addfile(file, io.BytesIO(content))
    return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("capture_fails", [False, True])
async def test_interrupt_retains_skills_artifacts_and_git_ownership_or_stops_closed(capture_fails):
    import io
    import tarfile
    from types import SimpleNamespace
    from packages.sandbox.docker_provider import DockerSandboxProvider

    class Container:
        attrs = {"HostConfig": {"Tmpfs": {}}}
        labels = {"io.deepagent.workspace-storage": "tmpfs"}

        def __init__(self):
            self.actions = []
            self.restored = {}

        def pause(self):
            self.actions.append("pause")

        def unpause(self):
            self.actions.append("unpause")

        def stop(self, timeout):
            self.actions.append("stop")

        def start(self):
            self.actions.append("start")

        def get_archive(self, directory):
            if capture_fails and directory == "/skills":
                raise RuntimeError("capture interrupted")
            root = directory.rsplit("/", 1)[1]
            return iter([mount_archive(root, ".git/config" if root == "repo" else "fixture.txt", uid=0 if root == "repo" else 10001)]), {}

        def put_archive(self, directory, content):
            self.restored[directory] = content
            return True

    container = Container()
    provider = DockerSandboxProvider(image="test", client=SimpleNamespace(containers=SimpleNamespace(get=lambda _: container)))
    provider._restore_volatile = lambda target, directory, content: target.restored.__setitem__(directory, content)
    if capture_fails:
        with pytest.raises(SandboxUnavailableError, match="state could not be preserved"):
            await provider.interrupt("sandbox")
        assert container.actions[-1] == "stop"
        assert "start" not in container.actions
    else:
        await provider.interrupt("sandbox")
        assert container.actions == ["pause", "unpause", "stop", "start"]
        assert set(container.restored) == {"/workspace", "/skills", "/artifacts", "/tmp"}
        with tarfile.open(fileobj=io.BytesIO(container.restored["/workspace"])) as archive:
            assert archive.getmember("repo/.git/config").uid == 0
        for directory in ("/skills", "/artifacts", "/tmp"):
            with tarfile.open(fileobj=io.BytesIO(container.restored[directory])) as archive:
                assert archive.getnames() == ["fixture.txt"]
                assert archive.getmember("fixture.txt").uid == 10001
