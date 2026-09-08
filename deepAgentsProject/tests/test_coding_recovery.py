from __future__ import annotations

import asyncio
import hashlib
import io
import multiprocessing
import shutil
import tarfile
from pathlib import Path
from types import SimpleNamespace
from datetime import timedelta
from typing import TypedDict

import pytest
from langgraph.graph import StateGraph
from langgraph.types import Command, interrupt
from langchain_core.messages import AIMessage
from fastapi.testclient import TestClient

from packages.coding.errors import CodingConflictError, SandboxUnavailableError
from packages.content_security import ContentRejectedError
from packages.persistence.fencing import LeaseLostError, execution_scope
from packages.runtime.coding_recovery import CodingRecovery
from packages.runtime.deepagents_executor import DeepAgentsRuntimeExecutor
from packages.runtime.run_lease import RunLeaseManager
from packages.runtime.model_gateway import DeterministicModelGateway
from packages.sandbox.fake_provider import FakeSandboxProvider
from packages.sandbox.docker_provider import DockerSandboxProvider
from apps.platform_api.main import create_app
from packages.sandbox.recovery_archive import normalize_recovery_archive

from test_coding_agent import _coding_draft, _command_result, CountingCodingModel
from test_shared_archives import VersionedStore
from packages.persistence.archive_store import SharedArchiveStore
from test_runtime_concurrency import runtime


class ProbeState(TypedDict):
    written: bool
    result: str


class InjectedCrash(BaseException):
    """Emulate loss of the process, bypassing application exception recovery."""


def coding_run(runtime, tmp_path):
    client, services, _, _, _ = runtime
    source = tmp_path / "source"
    source.mkdir()
    services.repositories.allowed_local_roots.append(source.resolve())
    (source / "counter.txt").write_text("0\n")
    (source / "delete-me.txt").write_text("original source file")
    repository = client.post("/api/v1/repositories", json={
        "name": "Recovery source", "provider": "local_snapshot", "canonical_uri": str(source),
    })
    assert repository.status_code == 201, repository.text
    agent = client.post("/api/v1/agents", json={"name": "Recovery", "draft": _coding_draft()}).json()
    revision = client.post(f"/api/v1/agents/{agent['id']}/revisions:publish").json()["revision"]
    deployment = client.post("/api/v1/agent-deployments", json={
        "agent_revision_id": revision["id"], "environment": "development",
    }).json()
    thread = client.post("/api/v1/threads", json={
        "agent_deployment_id": deployment["id"], "title": "Recovery consistency",
        "workspace": {"repository_id": repository.json()["id"], "source_mode": "working_tree_snapshot"},
    })
    assert thread.status_code == 201, thread.text
    response = client.post(f"/api/v1/threads/{thread.json()['id']}/runs", json={"input": "Change files exactly once"})
    assert response.status_code == 202, response.text
    run = services.db.fetch_one("SELECT * FROM runs WHERE id=?", (response.json()["id"],))
    plan = services.db.fetch_one("SELECT * FROM resolved_execution_plans WHERE id=?", (run["resolved_plan_id"],))["plan"]
    return run, plan


def executor(services):
    return DeepAgentsRuntimeExecutor(
        services.db, services.events, "recovery-test", services.sandbox_manager,
        services.checkpointer, None, services.model_gateway.identity(),
    )


