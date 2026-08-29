from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from apps.platform_api.main import create_app
from deepagents.backends.protocol import ExecuteResponse
from packages.runtime.model_gateway import DeterministicModelGateway
from packages.sandbox.docker_provider import DockerSandboxProvider
from packages.sandbox.fake_provider import FakeSandboxProvider


class ToolCallingFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, *args, **kwargs):
        return self


def _command_result(command: str) -> ExecuteResponse:
    if "git diff --binary" in command:
        return ExecuteResponse(
            output=(
                "diff --git a/coding-agent-test.txt b/coding-agent-test.txt\n"
                "new file mode 100644\nindex 0000000..45b983b\n"
                "--- /dev/null\n+++ b/coding-agent-test.txt\n@@ -0,0 +1 @@\n+implemented\n"
            ),
            exit_code=0,
        )
    if "git diff --numstat" in command:
        return ExecuteResponse(output="1\t0\tcoding-agent-test.txt\0", exit_code=0)
    if command.startswith("git status --porcelain"):
        return ExecuteResponse(output=" A coding-agent-test.txt\0", exit_code=0)
    if command == "test -f pyproject.toml -a -d tests":
        return ExecuteResponse(output="", exit_code=1)
    return ExecuteResponse(output="verification passed", exit_code=0)


def _wait(client: TestClient, run_id: str, expected: set[str], timeout: float = 8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/v1/runs/{run_id}").json()
        if run["status"] in expected:
            return run
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach {expected}")


def _wait_event(
    client: TestClient,
    run_id: str,
    event_type: str,
    *,
    timeout: float = 12,
    predicate=None,
):
    deadline = time.time() + timeout
    while time.time() < deadline:
        items = client.get(f"/api/v1/runs/{run_id}/events").json()["items"]
        for item in items:
            if item["type"] == event_type and (predicate is None or predicate(item)):
                return item
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not emit {event_type}")


def _coding_draft() -> dict:
    return {
        "harness_type": "deepagents",
        "harness_profile_revision_id": "coding-agent-v1",
        "model_deployment_id": "model_qwen_prod_v1",
        "system_prompt": "Implement the requested change and verify it.",
        "capabilities": {
            "tools": [],
            "mcp_servers": [],
            "skills": [
                "coding-workflow",
                "repository-safety",
                "test-and-verification",
                "change-delivery",
            ],
            "memories": [],
            "knowledge_bases": [],
            "subagents": [],
            "filesystem": True,
        },
        "policies": {
            "permission_policy": "coding-project-default-v1",
            "approval_mode": "high_risk",
            "audit_level": "strict",
        },
        "limits": {
            "max_duration_seconds": 120,
            "max_model_calls": 10,
            "max_tool_calls": 20,
            "max_subagent_depth": 1,
            "max_subagent_concurrency": 2,
            "max_sandbox_cpu_seconds": 120,
            "max_output_bytes": 1000000,
            "max_cost": 5,
        },
        "coding": {
            "enabled": True,
            "sandbox": {
                "provider": "fake",
                "image": "deepagent/coding-runtime:test",
                "image_digest": "sha256:test",
                "user": "10001:10001",
                "cpu_limit": 1,
                "memory_mb": 512,
                "disk_mb": 1024,
                "pids_limit": 64,
                "command_timeout_seconds": 30,
                "run_timeout_seconds": 120,
                "max_output_bytes": 200000,
                "network_mode": "deny_by_default",
                "workspace_root": "/workspace/repo",
                "read_only_rootfs": True,
                "lifecycle": "thread_scoped",
                "ttl_seconds": 3600,
            },
            "delivery_mode": "patch_only",
            "verification_policy": {
                "auto_discover": False,
                "required_commands": ["python -m pytest -q"],
                "max_attempts": 1,
                "command_timeout_seconds": 30,
                "require_success": True,
            },
            "protected_paths": ["/workspace/repo/.github/workflows/**"],
            "max_changed_files": 20,
            "max_diff_lines": 2000,
        },
    }


def _create_coding_thread(
    client: TestClient,
    draft: dict,
    *,
    repository_name: str = "coding-fixture",
) -> dict:
    project_root = Path(__file__).resolve().parents[1]
    repository = client.post(
        "/api/v1/repositories",
        json={
            "name": repository_name,
            "provider": "local_snapshot",
            "canonical_uri": str(project_root),
            "default_branch": "master",
        },
    )
    assert repository.status_code == 201, repository.text
    agent = client.post(
        "/api/v1/agents",
        json={"name": "Coding Runtime Test", "description": "test", "draft": draft},
    )
    assert agent.status_code == 201, agent.text
    published = client.post(
        f"/api/v1/agents/{agent.json()['id']}/revisions:publish"
    )
    assert published.status_code == 201, published.text
    deployment = client.post(
        "/api/v1/agent-deployments",
        json={
            "agent_revision_id": published.json()["revision"]["id"],
            "environment": "development",
        },
    )
    assert deployment.status_code == 201, deployment.text
    thread = client.post(
        "/api/v1/threads",
        json={
            "agent_deployment_id": deployment.json()["id"],
            "title": "Coding runtime test",
            "workspace": {
                "repository_id": repository.json()["id"],
                "base_ref": "master",
                "source_mode": "committed_ref",
            },
        },
    )
    assert thread.status_code == 201, thread.text
    return thread.json()


def test_real_deepagents_loop_builds_audited_changeset(tmp_path):
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {
                            "file_path": "/workspace/repo/coding-agent-test.txt",
                            "content": "implemented\n",
                        },
                        "id": "call-write",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Implemented the requested file and verified the change."),
        ]
    )
    provider = FakeSandboxProvider(_command_result)
    project_root = Path(__file__).resolve().parents[1]
    with TestClient(
        create_app(
            str(tmp_path / "platform.db"),
            seed=True,
            model_gateway=DeterministicModelGateway(),
            coding_model=model,
            sandbox_providers=[provider],
            load_env=False,
        )
    ) as client:
        repository = client.post(
            "/api/v1/repositories",
            json={
                "name": "deepagent-test",
                "provider": "local_snapshot",
                "canonical_uri": str(project_root),
                "default_branch": "master",
            },
        )
        assert repository.status_code == 201, repository.text
        agent = client.post(
            "/api/v1/agents",
            json={
                "name": "Coding Agent Test",
                "description": "test",
                "draft": _coding_draft(),
            },
        ).json()
        published = client.post(f"/api/v1/agents/{agent['id']}/revisions:publish")
        assert published.status_code == 201, published.text
        deployment = client.post(
            "/api/v1/agent-deployments",
            json={
                "agent_revision_id": published.json()["revision"]["id"],
                "environment": "development",
            },
        ).json()
        thread = client.post(
            "/api/v1/threads",
            json={
                "agent_deployment_id": deployment["id"],
                "title": "Coding E2E",
                "workspace": {
                    "repository_id": repository.json()["id"],
                    "base_ref": "master",
                    "source_mode": "committed_ref",
                },
            },
        )
        assert thread.status_code == 201, thread.text
        run = client.post(
            f"/api/v1/threads/{thread.json()['id']}/runs",
            json={"input": "Create coding-agent-test.txt containing implemented."},
        ).json()
        finished = _wait(client, run["id"], {"SUCCEEDED", "FAILED"})
        assert finished["status"] == "SUCCEEDED", finished.get("output")

        events = client.get(f"/api/v1/runs/{run['id']}/events").json()["items"]
        event_types = {event["type"] for event in events}
        assert {
            "workspace.ready",
            "file.changed",
            "tool.requested",
            "verification.completed",
            "changeset.created",
            "workspace.snapshot.created",
            "run.completed",
        }.issubset(event_types)
        artifacts = client.get(f"/api/v1/runs/{run['id']}/artifacts").json()["items"]
        assert {
            "changes.patch",
            "diff.json",
            "verification-report.json",
            "command-log.txt",
            "coding-agent-summary.md",
        }.issubset({artifact["name"] for artifact in artifacts})
        for artifact in artifacts:
            if artifact["name"] in {
                "changes.patch",
                "diff.json",
                "verification-report.json",
                "command-log.txt",
                "coding-agent-summary.md",
            }:
                assert artifact["plan_hash"]
                assert artifact["base_commit_sha"]
                assert artifact["workspace_generation"] is not None
                assert len(artifact["content_hash"]) == 64
        verification = client.get(f"/api/v1/runs/{run['id']}/verification").json()
        assert verification["status"] == "PASSED"
        diff = client.get(f"/api/v1/runs/{run['id']}/diff").json()
        assert diff["diff_stat"] == {"files": 1, "added": 1, "deleted": 0}
        assert diff["base_commit_sha"]
        assert diff["plan_hash"]
        change_set = client.get(
            f"/api/v1/runs/{run['id']}/changesets"
        ).json()["items"][0]
        approved = client.post(
            f"/api/v1/runs/{run['id']}/changesets/{change_set['id']}:approve",
            json={"message": "Reviewed"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "DELIVERED"


def test_repository_and_workspace_are_tenant_scoped(tmp_path):
    provider = FakeSandboxProvider(_command_result)
    model = ToolCallingFakeModel(responses=[AIMessage(content="done")])
    with TestClient(
        create_app(
            str(tmp_path / "platform.db"),
            seed=True,
            model_gateway=DeterministicModelGateway(),
            coding_model=model,
            sandbox_providers=[provider],
            load_env=False,
        )
    ) as client:
        repository = client.post(
            "/api/v1/repositories",
            json={
                "name": "scoped",
                "provider": "local_snapshot",
                "canonical_uri": str(Path(__file__).resolve().parents[1]),
            },
        ).json()
        foreign = {
            "X-Tenant-ID": "tenant_other",
            "X-Project-ID": "project_other",
            "X-Environment-ID": "env_development",
            "X-User-ID": "user_other",
            "X-Roles": "owner",
        }
        assert client.get(
            f"/api/v1/repositories/{repository['id']}", headers=foreign
        ).status_code == 404


def test_repository_registration_rejects_paths_outside_allowed_roots(tmp_path):
    with TestClient(
        create_app(
            str(tmp_path / "platform.db"),
            seed=True,
            model_gateway=DeterministicModelGateway(),
            load_env=False,
        )
    ) as client:
        response = client.post(
            "/api/v1/repositories",
            json={
                "name": "outside",
                "provider": "local_snapshot",
                "canonical_uri": "/tmp",
            },
        )
        assert response.status_code == 422
        internal_remote = client.post(
            "/api/v1/repositories",
            json={
                "name": "internal-remote",
                "provider": "generic_git",
                "canonical_uri": "https://127.0.0.1/private/repository.git",
            },
        )
        assert internal_remote.status_code == 422


def test_coding_starter_builds_all_read_only_subagents(tmp_path):
    draft = _coding_draft()
    draft["capabilities"]["subagents"] = [
        "codebase-explorer",
        "code-reviewer",
        "test-diagnostician",
    ]
    model = ToolCallingFakeModel(
        responses=[AIMessage(content="Inspected the task; no source change is required.")]
    )
    with TestClient(
        create_app(
            str(tmp_path / "platform.db"),
            seed=True,
            model_gateway=DeterministicModelGateway(),
            coding_model=model,
            sandbox_providers=[FakeSandboxProvider(_command_result)],
            load_env=False,
        )
    ) as client:
        thread = _create_coding_thread(
            client, draft, repository_name="subagent-fixture"
        )
        created = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            json={"input": "Inspect only; do not change code."},
        ).json()
        finished = _wait(client, created["id"], {"SUCCEEDED", "FAILED"})
        assert finished["status"] == "SUCCEEDED", finished.get("output")


def test_protected_path_interrupt_resumes_real_graph_after_approval(tmp_path):
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {
                            "file_path": "/workspace/repo/.github/workflows/approved.yml",
                            "content": "name: approved\n",
                        },
                        "id": "call-protected-write",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Protected change completed after explicit approval."),
        ]
    )
    provider = FakeSandboxProvider(_command_result)
    with TestClient(
        create_app(
            str(tmp_path / "platform.db"),
            seed=True,
            model_gateway=DeterministicModelGateway(),
            coding_model=model,
            sandbox_providers=[provider],
            load_env=False,
        )
    ) as client:
        thread = _create_coding_thread(client, _coding_draft())
        created = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            json={"input": "Add an approved CI workflow."},
        ).json()
        waiting = _wait(client, created["id"], {"WAITING_FOR_APPROVAL", "FAILED"})
        assert waiting["status"] == "WAITING_FOR_APPROVAL", waiting.get("output")
        interrupts = client.get("/api/v1/interrupts?status=PENDING").json()["items"]
        assert len(interrupts) == 1
        interrupt = interrupts[0]
        assert interrupt["actions"][0]["tool_name"] == "write_file"
        partial_names = {
            item["name"]
            for item in client.get(
                f"/api/v1/runs/{created['id']}/artifacts"
            ).json()["items"]
        }
        assert {
            "changes.patch",
            "diff.json",
            "verification-report.json",
            "command-log.txt",
            "coding-agent-summary.md",
        }.issubset(partial_names)
        action = interrupt["actions"][0]
        decision = client.post(
            f"/api/v1/interrupts/{interrupt['id']}/decisions",
            headers={"If-Match": str(interrupt["version"])},
            json={"decisions": [{"action_id": action["action_id"], "type": "approve"}]},
        )
        assert decision.status_code == 200, decision.text
        finished = _wait(client, created["id"], {"SUCCEEDED", "FAILED"})
        assert finished["status"] == "SUCCEEDED", finished.get("output")
        assert len(finished["attempts"]) == 2
        file_response = client.get(
            f"/api/v1/runs/{created['id']}/workspace/file",
            params={"path": ".github/workflows/approved.yml"},
        )
        assert file_response.status_code == 200, file_response.text
        assert file_response.json()["content"] == "name: approved\n"
        event_types = [
            item["type"]
            for item in client.get(
                f"/api/v1/runs/{created['id']}/events"
            ).json()["items"]
        ]
        assert "graph.paused" in event_types
        assert "graph.resumed" in event_types
        assert "workspace.snapshot.created" in event_types


