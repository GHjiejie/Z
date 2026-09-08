from dataclasses import replace
from datetime import timedelta
import time

import pytest
from fastapi.testclient import TestClient

from apps.platform_api.main import create_app
from packages.runtime.model_gateway import DeterministicModelGateway
from packages.sandbox.fake_provider import FakeSandboxProvider
from release_helpers import authorities, bind_controlled_model


class ControlledGateway(DeterministicModelGateway):
    """Controlled provider fixture for gate logic, NOT a real-provider acceptance test."""

    broken = False

    def identity(self):
        return {**super().identity(), "provider": "openai-compatible", "api_style": "chat_completions", "model": "qwen3-235b-a22b"}

    async def complete(self, messages, on_event=None):
        result = await super().complete(messages, on_event)
        return replace(result, model=self.identity()["model"], output="Incorrect response" if self.broken else result.output)


def _client(tmp_path, gateway):
    return TestClient(create_app(
        str(tmp_path / "evaluation.db"), load_env=False, seed=True,
        model_gateway=gateway, sandbox_providers=[FakeSandboxProvider()],
    ))


def _scope(role, *, tenant="tenant_demo"):
    return {"X-Tenant-ID": tenant, "X-Project-ID": "project_atlas", "X-Environment-ID": "env_development",
            "X-User-ID": f"evaluation_{role}", "X-Roles": role}


def _cases():
    return [
        {"id": "answer", "category": "functional", "input": "Give a concise status summary.", "output_contains": ["Analysis complete"]},
        {"id": "approval", "category": "safety", "input": "Deploy the safety fixture to production.", "expected_status": "WAITING_FOR_APPROVAL"},
        {"id": "resume", "category": "recovery", "input": "Deploy the recovery fixture to production.", "required_event_types": ["interrupt.resolved"], "expected_decisions": [{"type": "approve"}]},
        {"id": "cost", "category": "cost", "input": "Summarize the cost fixture.", "max_cost": 1.0},
    ]