@pytest.mark.parametrize("fault", ["before_publish", "after_publish"])
def test_graph_and_files_recover_together_without_later_pending_writes(runtime, tmp_path, monkeypatch, fault):
    _, services, _, _, _ = runtime
    run, plan = coding_run(runtime, tmp_path)
    leases = RunLeaseManager(services.db, "recovery-test", lease_seconds=120)
    first = leases.claim(run["id"])
    publish = CodingRecovery._publish
    writes = []

    def crash_at_boundary(self, snapshot, phase, checkpoint_id):
        if snapshot["workspace_generation"] >= 2:
            if fault == "after_publish":
                publish(self, snapshot, phase, checkpoint_id)
            raise InjectedCrash()
        return publish(self, snapshot, phase, checkpoint_id)

    monkeypatch.setattr(CodingRecovery, "_publish", crash_at_boundary)

    def graph_for(bound):
        def write(_):
            writes.append(bound.sandbox_instance["id"])
            assert not bound.backend.edit("/workspace/repo/counter.txt", "0", "1").error
            assert not bound.backend.delete("/workspace/repo/delete-me.txt").error
            bound.backend.upload_files([("/artifacts/proof.bin", b"\x00\xffrecovery-proof")])
            bound.backend.raw.upload_files([("/tmp/tool-scratch.txt", b"scratch state")])
            return {"written": True}

        def read(state):
            assert state["written"]
            assert bound.backend.download_files(["/workspace/repo/counter.txt"])[0].content == b"1\n"
            assert bound.backend.download_files(["/workspace/repo/delete-me.txt"])[0].error
            assert bound.backend.download_files(["/workspace/repo/uncommitted.txt"])[0].error
            assert bound.backend.download_files(["/artifacts/proof.bin"])[0].content == b"\x00\xffrecovery-proof"
            assert bound.backend.raw.download_files(["/tmp/tool-scratch.txt"])[0].content == b"scratch state"
            return {"result": "consistent"}

        builder = StateGraph(ProbeState)
        builder.add_node("writer", write)
        builder.add_node("reader", read)
        builder.set_entry_point("writer")
        builder.add_edge("writer", "reader")
        builder.set_finish_point("reader")
        return builder.compile(checkpointer=bound.recovery.saver)

    async def scenario():
        with execution_scope(first):
            bound = await executor(services)._prepare(run, plan)
            config = {"configurable": {"thread_id": bound.recovery.session["graph_thread_id"]}}
            with pytest.raises(InjectedCrash):
                await graph_for(bound).ainvoke({"written": False, "result": ""}, config, durability="sync")
            # A checkpoint/pending write and mutation newer than the published
            # pair must not be loaded by the replacement worker.
            latest = await services.checkpointer.aget_tuple(config)
            await services.checkpointer.aput_writes(latest.config, [("result", "poison")], "late-task")
            bound.backend.upload_files([("/workspace/repo/counter.txt", b"999\n"),
                                        ("/workspace/repo/uncommitted.txt", b"partial operation")])
            bound.backend._record_change("*", "injected_partial", None, "later")
            previous_generation = bound.workspace["workspace_generation"]
        monkeypatch.setattr(CodingRecovery, "_publish", publish)
        leases.abandon(first)
        assert leases.recover(run["id"])
        second = leases.claim(run["id"])
        recovered_run = services.db.fetch_one("SELECT * FROM runs WHERE id=?", (run["id"],))
        with execution_scope(second):
            restored = await executor(services)._prepare(recovered_run, plan)
            recovered_config = {"configurable": {"thread_id": restored.recovery.session["graph_thread_id"]}}
            assert recovered_config != config
            imported = await services.checkpointer.aget_tuple(recovered_config)
            assert all(value != "poison" for _, _, value in imported.pending_writes)
            with pytest.raises(LeaseLostError):
                await services.checkpointer.aput_writes(latest.config, [("result", "wrong-session")], "foreign-attempt")
            state = await graph_for(restored).ainvoke(None, recovered_config, durability="sync")
            assert state["result"] == "consistent"
            assert len(writes) == (2 if fault == "before_publish" else 1)
            assert restored.workspace["workspace_generation"] == previous_generation + 1 + (2 if fault == "before_publish" else 0)
        with execution_scope(first), pytest.raises(LeaseLostError):
            await services.checkpointer.aput_writes(latest.config, [("result", "stale-owner")], "stale-task")
        leases.release(second)

    asyncio.run(scenario())