def test_coding_reviewer_response_resumes_pending_graph_without_running_tool(
    tmp_path,
):
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {
                            "file_path": "/workspace/repo/.github/workflows/declined.yml",
                            "content": "name: declined\n",
                        },
                        "id": "call-review-response",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Acknowledged the reviewer response without writing the file."),
        ]
    )
    with TestClient(
        create_app(
            str(tmp_path / "platform.db"),
            seed=True,
            model_gateway=DeterministicModelGateway(),
            coding_model=model,
            sandbox_providers=[FakeSandboxProvider(_command_result)],
            load_env=False,
        )
    ) as client:
        thread = _create_coding_thread(
            client, _coding_draft(), repository_name="response-fixture"
        )
        created = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            json={"input": "Add a workflow only if approved."},
        ).json()
        waiting = _wait(client, created["id"], {"WAITING_FOR_APPROVAL", "FAILED"})
        assert waiting["status"] == "WAITING_FOR_APPROVAL"
        interrupt = client.get("/api/v1/interrupts?status=PENDING").json()["items"][0]
        action = interrupt["actions"][0]
        responded = client.post(
            f"/api/v1/interrupts/{interrupt['id']}/decisions",
            headers={"If-Match": str(interrupt["version"])},
            json={
                "decisions": [
                    {
                        "action_id": action["action_id"],
                        "type": "respond",
                        "message": "Use no workflow file.",
                    }
                ]
            },
        )
        assert responded.status_code == 200, responded.text
        assert client.get(f"/api/v1/runs/{created['id']}").json()["status"] == "WAITING_FOR_INPUT"
        resumed = client.post(
            f"/api/v1/runs/{created['id']}/input",
            json={"input": "Do not create the workflow; finish without that action."},
        )
        assert resumed.status_code == 202, resumed.text
        finished = _wait(client, created["id"], {"SUCCEEDED", "FAILED"})
        assert finished["status"] == "SUCCEEDED", finished.get("output")
        missing = client.get(
            f"/api/v1/runs/{created['id']}/workspace/file",
            params={"path": ".github/workflows/declined.yml"},
        )
        assert missing.status_code == 404


