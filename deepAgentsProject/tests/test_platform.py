from __future__ import annotations

import time

from fastapi.testclient import TestClient

from apps.platform_api.main import create_app


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
    return TestClient(create_app(str(tmp_path / "platform.db"), seed=True))


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
        assert run["usage"]["model_calls"] == 2
        events = client.get(f"/api/v1/runs/{run['id']}/events").json()["items"]
        assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
        assert {"model.started", "tool.completed", "subagent.completed", "artifact.created", "run.completed"}.issubset(
            {event["type"] for event in events}
        )
        resumed = client.get(
            f"/api/v1/runs/{run['id']}/events?after_sequence={events[-2]['sequence']}"
        ).json()["items"]
        assert len(resumed) == 1
        assert resumed[0]["type"] == "run.completed"


def test_hitl_checkpoint_decision_and_new_attempt_resume(tmp_path):
    with client_for(tmp_path) as client:
        deployment = client.get("/api/v1/agent-deployments").json()["items"][0]
        thread = client.post(
            "/api/v1/threads",
            json={"agent_deployment_id": deployment["id"], "title": "HITL test"},
        ).json()
        created = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            json={"input": "Deploy the build to production and write the release artifact."},
        ).json()
        waiting = wait_for_status(client, created["id"], {"WAITING_FOR_APPROVAL"})
        assert waiting["checkpoint"]["stage"] == "awaiting_approval"

        interrupt = client.get("/api/v1/interrupts?status=PENDING").json()["items"][0]
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
        assert "run.resumed" in types
        assert types[-1] == "run.completed"


def test_tenant_scope_prevents_cross_tenant_access(tmp_path):
    with client_for(tmp_path) as client:
        agent = client.get("/api/v1/agents").json()["items"][0]
        foreign_headers = {"X-Tenant-ID": "tenant_other", "X-Project-ID": "project_other"}
        assert client.get(f"/api/v1/agents/{agent['id']}", headers=foreign_headers).status_code == 404
        assert client.get("/api/v1/agents", headers=foreign_headers).json()["items"] == []
        assert client.get("/api/v1/agent-deployments", headers=foreign_headers).json()["items"] == []