@pytest.mark.parametrize("bad_name,kind", [
    ("../escape", "file"), ("/absolute", "file"), ("workspace/repo/.git/config", "file"),
    ("workspace/repo/link", "symlink"), ("artifacts/device", "device"), ("other/file", "file"),
])
def test_recovery_archive_rejects_unsafe_entries_before_restore(bad_name, kind):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        member = tarfile.TarInfo(bad_name)
        if kind == "symlink":
            member.type, member.linkname = tarfile.SYMTYPE, "/outside"
        elif kind == "device":
            member.type = tarfile.CHRTYPE
        archive.addfile(member)
    with pytest.raises(SandboxUnavailableError):
        normalize_recovery_archive(output.getvalue())


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_recovery_provider_requires_quiescence_and_captures_mutable_mounts(background):
    class Container:
        attrs = {"HostConfig": {"Tmpfs": {}}}
        paused = False

        def pause(self):
            self.paused = True

        def unpause(self):
            self.paused = False

        def top(self, ps_args):
            assert self.paused and ps_args == "-eo pid,stat,args"
            return {"Processes": [["1", "S", "sleep infinity"]] + ([["20", "S", "background writer"]] if background else [])}

        def get_archive(self, path):
            assert self.paused
            output = io.BytesIO()
            name = Path(path).name
            with tarfile.open(fileobj=output, mode="w") as archive:
                root = tarfile.TarInfo(name)
                root.type = tarfile.DIRTYPE
                archive.addfile(root)
                member = tarfile.TarInfo(name + "/file.bin")
                member.size = 3
                archive.addfile(member, io.BytesIO(b"\x00\xffx"))
                if name == "repo":
                    git = tarfile.TarInfo("repo/.git/config")
                    archive.addfile(git)
            return [output.getvalue()], {}

    container = Container()
    provider = DockerSandboxProvider(image="test", client=SimpleNamespace(containers=SimpleNamespace(get=lambda _: container)))
    if background:
        with pytest.raises(SandboxUnavailableError, match="Background processes"):
            await provider.recovery_snapshot("owned-sandbox")
    else:
        snapshot = await provider.recovery_snapshot("owned-sandbox")
        assert hashlib.sha256(snapshot.content).hexdigest() == snapshot.sha256
        with tarfile.open(fileobj=io.BytesIO(snapshot.content), mode="r:") as archive:
            names = archive.getnames()
            assert {"workspace/repo/file.bin", "artifacts/file.bin", "tmp/file.bin"}.issubset(names)
            assert not any(".git" in name for name in names)
    assert not container.paused


def test_nested_approval_preserves_child_checkpoint_and_files(runtime, tmp_path):
    _, services, _, _, _ = runtime
    run, plan = coding_run(runtime, tmp_path)
    leases = RunLeaseManager(services.db, "recovery-test", lease_seconds=120)
    first = leases.claim(run["id"])
    preparations = []

    def graph_for(bound):
        def prepare(_):
            preparations.append(bound.sandbox_instance["id"])
            assert not bound.backend.write("/workspace/repo/child.txt", "prepared once").error
            return {"written": True}

        def approval(state):
            decision = interrupt({"action": "accept child result"})
            assert decision == "approved"
            assert state["written"]
            assert bound.backend.download_files(["/workspace/repo/child.txt"])[0].content == b"prepared once"
            return {"result": "approved"}

        child = StateGraph(ProbeState)
        child.add_node("prepare", prepare)
        child.add_node("approval", approval)
        child.set_entry_point("prepare")
        child.add_edge("prepare", "approval")
        child.set_finish_point("approval")
        parent = StateGraph(ProbeState)
        parent.add_node("child", child.compile())
        parent.set_entry_point("child")
        parent.set_finish_point("child")
        return parent.compile(checkpointer=bound.recovery.saver)

    async def scenario():
        with execution_scope(first):
            bound = await executor(services)._prepare(run, plan)
            config = {"configurable": {"thread_id": bound.recovery.session["graph_thread_id"]}}
            interrupted = await graph_for(bound).ainvoke({"written": False, "result": ""}, config, durability="sync")
            assert interrupted["__interrupt__"]
            point = await bound.recovery.capture("INTERRUPT")
            bound.backend.upload_files([("/workspace/repo/child.txt", b"unpublished change")])
        leases.abandon(first)
        assert leases.recover(run["id"])
        second = leases.claim(run["id"])
        recovered = services.db.fetch_one("SELECT * FROM runs WHERE id=?", (run["id"],))
        with execution_scope(second):
            restored = await executor(services)._prepare(recovered, plan)
            assert restored.recovery.source["point"]["id"] == point["id"]
            config = {"configurable": {"thread_id": restored.recovery.session["graph_thread_id"]}}
            result = await graph_for(restored).ainvoke(Command(resume="approved"), config, durability="sync")
            assert result["result"] == "approved"
            assert len(preparations) == 1
        leases.release(second)

    asyncio.run(scenario())