def test_lost_sandbox_restores_validated_changeset_and_rejects_tampering(tmp_path):
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {
                            "file_path": "/workspace/repo/recover-me.txt",
                            "content": "durable\n",
                        },
                        "id": "call-durable-write",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Created a durable change."),
            AIMessage(content="Continued after the workspace was recovered."),
        ]
    )
    provider = FakeSandboxProvider(_command_result)
    with TestClient(
        create_app(
            str(tmp_path / "platform.db"),
            seed=True,
            model_gateway=DeterministicModelGateway(),
            coding_model=model,
            sandbox_providers=[provider],
            load_env=False,
        )
    ) as client:
        thread = _create_coding_thread(client, _coding_draft())
        first = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            json={"input": "Create a durable workspace change."},
        ).json()
        assert _wait(client, first["id"], {"SUCCEEDED", "FAILED"})["status"] == "SUCCEEDED"
        services = client.app.state.services
        workspace = services.db.fetch_one(
            "SELECT * FROM coding_workspaces WHERE thread_id=?", (thread["id"],)
        )
        old_instance = services.db.fetch_one(
            "SELECT * FROM sandbox_instances WHERE id=?",
            (workspace["sandbox_instance_id"],),
        )
        asyncio.run(provider.destroy(old_instance["external_id"]))

        second = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            json={"input": "Continue from the existing workspace."},
        ).json()
        second_finished = _wait(client, second["id"], {"SUCCEEDED", "FAILED"})
        assert second_finished["status"] == "SUCCEEDED", second_finished.get("output")
        second_events = client.get(
            f"/api/v1/runs/{second['id']}/events"
        ).json()["items"]
        assert "workspace.recovering" in {item["type"] for item in second_events}
        recovered_workspace = services.db.fetch_one(
            "SELECT * FROM coding_workspaces WHERE thread_id=?", (thread["id"],)
        )
        assert recovered_workspace["sandbox_instance_id"] != old_instance["id"]

        recovered_instance = services.db.fetch_one(
            "SELECT * FROM sandbox_instances WHERE id=?",
            (recovered_workspace["sandbox_instance_id"],),
        )
        asyncio.run(provider.destroy(recovered_instance["external_id"]))
        latest_change_set = services.db.fetch_one(
            """SELECT * FROM change_sets WHERE workspace_id=?
               ORDER BY created_at DESC LIMIT 1""",
            (recovered_workspace["id"],),
        )
        services.db.execute(
            "UPDATE artifacts SET content='tampered patch' WHERE id=?",
            (latest_change_set["patch_artifact_id"],),
        )
        refused_delivery = client.post(
            f"/api/v1/runs/{second['id']}/changesets/{latest_change_set['id']}:approve",
            json={"message": "must fail integrity validation"},
        )
        assert refused_delivery.status_code == 409
        third = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            json={"input": "This recovery must fail closed."},
        ).json()
        third_finished = _wait(client, third["id"], {"SUCCEEDED", "FAILED"})
        assert third_finished["status"] == "FAILED"
        assert "patch hash is invalid" in third_finished["output"].lower()


