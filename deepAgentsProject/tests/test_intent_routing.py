from __future__ import annotations

import time

from fastapi.testclient import TestClient

from apps.platform_api.main import create_app
from packages.runtime.model_gateway import DeterministicModelGateway
from packages.sandbox.fake_provider import FakeSandboxProvider


def _client(tmp_path):
    return TestClient(
        create_app(
            str(tmp_path / "routing.db"),
            seed=True,
            model_gateway=DeterministicModelGateway(),
            load_env=False,
            sandbox_providers=[FakeSandboxProvider()],
        )
    )


def _resolve(client: TestClient, text: str, **extra):
    response = client.post(
        "/api/v1/intent-routing:resolve", json={"input": text, **extra}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _wait(client: TestClient, run_id: str, timeout: float = 4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/v1/runs/{run_id}").json()
        if run["status"] not in {"CREATED", "QUEUED", "PREPARING", "RUNNING"}:
            return run
        time.sleep(0.04)
    raise AssertionError("Routed run did not settle")


def test_coding_intent_requires_workspace_and_selects_coding_agent(tmp_path):
    with _client(tmp_path) as client:
        decision = _resolve(
            client, "请修复登录代码中的 bug，然后运行单元测试。"
        )
        assert decision["status"] == "NEEDS_WORKSPACE"
        assert decision["classification"]["primary_intent"] == "coding"
        assert decision["classification"]["subtype"] == "code_change"
        assert decision["selected_deployment"]["coding_enabled"] is True
        rejected = client.post(
            "/api/v1/routed-runs",
            json={"decision_id": decision["id"], "input": "请修复登录代码中的 bug，然后运行单元测试。"},
        )
        assert rejected.status_code == 409
        assert "workspace" in rejected.json()["error"]["message"].lower()


def test_release_intent_creates_sticky_routed_thread_and_audit_events(tmp_path):
    text = "请把这个版本部署到生产环境，并准备发布记录。"
    with _client(tmp_path) as client:
        decision = _resolve(client, text)
        assert decision["status"] == "READY"
        assert decision["classification"]["primary_intent"] == "release"
        assert decision["selected_deployment"]["agent_name"] == "Release Sentinel"

        created = client.post(
            "/api/v1/routed-runs",
            json={"decision_id": decision["id"], "input": text},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["thread"]["agent_deployment_id"] == decision["selected_deployment_id"]
        assert body["thread"]["routing_decision_id"] == decision["id"]
        assert body["run"]["routing_decision_id"] == decision["id"]

        # A later turn stays on the thread's pinned deployment and does not create
        # a second routing decision.
        follow_up = client.post(
            f"/api/v1/threads/{body['thread']['id']}/runs",
            json={"input": "现在解释一下相关代码，不要切换 Agent。"},
        )
        assert follow_up.status_code == 409
        cancelled = client.post(f"/api/v1/runs/{body['run']['id']}:cancel")
        assert cancelled.status_code == 200
        follow_up = client.post(
            f"/api/v1/threads/{body['thread']['id']}/runs",
            json={"input": "现在解释一下相关代码，不要切换 Agent。"},
        )
        assert follow_up.status_code == 202
        assert follow_up.json()["agent_deployment_id"] == decision["selected_deployment_id"]
        decisions = client.get("/api/v1/intent-routing/decisions").json()["items"]
        assert len(decisions) == 1

        events = client.get(
            f"/api/v1/runs/{body['run']['id']}/events"
        ).json()["items"]
        event_types = {event["type"] for event in events}
        assert "intent.classification.started" in event_types
        assert "intent.classification.completed" in event_types
        assert "routing.agent.selected" in event_types

        mismatched_retry = client.post(
            "/api/v1/routed-runs",
            json={"decision_id": decision["id"], "input": "not the original request"},
        )
        assert mismatched_retry.status_code == 409


def test_knowledge_route_falls_back_when_no_knowledge_deployment_exists(tmp_path):
    with _client(tmp_path) as client:
        decision = _resolve(client, "请根据项目手册说明请假审批流程。")
        assert decision["classification"]["primary_intent"] == "knowledge"
        assert decision["status"] == "FALLBACK"
        assert decision["reason"] == "knowledge_target_unavailable"
        assert decision["selected_deployment"]["coding_enabled"] is False


def test_ambiguous_classification_requires_confirmation_and_checks_input_hash(tmp_path):
    text = "帮我看看这个"
    with _client(tmp_path) as client:
        decision = _resolve(client, text)
        assert decision["classification"]["source"] == "fallback"
        assert decision["status"] == "NEEDS_CONFIRMATION"

        unconfirmed = client.post(
            "/api/v1/routed-runs",
            json={"decision_id": decision["id"], "input": text},
        )
        assert unconfirmed.status_code == 409

        mismatched = client.post(
            "/api/v1/routed-runs",
            json={
                "decision_id": decision["id"],
                "input": "different input",
                "confirmed": True,
            },
        )
        assert mismatched.status_code == 409

        confirmed = client.post(
            "/api/v1/routed-runs",
            json={"decision_id": decision["id"], "input": text, "confirmed": True},
        )
        assert confirmed.status_code == 201, confirmed.text


def test_manual_deployment_override_has_priority(tmp_path):
    text = "请修复代码并运行测试。"
    with _client(tmp_path) as client:
        deployments = client.get("/api/v1/agent-deployments").json()["items"]
        release = next(item for item in deployments if not item["coding_enabled"])
        decision = _resolve(
            client,
            text,
            preferred_deployment_id=release["id"],
        )
        assert decision["status"] == "READY"
        assert decision["reason"] == "user_selected_deployment"
        assert decision["selected_deployment_id"] == release["id"]
        assert decision["classification"]["primary_intent"] == "coding"

        created = client.post(
            "/api/v1/routed-runs",
            json={"decision_id": decision["id"], "input": text},
        )
        assert created.status_code == 201, created.text
        assert created.json()["decision"]["override_deployment_id"] == release["id"]
        events = client.get(
            f"/api/v1/runs/{created.json()['run']['id']}/events"
        ).json()["items"]
        override = next(
            event for event in events if event["type"] == "routing.user_overridden"
        )
        assert override["payload"]["manual_override"] is True


def test_routing_profile_is_versioned_and_shadow_mode_does_not_auto_route(tmp_path):
    with _client(tmp_path) as client:
        initial = client.get("/api/v1/intent-routing/profile").json()
        assert initial["revision_number"] == 1
        updated = client.put(
            "/api/v1/intent-routing/profile",
            json={
                "mode": "shadow",
                "auto_route_threshold": 0.8,
                "confirmation_threshold": 0.55,
                "decision_ttl_seconds": 600,
                "target_deployments": {},
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["revision_number"] == 2
        assert updated.json()["mode"] == "shadow"

        decision = _resolve(client, "修复代码 bug 并运行测试")
        assert decision["status"] == "FALLBACK"
        assert decision["predicted_deployment"]["coding_enabled"] is True
        assert decision["selected_deployment"]["coding_enabled"] is False
        assert decision["reason"] == "shadow_mode_default_selected"


def test_disabled_routing_uses_general_without_confirmation(tmp_path):
    with _client(tmp_path) as client:
        disabled = client.put(
            "/api/v1/intent-routing/profile",
            json={
                "mode": "disabled",
                "auto_route_threshold": 0.8,
                "confirmation_threshold": 0.55,
                "decision_ttl_seconds": 600,
                "target_deployments": {},
            },
        )
        assert disabled.status_code == 200, disabled.text

        decision = _resolve(client, "帮我看看这个")
        assert decision["status"] == "FALLBACK"
        assert decision["reason"] == "routing_disabled"
        assert decision["classification"]["subtype"] == "routing_disabled"
        assert decision["requirements"]["confirmation"] is False
        assert decision["selected_deployment"]["coding_enabled"] is False


def test_routing_profile_update_requires_owner_or_admin(tmp_path):
    with _client(tmp_path) as client:
        headers = {
            "X-Tenant-ID": "tenant_demo",
            "X-Project-ID": "project_atlas",
            "X-Environment-ID": "env_development",
            "X-User-ID": "user_viewer",
            "X-Roles": "viewer",
        }
        visible = client.get("/api/v1/intent-routing/profile", headers=headers)
        assert visible.status_code == 200
        forbidden = client.put(
            "/api/v1/intent-routing/profile",
            headers=headers,
            json={
                "mode": "disabled",
                "auto_route_threshold": 0.8,
                "confirmation_threshold": 0.55,
                "decision_ttl_seconds": 600,
                "target_deployments": {},
            },
        )
        assert forbidden.status_code == 403


def test_routing_profile_rejects_incompatible_general_target(tmp_path):
    with _client(tmp_path) as client:
        deployments = client.get("/api/v1/agent-deployments").json()["items"]
        coding = next(item for item in deployments if item["coding_enabled"])
        rejected = client.put(
            "/api/v1/intent-routing/profile",
            json={
                "mode": "active",
                "auto_route_threshold": 0.8,
                "confirmation_threshold": 0.55,
                "decision_ttl_seconds": 600,
                "target_deployments": {"general": coding["id"]},
            },
        )
        assert rejected.status_code == 409
        assert "non-Coding" in rejected.json()["error"]["message"]


def test_normal_thread_creation_cannot_forge_routing_link(tmp_path):
    with _client(tmp_path) as client:
        decision = _resolve(client, "写一首关于海风的诗")
        created = client.post(
            "/api/v1/threads",
            json={
                "agent_deployment_id": decision["selected_deployment_id"],
                "title": "Manual thread",
                "routing_decision_id": decision["id"],
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["routing_decision_id"] is None


def test_routing_decisions_are_tenant_scoped(tmp_path):
    with _client(tmp_path) as client:
        decision = _resolve(client, "写一首关于海风的诗")
        other_headers = {
            "X-Tenant-ID": "tenant_other",
            "X-Project-ID": "project_other",
            "X-Environment-ID": "env_development",
            "X-User-ID": "user_other",
            "X-Roles": "owner",
        }
        hidden = client.get(
            f"/api/v1/intent-routing/decisions/{decision['id']}",
            headers=other_headers,
        )
        assert hidden.status_code == 404


def test_routing_decisions_are_environment_scoped(tmp_path):
    with _client(tmp_path) as client:
        decision = _resolve(client, "写一首关于海风的诗")
        staging_headers = {
            "X-Tenant-ID": "tenant_demo",
            "X-Project-ID": "project_atlas",
            "X-Environment-ID": "env_staging",
            "X-User-ID": "user_demo",
            "X-Roles": "owner",
        }
        hidden = client.get(
            f"/api/v1/intent-routing/decisions/{decision['id']}",
            headers=staging_headers,
        )
        assert hidden.status_code == 404