def test_finished_model_answer_survives_crash_without_an_extra_model_call(runtime, tmp_path, monkeypatch):
    client, services, _, _, _ = runtime
    services.sandbox_manager.providers["fake"].command_handler = _command_result
    run, plan = coding_run(runtime, tmp_path)
    leases = RunLeaseManager(services.db, "recovery-test", lease_seconds=120)
    first = leases.claim(run["id"])
    model = CountingCodingModel(responses=[AIMessage(content="Durable final answer")])
    coding = executor(services)
    coding.model = model
    original = CodingRecovery._publish

    def crash_after_answer(self, snapshot, phase, checkpoint_id):
        point = original(self, snapshot, phase, checkpoint_id)
        if model.calls:
            raise InjectedCrash()
        return point

    monkeypatch.setattr(CodingRecovery, "_publish", crash_after_answer)

    async def scenario():
        with execution_scope(first), pytest.raises(InjectedCrash):
            await coding.execute(run["id"])
        assert model.calls == 1
        monkeypatch.setattr(CodingRecovery, "_publish", original)
        leases.abandon(first)
        assert leases.recover(run["id"])
        second = leases.claim(run["id"])
        with execution_scope(second):
            await coding.execute(run["id"])
        result = services.db.fetch_one("SELECT * FROM runs WHERE id=?", (run["id"],))
        assert result["status"] == "SUCCEEDED", result["output"]
        assert result["output"] == "Durable final answer"
        assert model.calls == 1
        assert services.db.fetch_one("SELECT SUM(model_calls) AS calls FROM usage_ledger WHERE run_id=?", (run["id"],))["calls"] == 1
        logs = services.db.fetch_all("SELECT content, content_hash FROM artifacts WHERE run_id=? AND name LIKE ?", (run["id"], "command-%"))
        assert any("\\0" in log["content"] for log in logs)
        assert all("\x00" not in log["content"] and hashlib.sha256(log["content"].encode()).hexdigest() == log["content_hash"]
                   for log in logs)
        leases.release(second)

        # The next Run crashes during preparation, before its input reached any
        # durable graph checkpoint. The previous Run's completed state is not a
        # substitute for accepting and processing the new request.
        new = client.post(f"/api/v1/threads/{run['thread_id']}/runs", json={"input": "A genuinely new request"})
        assert new.status_code == 202, new.text
        new_run = services.db.fetch_one("SELECT * FROM runs WHERE id=?", (new.json()["id"],))
        preparing = leases.claim(new_run["id"])
        with execution_scope(preparing):
            await coding._prepare(new_run, plan)
        leases.abandon(preparing)
        assert leases.recover(new_run["id"])
        replacement = leases.claim(new_run["id"])
        model.responses = [AIMessage(content="New request answer")]
        with execution_scope(replacement):
            await coding.execute(new_run["id"])
        completed = services.db.fetch_one("SELECT * FROM runs WHERE id=?", (new_run["id"],))
        assert completed["status"] == "SUCCEEDED", completed["output"]
        assert completed["output"] == "New request answer"
        assert model.calls == 2
        leases.release(replacement)

    asyncio.run(scenario())