def test_cancelling_run_terminates_real_docker_command_and_preserves_partial_state(
    tmp_path,
):
    provider = DockerSandboxProvider(
        image="deepagent/coding-runtime:0.1.0",
        dockerfile_root="docker/coding-runtime",
        auto_build=True,
    )
    if not asyncio.run(provider.available()):
        import pytest

        pytest.skip("Docker daemon is unavailable")
    draft = _coding_draft()
    draft["coding"]["sandbox"].update(
        {
            "provider": "docker",
            "image": "deepagent/coding-runtime:0.1.0",
            "image_digest": "sha256:" + provider.resolve_image_digest(
                "deepagent/coding-runtime:0.1.0"
            ),
        }
    )
    draft["coding"]["verification_policy"] = {
        "auto_discover": False,
        "required_commands": ["true"],
        "max_attempts": 1,
        "command_timeout_seconds": 10,
        "require_success": True,
    }
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "execute",
                        "args": {"command": "sleep 30", "timeout": 60},
                        "id": "call-long-command",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="This response must not be reached after cancellation."),
        ]
    )
    external_id = None
    with TestClient(
        create_app(
            str(tmp_path / "platform.db"),
            seed=True,
            model_gateway=DeterministicModelGateway(),
            coding_model=model,
            sandbox_providers=[provider],
            load_env=False,
        )
    ) as client:
        thread = _create_coding_thread(
            client, draft, repository_name="docker-cancel-fixture"
        )
        created = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            json={"input": "Run the requested long local check."},
        ).json()
        _wait_event(
            client,
            created["id"],
            "sandbox.command.started",
            predicate=lambda item: item["payload"].get("command_id") is not None,
        )
        started = time.monotonic()
        cancelled = client.post(f"/api/v1/runs/{created['id']}:cancel")
        elapsed = time.monotonic() - started
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "CANCELLED"
        assert elapsed < 8
        _wait_event(
            client,
            created["id"],
            "workspace.snapshot.created",
            predicate=lambda item: item["payload"].get("reason") == "run_cancelled",
            timeout=15,
        )
        workspace = client.app.state.services.db.fetch_one(
            "SELECT * FROM coding_workspaces WHERE thread_id=?", (thread["id"],)
        )
        instance = client.app.state.services.db.fetch_one(
            "SELECT * FROM sandbox_instances WHERE id=?",
            (workspace["sandbox_instance_id"],),
        )
        external_id = instance["external_id"]
        processes = provider.client.containers.get(external_id).top().get("Processes") or []
        assert not any("sleep 30" in " ".join(row) for row in processes)
        artifact_names = {
            item["name"]
            for item in client.get(
                f"/api/v1/runs/{created['id']}/artifacts"
            ).json()["items"]
        }
        assert {"changes.patch", "diff.json", "verification-report.json"}.issubset(
            artifact_names
        )
    if external_id:
        asyncio.run(provider.destroy(external_id))


