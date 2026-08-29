from __future__ import annotations

import time

from fastapi.testclient import TestClient

from apps.platform_api.main import create_app
from packages.runtime.model_gateway import DeterministicModelGateway


class RecordingModelGateway(DeterministicModelGateway):
    def __init__(self):
        self.calls = []

    async def complete(self, messages, on_delta=None):
        self.calls.append(messages)
        return await super().complete(messages, on_delta)


def wait_for_status(client: TestClient, run_id: str, expected: set[str], timeout: float = 4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/v1/runs/{run_id}")
        assert response.status_code == 200
        run = response.json()
        if run["status"] in expected:
            return run
        time.sleep(0.04)
    raise AssertionError(f"Run did not reach {expected}")


def client_for(tmp_path):
    return TestClient(
        create_app(
            str(tmp_path / "platform.db"),
            seed=True,
            model_gateway=DeterministicModelGateway(),
            load_env=False,
        )
    )


def create_run_waiting_for_approval(client: TestClient, title: str = "HITL test"):
    deployment = client.get("/api/v1/agent-deployments").json()["items"][0]
    thread = client.post(
        "/api/v1/threads",
        json={"agent_deployment_id": deployment["id"], "title": title},
    ).json()
    created = client.post(
        f"/api/v1/threads/{thread['id']}/runs",
        json={"input": "Deploy the build to production and write the release artifact."},
    ).json()
    wait_for_status(client, created["id"], {"WAITING_FOR_APPROVAL"})
    interrupt = client.get("/api/v1/interrupts?status=PENDING").json()["items"][0]
    return created, interrupt


def test_platform_context_reports_authoritative_scope_and_capabilities(tmp_path):
    with client_for(tmp_path) as client:
        response = client.get(
            "/api/v1/context",
            headers={
                "X-Tenant-ID": "tenant_review",
                "X-Project-ID": "project_console",
                "X-Environment-ID": "env_staging",
                "X-User-ID": "reviewer_1",
                "X-Roles": "viewer",
            },
        )
        assert response.status_code == 200
        context = response.json()
        assert context["tenant"]["id"] == "tenant_review"
        assert context["project"]["id"] == "project_console"
        assert context["environment"]["name"] == "Staging"
        assert context["user"]["id"] == "reviewer_1"
        assert context["runtime"]["event_lag_ms"] is None
        assert context["features"]["notifications"] is False


def test_untrusted_deployment_rejects_caller_supplied_identity_headers(tmp_path):
    with TestClient(
        create_app(
            str(tmp_path / "platform.db"),
            seed=True,
            model_gateway=DeterministicModelGateway(),
            load_env=False,
            trust_identity_headers=False,
            allow_demo_identity=False,
        )
    ) as client:
        assert client.get("/api/v1/context").status_code == 401
        rejected = client.get(
            "/api/v1/context", headers={"X-Roles": "owner", "X-User-ID": "attacker"}
        )
        assert rejected.status_code == 401


def test_trusted_identity_adapter_requires_complete_principal(tmp_path):
    with TestClient(
        create_app(
            str(tmp_path / "platform.db"),
            seed=True,
            model_gateway=DeterministicModelGateway(),
            load_env=False,
            trust_identity_headers=True,
            allow_demo_identity=False,
        )
    ) as client:
        assert (
            client.get("/api/v1/context", headers={"X-User-ID": "partial"}).status_code
            == 401
        )
        accepted = client.get(
            "/api/v1/context",
            headers={
                "X-Tenant-ID": "tenant_review",
                "X-Project-ID": "project_console",
                "X-Environment-ID": "env_staging",
                "X-User-ID": "reviewer_1",
                "X-Roles": "viewer",
            },
        )
        assert accepted.status_code == 200
        assert accepted.json()["user"]["id"] == "reviewer_1"


def test_builtin_plugins_are_loaded_pinned_and_idempotent(tmp_path):
    database_path = str(tmp_path / "platform.db")
    with TestClient(
        create_app(
            database_path,
            seed=True,
            model_gateway=DeterministicModelGateway(),
            load_env=False,
        )
    ) as client:
        health = client.get("/health").json()
        plugins = client.get("/api/v1/plugins").json()["items"]
        skills = client.get("/api/v1/skills").json()["items"]
        assert health["plugins_loaded"] == len(plugins)
        assert health["skills_loaded"] == len(skills)
        core = next(plugin for plugin in plugins if plugin["id"] == "deepagent-core")
        assert core["skill_count"] == 3
        core_skills = {
            "task-planning",
            "evidence-research",
            "release-safety",
        }
        assert core_skills.issubset({skill["slug"] for skill in skills})
        plugin_ids = {plugin["id"] for plugin in plugins}
        skill_slugs = {skill["slug"] for skill in skills}

        agent = client.get("/api/v1/agents").json()["items"][0]
        detail = client.get(f"/api/v1/agents/{agent['id']}").json()
        plan = detail["revisions"][0]
        resolved = client.get(f"/api/v1/agent-revisions/{plan['id']}").json()["resolved_plan"]["plan"]
        pinned_skills = resolved["skill_versions"]
        assert {skill["slug"] for skill in pinned_skills} == {"task-planning", "release-safety"}
        assert all(len(skill["artifact_hash"]) == 64 for skill in pinned_skills)
        assert all(skill["instructions"].startswith("# ") for skill in pinned_skills)

    with TestClient(
        create_app(
            database_path,
            seed=True,
            model_gateway=DeterministicModelGateway(),
            load_env=False,
        )
    ) as client:
        assert {
            plugin["id"]
            for plugin in client.get("/api/v1/plugins").json()["items"]
        } == plugin_ids
        assert {
            skill["slug"] for skill in client.get("/api/v1/skills").json()["items"]
        } == skill_slugs


def test_unknown_skill_blocks_publish(tmp_path):
    with client_for(tmp_path) as client:
        agent = client.get("/api/v1/agents").json()["items"][0]
        detail = client.get(f"/api/v1/agents/{agent['id']}").json()
        detail["draft"]["capabilities"]["skills"].append("missing-skill")
        saved = client.patch(
            f"/api/v1/agents/{agent['id']}/draft",
            json={
                "name": detail["name"],
                "description": detail["description"],
                "draft": detail["draft"],
                "version": detail["version"],
            },
        )
        assert saved.status_code == 200
        validation = client.post(f"/api/v1/agents/{agent['id']}/revisions:validate").json()
        assert validation["valid"] is False
        assert any(issue["code"] == "SKILL_NOT_FOUND" for issue in validation["issues"])
        assert client.post(f"/api/v1/agents/{agent['id']}/revisions:publish").status_code == 409


def test_publish_creates_immutable_revision_and_plan(tmp_path):
    with client_for(tmp_path) as client:
        agent = client.get("/api/v1/agents").json()["items"][0]
        detail = client.get(f"/api/v1/agents/{agent['id']}").json()
        original_revision = detail["revisions"][0]

        changed_draft = detail["draft"]
        changed_draft["system_prompt"] = "A new prompt that must not rewrite revision one."
        updated = client.patch(
            f"/api/v1/agents/{agent['id']}/draft",
            json={
                "name": detail["name"],
                "description": detail["description"],
                "draft": changed_draft,
                "version": detail["version"],
            },
        )
        assert updated.status_code == 200

        published = client.post(f"/api/v1/agents/{agent['id']}/revisions:publish")
        assert published.status_code == 201
        body = published.json()
        assert body["revision"]["revision_number"] == 2
        assert len(body["resolved_plan"]["plan_hash"]) == 64

        old_revision = client.get(f"/api/v1/agent-revisions/{original_revision['id']}").json()
        assert old_revision["spec"]["system_prompt"] != changed_draft["system_prompt"]
        assert old_revision["resolved_plan"]["runtime_image_digest"].startswith("deepagent/runtime@sha256:")


def test_run_is_idempotent_stream_is_sequenced_and_usage_is_recorded(tmp_path):
    with client_for(tmp_path) as client:
        deployment = client.get("/api/v1/agent-deployments").json()["items"][0]
        thread = client.post(
            "/api/v1/threads",
            json={"agent_deployment_id": deployment["id"], "title": "Idempotency test"},
        ).json()
        headers = {"Idempotency-Key": "same-user-command"}
        first = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            headers=headers,
            json={"input": "Analyze the release and prepare a read-only recommendation."},
        )
        second = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            headers=headers,
            json={"input": "Analyze the release and prepare a read-only recommendation."},
        )
        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["id"] == second.json()["id"]

        run = wait_for_status(client, first.json()["id"], {"SUCCEEDED"})
        assert run["usage"]["model_calls"] == 1
        assert run["usage"]["tool_calls"] == 0
        events = client.get(f"/api/v1/runs/{run['id']}/events").json()["items"]
        assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
        assert {
            "graph.started",
            "graph.node.started",
            "graph.node.completed",
            "graph.subgraph.started",
            "graph.subgraph.completed",
            "model.started",
            "model.reasoning.started",
            "model.reasoning.delta",
            "model.reasoning.completed",
            "rag.agent.routed",
            "subagent.completed",
            "artifact.created",
            "graph.completed",
            "run.completed",
        }.issubset(
            {event["type"] for event in events}
        )
        route = next(event for event in events if event["type"] == "rag.agent.routed")
        assert route["payload"]["route"] == "model_only"
        loaded_skills = [event for event in events if event["type"] == "skill.loaded"]
        assert {event["payload"]["slug"] for event in loaded_skills} == {
            "task-planning",
            "release-safety",
        }
        event_types = [event["type"] for event in events]
        assert event_types.index("model.reasoning.started") < event_types.index("model.reasoning.delta")
        assert event_types.index("model.reasoning.delta") < event_types.index("model.reasoning.completed")
        assert event_types.index("model.reasoning.completed") < event_types.index("model.completed")
        reasoning_completed = next(
            event for event in events if event["type"] == "model.reasoning.completed"
        )
        assert reasoning_completed["payload"]["reasoning"]
        resumed = client.get(
            f"/api/v1/runs/{run['id']}/events?after_sequence={events[-2]['sequence']}"
        ).json()["items"]
        assert len(resumed) == 1
        assert resumed[0]["type"] == "run.completed"
        unified_stream = client.get(
            f"/api/v1/runs/{run['id']}/stream?channel=all"
        )
        assert unified_stream.status_code == 200
        assert unified_stream.text.count("event: runtime.event") == len(events)
        assert '"type":"run.completed"' in unified_stream.text
        assert "event: stream.idle" in unified_stream.text
        artifacts = client.get(f"/api/v1/runs/{run['id']}/artifacts").json()["items"]
        assert artifacts
        assert "content" not in artifacts[0]
        assert artifacts[0]["uri"].endswith(artifacts[0]["id"])
        artifact = client.get(artifacts[0]["uri"])
        assert artifact.status_code == 200
        assert artifact.headers["content-type"].startswith("text/markdown")
        assert "Agent response" in artifact.text