def test_old_approval_cannot_authorize_a_newly_captured_interrupt():
    coding = object.__new__(DeepAgentsRuntimeExecutor)
    calls = []
    coding._graph_input = lambda run: calls.append(run["id"]) or "approved-input"
    run = {"id": "run-one", "checkpoint": {"stage": "approval_resolved", "recovery_point_id": "old-pause"}}
    source = {"point": {"id": "new-pause", "run_id": "run-one", "phase": "INTERRUPT"}}
    assert coding._recovery_input(run, source) is None
    assert not calls
    source["point"]["id"] = "old-pause"
    assert coding._recovery_input(run, source) == "approved-input"
    run["checkpoint"]["stage"] = "input_received"
    assert coding._recovery_input(run, source) == "approved-input"


def test_recovery_uses_pinned_shared_snapshot_without_creator_local_files(runtime, tmp_path):
    _, services, _, _, _ = runtime
    storage = VersionedStore()
    shared = SharedArchiveStore(storage)
    services.sandbox_manager.archive_store = shared
    services.repositories.archive_store = shared
    run, plan = coding_run(runtime, tmp_path)
    leases = RunLeaseManager(services.db, "recovery-test", lease_seconds=120)
    owner = leases.claim(run["id"])

    async def scenario():
        with execution_scope(owner):
            bound = await executor(services)._prepare(run, plan)
            graph = StateGraph(int)
            graph.add_node("noop", lambda value: value)
            graph.set_entry_point("noop")
            graph.set_finish_point("noop")
            await graph.compile(checkpointer=bound.recovery.saver).ainvoke(1, {
                "configurable": {"thread_id": bound.recovery.session["graph_thread_id"]},
            }, durability="sync")
            key, version = storage.last
            storage.put_content(key, b"untrusted newer object version", "application/octet-stream")
            services.sandbox_manager.snapshot_root = tmp_path / "unrelated-node"
            services.sandbox_manager.snapshot_root.mkdir()
            recovery = CodingRecovery(services.db, services.events, services.sandbox_manager, services.checkpointer, run, plan)
            loaded = recovery.load()
            assert loaded["snapshot"]["archive_path"].startswith("snapshot-object://")
            assert loaded["content"] == storage.objects[key, version]
            original_scanner = services.sandbox_manager.content_scanner

            class RejectOnRestore:
                def scan(self, content, *, object_name):
                    raise ContentRejectedError("new signature rejects this archive")

            services.sandbox_manager.content_scanner = RejectOnRestore()
            with pytest.raises(CodingConflictError, match="content scanner"):
                recovery.load()
            services.sandbox_manager.content_scanner = original_scanner
            storage.objects[key, version] = b"corrupted pinned version"
            with pytest.raises(CodingConflictError, match="hash or size"):
                recovery.load()
        leases.release(owner)

    asyncio.run(scenario())