def test_real_docker_agent_change_is_verified_and_does_not_touch_host_checkout(
    tmp_path,
):
    provider = DockerSandboxProvider(
        image="deepagent/coding-runtime:0.1.0",
        dockerfile_root="docker/coding-runtime",
        auto_build=True,
    )
    if not asyncio.run(provider.available()):
        import pytest

        pytest.skip("Docker daemon is unavailable")
    draft = _coding_draft()
    draft["coding"]["sandbox"].update(
        {
            "provider": "docker",
            "image": "deepagent/coding-runtime:0.1.0",
            "image_digest": "sha256:" + provider.resolve_image_digest(
                "deepagent/coding-runtime:0.1.0"
            ),
        }
    )
    draft["coding"]["verification_policy"] = {
        "auto_discover": False,
        "required_commands": ["test \"$(cat docker-coding-e2e.txt)\" = isolated"],
        "max_attempts": 1,
        "command_timeout_seconds": 10,
        "require_success": True,
    }
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {
                            "file_path": "/workspace/repo/docker-coding-e2e.txt",
                            "content": "isolated\n",
                        },
                        "id": "call-docker-write",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Created and verified the isolated file."),
        ]
    )
    host_target = Path(__file__).resolve().parents[1] / "docker-coding-e2e.txt"
    assert not host_target.exists()
    external_id = None
    try:
        with TestClient(
            create_app(
                str(tmp_path / "platform.db"),
                seed=True,
                model_gateway=DeterministicModelGateway(),
                coding_model=model,
                sandbox_providers=[provider],
                load_env=False,
            )
        ) as client:
            thread = _create_coding_thread(
                client, draft, repository_name="docker-e2e-fixture"
            )
            created = client.post(
                f"/api/v1/threads/{thread['id']}/runs",
                json={"input": "Create docker-coding-e2e.txt with isolated."},
            ).json()
            finished = _wait(
                client, created["id"], {"SUCCEEDED", "FAILED"}, timeout=20
            )
            assert finished["status"] == "SUCCEEDED", finished.get("output")
            diff = client.get(f"/api/v1/runs/{created['id']}/diff").json()
            changed = next(
                item
                for item in diff["changed_files"]
                if item["path"] == "docker-coding-e2e.txt"
            )
            assert len(changed["sha256"]) == 64
            assert "+isolated" in diff["patch"]
            assert client.get(
                f"/api/v1/runs/{created['id']}/verification"
            ).json()["status"] == "PASSED"
            commands = client.app.state.services.db.fetch_all(
                "SELECT * FROM sandbox_commands WHERE run_id=?", (created["id"],)
            )
            assert any(
                item.get("resource_usage", {}).get("workspace_disk_bytes", 0) > 0
                for item in commands
            )
            workspace = client.app.state.services.db.fetch_one(
                "SELECT * FROM coding_workspaces WHERE thread_id=?", (thread["id"],)
            )
            instance = client.app.state.services.db.fetch_one(
                "SELECT * FROM sandbox_instances WHERE id=?",
                (workspace["sandbox_instance_id"],),
            )
            external_id = instance["external_id"]
            plan = client.app.state.services.db.fetch_one(
                "SELECT * FROM resolved_execution_plans WHERE id=?",
                (finished["resolved_plan_id"],),
            )
            assert plan["runtime_image_digest"].endswith(
                instance["provider_metadata"]["image_id"]
            )
    finally:
        if external_id:
            asyncio.run(provider.destroy(external_id))
    assert not host_target.exists()