def test_second_run_sends_prior_successful_turns_to_model(tmp_path):
    gateway = RecordingModelGateway()
    with TestClient(
        create_app(
            str(tmp_path / "platform.db"),
            seed=True,
            model_gateway=gateway,
            load_env=False,
        )
    ) as client:
        deployment = client.get("/api/v1/agent-deployments").json()["items"][0]
        thread = client.post(
            "/api/v1/threads",
            json={"agent_deployment_id": deployment["id"], "title": "History test"},
        ).json()
        first = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            headers={"Idempotency-Key": "history-one"},
            json={"input": "Remember that the release codename is Aurora."},
        ).json()
        wait_for_status(client, first["id"], {"SUCCEEDED"})
        second = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            headers={"Idempotency-Key": "history-two"},
            json={"input": "What was the codename?"},
        ).json()
        wait_for_status(client, second["id"], {"SUCCEEDED"})

    assert len(gateway.calls) == 2
    assert [message["role"] for message in gateway.calls[1]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert gateway.calls[1][1]["content"] == "Remember that the release codename is Aurora."
    assert gateway.calls[1][-1]["content"] == "What was the codename?"


def test_hitl_checkpoint_decision_and_new_attempt_resume(tmp_path):
    with client_for(tmp_path) as client:
        created, interrupt = create_run_waiting_for_approval(client)
        waiting = client.get(f"/api/v1/runs/{created['id']}").json()
        assert waiting["checkpoint"]["stage"] == "awaiting_approval"

        action = interrupt["actions"][0]
        decision_headers = {"If-Match": str(interrupt["version"]), "Idempotency-Key": "approve-once"}
        payload = {"decisions": [{"action_id": action["action_id"], "type": "approve"}]}
        first = client.post(
            f"/api/v1/interrupts/{interrupt['id']}/decisions",
            headers=decision_headers,
            json=payload,
        )
        repeated = client.post(
            f"/api/v1/interrupts/{interrupt['id']}/decisions",
            headers=decision_headers,
            json=payload,
        )
        assert first.status_code == 200
        assert repeated.status_code == 200

        finished = wait_for_status(client, created["id"], {"SUCCEEDED"})
        assert len(finished["attempts"]) == 2
        events = client.get(f"/api/v1/runs/{created['id']}/events").json()["items"]
        types = [event["type"] for event in events]
        assert "interrupt.created" in types
        assert "interrupt.resolved" in types
        assert "graph.paused" in types
        assert "graph.resumed" in types
        assert "run.resumed" in types
        assert types[-1] == "run.completed"


def test_rejecting_approval_cancels_without_creating_an_attempt(tmp_path):
    with client_for(tmp_path) as client:
        created, interrupt = create_run_waiting_for_approval(client, "Reject test")
        action = interrupt["actions"][0]
        response = client.post(
            f"/api/v1/interrupts/{interrupt['id']}/decisions",
            headers={"If-Match": str(interrupt["version"])},
            json={
                "decisions": [
                    {
                        "action_id": action["action_id"],
                        "type": "reject",
                        "message": "Production window is closed.",
                    }
                ]
            },
        )
        assert response.status_code == 200
        run = client.get(f"/api/v1/runs/{created['id']}").json()
        assert run["status"] == "CANCELLED"
        assert run["checkpoint"]["stage"] == "approval_rejected"
        assert len(run["attempts"]) == 1
        events = client.get(f"/api/v1/runs/{created['id']}/events").json()["items"]
        assert events[-1]["type"] == "run.cancelled"


def test_requesting_changes_waits_for_input_without_executing_tool(tmp_path):
    with client_for(tmp_path) as client:
        created, interrupt = create_run_waiting_for_approval(client, "Changes test")
        action = interrupt["actions"][0]
        response = client.post(
            f"/api/v1/interrupts/{interrupt['id']}/decisions",
            headers={"If-Match": str(interrupt["version"])},
            json={
                "decisions": [
                    {
                        "action_id": action["action_id"],
                        "type": "respond",
                        "message": "Use the staging environment and request approval again.",
                    }
                ]
            },
        )
        assert response.status_code == 200
        run = client.get(f"/api/v1/runs/{created['id']}").json()
        assert run["status"] == "WAITING_FOR_INPUT"
        assert run["checkpoint"]["stage"] == "waiting_for_input"
        assert len(run["attempts"]) == 1
        events = client.get(f"/api/v1/runs/{created['id']}/events").json()["items"]
        assert events[-1]["type"] == "run.waiting_for_input"

        resumed = client.post(
            f"/api/v1/runs/{created['id']}/input",
            json={"input": "Prepare a revised read-only recommendation for the staging review."},
        )
        assert resumed.status_code == 202
        finished = wait_for_status(client, created["id"], {"SUCCEEDED"})
        assert len(finished["attempts"]) == 2
        assert finished["checkpoint"]["stage"] == "input_received"
        resumed_events = client.get(
            f"/api/v1/runs/{created['id']}/events"
        ).json()["items"]
        assert "run.input_received" in {event["type"] for event in resumed_events}
        assert resumed_events[-1]["type"] == "run.completed"


def test_tenant_scope_prevents_cross_tenant_access(tmp_path):
    with client_for(tmp_path) as client:
        agent = client.get("/api/v1/agents").json()["items"][0]
        foreign_headers = {
            "X-Tenant-ID": "tenant_other",
            "X-Project-ID": "project_other",
            "X-Environment-ID": "env_development",
            "X-User-ID": "foreign_user",
            "X-Roles": "viewer",
        }
        assert client.get(f"/api/v1/agents/{agent['id']}", headers=foreign_headers).status_code == 404
        assert client.get("/api/v1/agents", headers=foreign_headers).json()["items"] == []
        assert client.get("/api/v1/agent-deployments", headers=foreign_headers).json()["items"] == []