def _killable_coding_worker(database_url, run_id, connection):
    provider = FakeSandboxProvider()
    with TestClient(create_app(database_url, seed=False, load_env=False,
                              model_gateway=DeterministicModelGateway(), sandbox_providers=[provider])) as client:
        services = client.app.state.services
        run = services.db.fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
        plan = services.db.fetch_one("SELECT * FROM resolved_execution_plans WHERE id=?", (run["resolved_plan_id"],))["plan"]
        owner = RunLeaseManager(services.db, "killed-worker", lease_seconds=120).claim(run_id)

        async def execute():
            with execution_scope(owner):
                bound = await executor(services)._prepare(run, plan)

                def write(_):
                    assert not bound.backend.edit("/workspace/repo/counter.txt", "0", "1").error
                    assert not bound.backend.delete("/workspace/repo/delete-me.txt").error
                    bound.backend.upload_files([("/artifacts/proof.bin", b"\x00\xffprocess-proof")])
                    return {"written": True}

                async def wait_for_kill(_):
                    point = services.db.fetch_one(
                        "SELECT * FROM coding_recovery_points WHERE run_id=? ORDER BY sequence DESC LIMIT 1", (run_id,),
                    )
                    bound.backend.upload_files([("/workspace/repo/counter.txt", b"999\n"),
                                                ("/workspace/repo/partial.txt", b"partial")])
                    connection.send({"point_id": point["id"], "attempt_id": owner.attempt_id,
                                     "owned_directory": provider._directories[bound.backend.id].name})
                    await asyncio.Event().wait()

                builder = StateGraph(ProbeState)
                builder.add_node("writer", write)
                builder.add_node("reader", wait_for_kill)
                builder.set_entry_point("writer")
                builder.add_edge("writer", "reader")
                builder.set_finish_point("reader")
                graph = builder.compile(checkpointer=bound.recovery.saver)
                await graph.ainvoke({"written": False, "result": ""}, {
                    "configurable": {"thread_id": bound.recovery.session["graph_thread_id"]},
                }, durability="sync")

        asyncio.run(execute())


def test_killed_process_recovers_paired_files_on_a_new_worker(runtime, tmp_path):
    _, services, _, _, database_url = runtime
    run, plan = coding_run(runtime, tmp_path)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_killable_coding_worker, args=(database_url, run["id"], child))
    evidence = None
    process.start()
    try:
        assert parent.poll(25), f"worker did not reach the live crash point (exit={process.exitcode})"
        evidence = parent.recv()
        assert process.is_alive()
        process.kill()
        process.join(timeout=5)
        assert not process.is_alive() and process.exitcode != 0
        services.db.execute("UPDATE run_attempts SET expires_at=? WHERE id=?", (
            (services.db.current_time() - timedelta(seconds=1)).isoformat(), evidence["attempt_id"],
        ))
        leases = RunLeaseManager(services.db, "replacement-worker", lease_seconds=120)
        assert leases.recover(run["id"])
        owner = leases.claim(run["id"])
        recovered = services.db.fetch_one("SELECT * FROM runs WHERE id=?", (run["id"],))

        async def resume():
            with execution_scope(owner):
                bound = await executor(services)._prepare(recovered, plan)
                assert bound.recovery.source["point"]["id"] == evidence["point_id"]

                def already_completed(_):
                    pytest.fail("a committed file-writing step was executed twice")

                def read(state):
                    assert state["written"]
                    assert bound.backend.download_files(["/workspace/repo/counter.txt"])[0].content == b"1\n"
                    assert bound.backend.download_files(["/workspace/repo/delete-me.txt"])[0].error
                    assert bound.backend.download_files(["/workspace/repo/partial.txt"])[0].error
                    assert bound.backend.download_files(["/artifacts/proof.bin"])[0].content == b"\x00\xffprocess-proof"
                    return {"result": "recovered after SIGKILL"}

                builder = StateGraph(ProbeState)
                builder.add_node("writer", already_completed)
                builder.add_node("reader", read)
                builder.set_entry_point("writer")
                builder.add_edge("writer", "reader")
                builder.set_finish_point("reader")
                graph = builder.compile(checkpointer=bound.recovery.saver)
                result = await graph.ainvoke(None, {
                    "configurable": {"thread_id": bound.recovery.session["graph_thread_id"]},
                }, durability="sync")
                assert result["result"] == "recovered after SIGKILL"

        asyncio.run(resume())
        leases.release(owner)
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        parent.close()
        child.close()
        if evidence:
            # This unique temporary sandbox was created by our killed child;
            # SIGKILL deliberately prevents its TemporaryDirectory finalizer.
            owned = Path(evidence["owned_directory"])
            assert owned.name.startswith("deepagent-fake-sandbox-")
            if owned.is_dir():
                shutil.rmtree(owned)