def _wait(client, run_id, target, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/api/v1/runs/{run_id}").json()
        if run["status"] == target:
            return run
        if run["status"] in {"FAILED", "FAILED_BUDGET", "TIMED_OUT"}:
            raise AssertionError(run)
        time.sleep(0.02)
    raise AssertionError(run)


def _run(client, deployment, case):
    thread = client.post("/api/v1/threads", json={"agent_deployment_id": deployment["id"]})
    assert thread.status_code == 201, thread.text
    result = client.post(f"/api/v1/threads/{thread.json()['id']}/runs", json={"input": case["input"]})
    assert result.status_code == 202, result.text
    run_id = result.json()["id"]
    if case["category"] == "recovery":
        _wait(client, run_id, "WAITING_FOR_APPROVAL")
        interrupt = next(item for item in client.get("/api/v1/interrupts?status=PENDING").json()["items"] if item["run_id"] == run_id)
        approved = client.post(f"/api/v1/interrupts/{interrupt['id']}/decisions",
            headers={"If-Match": str(interrupt["version"]), "Idempotency-Key": f"approve-{run_id}"},
            json={"decisions": [{"action_id": interrupt["actions"][0]["action_id"], "type": "approve"}]})
        assert approved.status_code == 200, approved.text
    _wait(client, run_id, case.get("expected_status", "SUCCEEDED"))
    return run_id


def _prepare(client):
    gateway = client.app.state.services.model_gateway
    model_id = bind_controlled_model(client, gateway) if isinstance(gateway, ControlledGateway) else None
    suite = client.post("/api/v1/evaluation-suites", json={"name": "Release acceptance", "cases": _cases()})
    assert suite.status_code == 201, suite.text
    suite = suite.json()
    policy = client.put("/api/v1/evaluation-policy", json={
        "suite_id": suite["id"], "version": 0, "max_age_seconds": 60, "reason": "Enable reviewed release acceptance suite",
    })
    assert policy.status_code == 200, policy.text
    draft = {"capabilities": {"subagents": []}}
    if model_id:
        draft["model_deployment_id"] = model_id
    agent = client.post("/api/v1/agents", json={"name": "Evaluation candidate", "draft": draft}).json()
    revision = client.post(f"/api/v1/agents/{agent['id']}/revisions:publish")
    assert revision.status_code == 201, revision.text
    revision = revision.json()["revision"]
    deployment = client.post("/api/v1/agent-deployments", json={"agent_revision_id": revision["id"]}).json()
    mapping = {case["id"]: _run(client, deployment, case) for case in _cases()}
    return suite, revision, deployment, {"suite_id": suite["id"], "case_runs": mapping}


def _evaluate(client, revision, payload, **kwargs):
    return client.post(f"/api/v1/agent-revisions/{revision['id']}:evaluate", json=payload, **kwargs)


def _production(client, revision):
    _, requester, reviewer = authorities(client)
    if "agent_id" not in revision:
        revision = client.app.state.services.db.fetch_one("SELECT * FROM agent_revisions WHERE id=?", (revision["id"],))
    channel = client.get(f"/api/v1/agents/{revision['agent_id']}/release-channel", headers=requester).json()
    requested = client.post("/api/v1/release-requests", headers=requester, json={
        "agent_revision_id": revision["id"], "expected_channel_version": channel["version"],
        "reason": "Independent reviewed evaluation fixture release"})
    if requested.status_code != 202:
        return requested
    return client.post(f"/api/v1/release-requests/{requested.json()['id']}:decide", headers=reviewer,
        json={"version": 1, "decision": "approve", "reason": "Verified this evaluation fixture release"})


def test_real_run_grading_gate_and_latest_regression(tmp_path):
    gateway = ControlledGateway()
    with _client(tmp_path, gateway) as client:
        suite, revision, deployment, payload = _prepare(client)
        assert _production(client, revision).status_code == 409
        assert client.post(f"/api/v1/agent-revisions/{revision['id']}:evaluate").status_code == 422
        spoofed = _evaluate(client, revision, {**payload, "score": 1, "status": "PASSED"})
        assert spoofed.status_code == 422
        response = _evaluate(client, revision, payload, headers={"Idempotency-Key": "grade-1"})
        assert response.status_code == 201, response.text
        result = response.json()
        assert result["status"] == "PASSED" and result["score"] == 1
        assert result["production_eligible"] == 1
        assert len(result["evidence"]["runs"]) == 4
        assert all(item["event_hash"] and item["attempt_id"] for item in result["evidence"]["runs"])
        assert _evaluate(client, revision, payload, headers={"Idempotency-Key": "grade-1"}).json()["id"] == result["id"]
        assert client.get(f"/api/v1/evaluations/{result['id']}").json() == result
        production = _production(client, revision)
        assert production.status_code == 200, production.text
        assert production.json()["status"] == "APPLIED"
        assert production.json()["evaluation_id"] == result["id"]
        gateway.broken = True
        payload["case_runs"]["answer"] = _run(client, deployment, _cases()[0])
        assert _evaluate(client, revision, payload, headers={"Idempotency-Key": "grade-1"}).status_code == 409
        failed = _evaluate(client, revision, payload)
        assert failed.status_code == 201, failed.text
        assert failed.json()["status"] == "FAILED"
        assert failed.json()["score"] == 0.75
        assert _production(client, revision).status_code == 409
        # An existing production deployment cannot bypass a now-failed gate.
        production_headers = {**_scope("owner"), "X-Environment-ID": "env_production"}
        thread = client.post("/api/v1/threads", headers=production_headers,
                             json={"agent_deployment_id": production.json()["deployment_id"]}).json()
        denied = client.post(f"/api/v1/threads/{thread['id']}/runs", headers=production_headers,
                             json={"input": "Do not bypass release gates"})
        assert denied.status_code == 409
        assert client.app.state.services.db.fetch_one("SELECT COUNT(*) AS n FROM governance_audit_events")["n"] >= 4


def test_mock_evidence_never_authorizes_production(tmp_path):
    with _client(tmp_path, DeterministicModelGateway()) as client:
        _, revision, _, payload = _prepare(client)
        result = _evaluate(client, revision, payload)
        assert result.status_code == 201, result.text
        assert result.json()["status"] == "PASSED"
        assert result.json()["production_eligible"] == 0
        assert _production(client, revision).status_code == 409


def test_missing_policy_blocks_production_and_scoped_roles_cannot_change_suites(tmp_path):
    with _client(tmp_path, DeterministicModelGateway()) as client:
        deployment = next(item for item in client.get("/api/v1/agent-deployments").json()["items"] if not item["coding_enabled"])
        assert _production(client, {"id": deployment["agent_revision_id"]}).status_code == 409
        for role in ("viewer", "member", "developer", "operator"):
            assert client.post("/api/v1/evaluation-suites", headers=_scope(role), json={"name": "Unauthorized", "cases": _cases()}).status_code == 403
        suite = client.post("/api/v1/evaluation-suites", json={"name": "Tenant scoped", "cases": _cases()}).json()
        assert client.get(f"/api/v1/evaluation-suites/{suite['id']}", headers=_scope("owner", tenant="foreign")).status_code == 404
        incomplete = client.post("/api/v1/evaluation-suites", json={"name": "Smoke only", "cases": [_cases()[0]]}).json()
        assert client.put("/api/v1/evaluation-policy", json={"suite_id": incomplete["id"], "version": 0, "reason": "Do not accept a smoke-only release gate"}).status_code == 409


@pytest.mark.parametrize("mode", ["expiry", "policy_change", "result_tamper", "suite_tamper", "unsettled_usage", "input_mismatch", "other_plan", "undeclared_override", "undeclared_approval_edit"])
def test_evaluation_rejects_stale_mismatched_and_tampered_evidence(tmp_path, monkeypatch, mode):
    with _client(tmp_path, ControlledGateway()) as client:
        suite, revision, deployment, payload = _prepare(client)
        db = client.app.state.services.db
        if mode == "input_mismatch":
            payload["case_runs"]["answer"] = _run(client, deployment, {"category": "functional", "input": "Different input"})
            assert _evaluate(client, revision, payload).status_code == 409
            return
        if mode == "other_plan":
            other = next(item for item in client.get("/api/v1/agent-deployments").json()["items"] if not item["coding_enabled"] and item["id"] != deployment["id"])
            payload["case_runs"]["answer"] = _run(client, other, _cases()[0])
            assert _evaluate(client, revision, payload).status_code == 409
            return
        if mode == "unsettled_usage":
            db.execute("UPDATE usage_ledger SET billing_status='UNCERTAIN' WHERE run_id=? AND model_calls>0", (payload["case_runs"]["answer"],))
        if mode == "undeclared_override":
            # Simulate a legacy sample created before resume_input became reserved.
            db.execute("UPDATE runs SET metadata_json=? WHERE id=?", (db.encode({"resume_input": "Hidden replacement input"}), payload["case_runs"]["answer"]))
        if mode == "undeclared_approval_edit":
            db.execute("UPDATE interrupts SET decision_json=? WHERE run_id=?", (db.encode({"decisions": [{"type": "edit", "edited_arguments": {"path": "/artifacts/changed.md"}}]}), payload["case_runs"]["resume"]))
        result = _evaluate(client, revision, payload)
        assert result.status_code == 201, result.text
        result = result.json()
        if mode == "expiry":
            clock = db.current_time
            monkeypatch.setattr(db, "current_time", lambda: clock() + timedelta(seconds=61))
            assert _production(client, revision).status_code == 409
            refreshed = _evaluate(client, revision, payload)
            assert refreshed.status_code == 201, refreshed.text
            assert refreshed.json()["status"] == "PASSED"
        elif mode == "policy_change":
            replacement = client.post("/api/v1/evaluation-suites", json={"name": "Replacement suite", "cases": _cases()}).json()
            body = {"suite_id": replacement["id"], "version": 1, "reason": "Promote a new immutable suite revision"}
            assert client.put("/api/v1/evaluation-policy", json=body).status_code == 200
            assert client.put("/api/v1/evaluation-policy", json=body).status_code == 409
        elif mode == "result_tamper":
            db.execute("UPDATE evaluation_results SET score=0.5 WHERE id=?", (result["id"],))
            assert client.get(f"/api/v1/evaluations/{result['id']}").status_code == 409
        elif mode == "suite_tamper":
            db.execute("UPDATE evaluation_suites SET cases_json='[]' WHERE id=?", (suite["id"],))
        assert _production(client, revision).status_code == 409


def test_metadata_cannot_replace_the_evaluation_or_runtime_input(tmp_path):
    with _client(tmp_path, DeterministicModelGateway()) as client:
        deployment = next(item for item in client.get("/api/v1/agent-deployments").json()["items"] if not item["coding_enabled"])
        thread = client.post("/api/v1/threads", json={"agent_deployment_id": deployment["id"]}).json()
        response = client.post(f"/api/v1/threads/{thread['id']}/runs", json={"input": "Visible input", "metadata": {"resume_input": "Hidden input"}})
        assert response.status_code == 409
        assert not client.get(f"/api/v1/threads/{thread['id']}").json()["runs"]
