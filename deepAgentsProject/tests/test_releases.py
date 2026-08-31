from datetime import timedelta

import pytest

from packages.auth.resource_access import ResourceAccess
from release_helpers import authorities, user_headers
from test_evaluations import ControlledGateway, _prepare, _evaluate, _cases, _run
from test_runtime_concurrency import runtime, race


@pytest.fixture
def release_runtime(runtime):
    client, services, context, _, _ = runtime
    services.model_gateway = ControlledGateway()
    client.portal.call(services.orchestrator.start)
    try:
        suite, revision, deployment, samples = _prepare(client)
        result = _evaluate(client, revision, samples)
        assert result.status_code == 201 and result.json()["production_eligible"] == 1, result.text
        admin, requester, reviewer = authorities(client)
        yield client, services, revision, suite, requester, reviewer, admin
    finally:
        client.portal.call(services.orchestrator.stop)


def request(client, revision, requester, *, version=0, **changes):
    body = {"agent_revision_id": revision["id"], "expected_channel_version": version,
        "reason": "Release reviewed test candidate", **changes}
    response = client.post("/api/v1/release-requests", headers=requester, json=body)
    assert response.status_code == 202, response.text
    return response.json()


def decide(client, item, reviewer, *, decision="approve"):
    return client.post(f"/api/v1/release-requests/{item['id']}:decide", headers=reviewer,
        json={"version": item["version"], "decision": decision, "reason": "Independently reviewed test candidate"})


def test_environment_grants_do_not_grant_runtime_access_or_direct_production(release_runtime):
    client, services, revision, _, requester, reviewer, admin = release_runtime
    outsider = user_headers(services, "ungranted_operator")
    body = {"agent_revision_id": revision["id"], "environment": "staging"}
    assert client.post("/api/v1/agent-deployments", headers=outsider, json=body).status_code == 403
    grant = {"user_id": outsider["X-User-ID"], "environment": "staging", "can_deploy": True,
        "can_approve": False, "version": 0, "reason": "Allow reviewed staging deployment"}
    assert client.put("/api/v1/deployment-environment-grants", headers=requester, json=grant).status_code == 403
    assert client.put("/api/v1/deployment-environment-grants", headers=admin, json=grant).status_code == 200
    assert client.put("/api/v1/deployment-environment-grants", headers=admin, json=grant).status_code == 409
    staging = client.post("/api/v1/agent-deployments", headers=outsider, json=body)
    assert staging.status_code == 201, staging.text
    assert client.post("/api/v1/threads", headers=outsider,
        json={"agent_deployment_id": staging.json()["id"]}).status_code == 404
    for headers, expected in ((requester, 409), (outsider, 403), (reviewer, 403)):
        assert client.post("/api/v1/agent-deployments", headers=headers,
            json={**body, "environment": "production"}).status_code == expected
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM agent_deployments WHERE environment='production'")["n"] == 0
    item = request(client, revision, requester)
    assert client.get(f"/api/v1/release-requests/{item['id']}", headers=outsider).status_code == 404
    assert client.get("/api/v1/release-requests", headers=outsider).json()["items"] == []
    assert all(row["user_id"] == outsider["X-User-ID"] for row in
        client.get("/api/v1/deployment-environment-grants", headers=outsider).json()["items"])


def test_initial_router_and_release_share_the_candidate_lock(release_runtime, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event
    from packages.domain.models import TenantContext

    client, services, revision, _, requester, reviewer, _ = release_runtime
    first = decide(client, request(client, revision, requester), reviewer).json()
    pending = request(client, revision, requester, version=1)
    context = TenantContext(tenant_id="tenant_demo", project_id="project_atlas",
        environment_id="env_production", user_id="router-fixture", roles=["owner"])
    candidates_read, continue_router = Event(), Event()
    original = services.routing._active_deployments

    def pause_after_read(scope):
        candidates = original(scope)
        candidates_read.set()
        assert continue_router.wait(5)
        return candidates

    monkeypatch.setattr(services.routing, "_active_deployments", pause_after_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        router = pool.submit(services.routing._ensure_profile, context)
        try:
            assert candidates_read.wait(5)
            approval = pool.submit(decide, client, pending, reviewer)
            # Before the fix, approval completed here and the router then
            # committed the old (now DRAINING) candidate.
            with pytest.raises(TimeoutError):
                approval.result(timeout=.15)
        finally:
            continue_router.set()
        profile = router.result(timeout=5)
        response = approval.result(timeout=5)
    assert response.status_code == 409, response.text
    target = profile["config"]["target_deployments"]["general"]
    assert target == first["deployment_id"]
    assert services.db.fetch_one("SELECT status FROM agent_deployments WHERE id=?", (target,))["status"] == "ACTIVE"
    fresh = request(client, revision, requester, version=1)
    applied = decide(client, fresh, reviewer)
    assert applied.status_code == 200, applied.text
    assert services.routing._ensure_profile(context)["config"]["target_deployments"]["general"] == applied.json()["deployment_id"]


def test_production_requires_an_independent_atomic_review(release_runtime):
    client, services, revision, _, requester, reviewer, _ = release_runtime
    item = request(client, revision, requester)
    assert item["status"] == "PENDING" and item["deployment_id"] is None
    assert decide(client, item, requester).status_code == 403
    result = decide(client, item, reviewer)
    assert result.status_code == 200, result.text
    applied = result.json()
    assert applied["status"] == "APPLIED" and applied["version"] == 2
    assert applied["decided_by"] != applied["requested_by"]
    assert decide(client, item, reviewer).json() == applied
    assert decide(client, item, reviewer, decision="reject").status_code == 409
    channel = client.get(f"/api/v1/agents/{revision['agent_id']}/release-channel", headers=requester).json()
    assert channel["version"] == 1 and channel["active_deployment_id"] == applied["deployment_id"]
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM agent_deployments WHERE environment='production'")["n"] == 1
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM governance_audit_events WHERE action='release.promote.applied'")["n"] == 1
    production = user_headers(services, "production_member", "member", "env_production")
    thread = client.post("/api/v1/threads", headers=production,
        json={"agent_deployment_id": applied["deployment_id"]})
    assert thread.status_code == 201, thread.text
    run = client.post(f"/api/v1/threads/{thread.json()['id']}/runs", headers=production,
        json={"input": "Give a concise status summary."})
    assert run.status_code == 202, run.text
    assert client.get(f"/api/v1/threads/{thread.json()['id']}", headers=requester).status_code == 404


@pytest.mark.parametrize("change,expected", [
    ("requester_grant", 403), ("reviewer_grant", 403), ("account_disabled", 403),
    ("role_removed", 403), ("grant_reissued", 409), ("policy", 409),
    ("expired", 409), ("model_disabled", 409), ("snapshot_tampered", 409), ("routing_changed", 409),
])
def test_pending_reviews_revalidate_authority_and_evidence(release_runtime, monkeypatch, change, expected):
    client, services, revision, suite, requester, reviewer, admin = release_runtime
    item = request(client, revision, requester)
    if change in {"requester_grant", "reviewer_grant", "grant_reissued"}:
        target = reviewer if change == "reviewer_grant" else requester
        response = client.put("/api/v1/deployment-environment-grants", headers=admin, json={
            "user_id": target["X-User-ID"], "environment": "production", "can_deploy": False,
            "can_approve": False, "version": 1, "reason": "Revoke this release authority"})
        assert response.status_code == 200
        if change == "grant_reissued":
            assert client.put("/api/v1/deployment-environment-grants", headers=admin, json={
                "user_id": target["X-User-ID"], "environment": "production", "can_deploy": True,
                "can_approve": True, "version": 2, "reason": "Issue a new authorization grant"}).status_code == 200
    elif change == "account_disabled":
        services.db.execute("UPDATE users SET status='INACTIVE' WHERE id=?", (requester["X-User-ID"],))
    elif change == "role_removed":
        services.db.execute("UPDATE users SET roles_json=? WHERE id=?", (services.db.encode(["member"]), requester["X-User-ID"]))
    elif change == "policy":
        assert client.put("/api/v1/evaluation-policy", json={"suite_id": suite["id"], "version": 1,
            "reason": "Require a new policy review"}).status_code == 200
    elif change == "expired":
        clock = services.db.current_time
        monkeypatch.setattr(services.db, "current_time", lambda: clock() + timedelta(seconds=3601))
    elif change == "model_disabled":
        plan = services.db.fetch_one("SELECT * FROM resolved_execution_plans WHERE agent_revision_id=?", (revision["id"],))["plan"]
        services.db.execute("UPDATE model_deployments SET status='disabled' WHERE id=?", (plan["model_deployment_revision_id"],))
    elif change == "snapshot_tampered":
        services.db.execute("UPDATE release_requests SET snapshot_json='{}' WHERE id=?", (item["id"],))
    elif change == "routing_changed":
        production = user_headers(services, "routing_change_owner", "owner", "env_production")
        assert client.get("/api/v1/intent-routing/profile", headers=production).status_code == 200
    response = decide(client, item, reviewer)
    assert response.status_code == expected, response.text
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM agent_deployments WHERE environment='production'")["n"] == 0
    assert services.db.fetch_one("SELECT status FROM release_requests WHERE id=?", (item["id"],))["status"] == "PENDING"


def test_concurrent_approval_and_stale_channel_cannot_apply_twice(release_runtime):
    client, services, revision, _, requester, reviewer, _ = release_runtime
    first = request(client, revision, requester)
    second = request(client, revision, requester)
    responses = race(lambda _: decide(client, first, reviewer), count=4)
    assert {response.status_code for response in responses} == {200}
    assert len({response.json()["deployment_id"] for response in responses}) == 1
    assert decide(client, second, reviewer).status_code == 409
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM governance_audit_events WHERE action='release.promote.applied'")["n"] == 1
    assert client.post(f"/api/v1/release-requests/{second['id']}:cancel", headers=requester,
        json={"version": 1, "reason": "Cancel this stale release request"}).json()["status"] == "CANCELLED"
    assert decide(client, second, reviewer).status_code == 409


def test_request_idempotency_rejection_and_audit_failure_are_atomic(release_runtime, monkeypatch):
    client, services, revision, _, requester, reviewer, _ = release_runtime
    body = {"agent_revision_id": revision["id"], "expected_channel_version": 0, "reason": "Idempotent release fixture request"}
    responses = race(lambda _: client.post("/api/v1/release-requests",
        headers={**requester, "Idempotency-Key": "same-release"}, json=body), count=4)
    assert {response.status_code for response in responses} == {202}
    assert len({response.json()["id"] for response in responses}) == 1
    assert client.post("/api/v1/release-requests", headers={**requester, "Idempotency-Key": "same-release"},
        json={**body, "reason": "Different candidate reason"}).status_code == 409
    first = responses[0].json()
    assert decide(client, first, reviewer, decision="reject").json()["status"] == "REJECTED"
    assert decide(client, first, reviewer).status_code == 409
    second = request(client, revision, requester)
    original = services.db.execute

    def fail_audit(sql, params=()):
        if "INSERT INTO governance_audit_events" in sql and "release.promote.applied" in params:
            raise RuntimeError("Injected release audit failure")
        return original(sql, params)

    monkeypatch.setattr(services.db, "execute", fail_audit)
    with pytest.raises(RuntimeError, match="Injected"):
        decide(client, second, reviewer)
    assert services.db.fetch_one("SELECT status FROM release_requests WHERE id=?", (second["id"],))["status"] == "PENDING"
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM release_channels")["n"] == 0
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM agent_deployments WHERE environment='production'")["n"] == 0


def test_rollback_reuses_reviewed_revision_and_old_runs_drain(release_runtime):
    client, services, revision, suite, requester, reviewer, _ = release_runtime
    first = decide(client, request(client, revision, requester), reviewer).json()
    production = user_headers(services, "draining_owner", "owner", "env_production")
    router = client.get("/api/v1/intent-routing/profile", headers=production).json()
    assert router["config"]["target_deployments"]["general"] == first["deployment_id"]
    thread = client.post("/api/v1/threads", headers=production,
        json={"agent_deployment_id": first["deployment_id"]}).json()
    run = client.post(f"/api/v1/threads/{thread['id']}/runs", headers=production,
        json={"input": "Deploy this production fixture after approval."}).json()
    # Use the production principal to observe its private Run.
    import time
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if client.get(f"/api/v1/runs/{run['id']}", headers=production).json()["status"] == "WAITING_FOR_APPROVAL":
            break
        time.sleep(0.02)
    assert client.get(f"/api/v1/runs/{run['id']}", headers=production).json()["status"] == "WAITING_FOR_APPROVAL"
    second_revision = client.post(f"/api/v1/agents/{revision['agent_id']}/revisions:publish").json()["revision"]
    development = client.post("/api/v1/agent-deployments", json={"agent_revision_id": second_revision["id"]}).json()
    mapping = {case["id"]: _run(client, development, case) for case in _cases()}
    result = _evaluate(client, second_revision, {"suite_id": suite["id"], "case_runs": mapping})
    assert result.status_code == 201 and result.json()["production_eligible"] == 1, result.text
    second = decide(client, request(client, second_revision, requester, version=1), reviewer).json()
    assert second["status"] == "APPLIED"
    promoted_router = client.get("/api/v1/intent-routing/profile", headers=production).json()
    assert promoted_router["id"] != router["id"]
    assert promoted_router["config"]["target_deployments"]["general"] == second["deployment_id"]
    assert services.db.fetch_one("SELECT status FROM agent_deployments WHERE id=?", (first["deployment_id"],))["status"] == "DRAINING"
    ResourceAccess(services.db).require_execution(run["id"])
    assert client.post("/api/v1/threads", headers=production,
        json={"agent_deployment_id": first["deployment_id"]}).status_code == 404
    rollback = request(client, revision, requester, version=2, action="rollback", rollback_deployment_id=first["deployment_id"])
    restored = decide(client, rollback, reviewer)
    assert restored.status_code == 200, restored.text
    restored_deployment = services.db.fetch_one("SELECT * FROM agent_deployments WHERE id=?", (restored.json()["deployment_id"],))
    assert restored_deployment["agent_revision_id"] == revision["id"]
    assert restored_deployment["release_request_id"] == rollback["id"]
    assert client.get("/api/v1/intent-routing/profile", headers=production).json()["config"]["target_deployments"]["general"] == restored.json()["deployment_id"]
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM agent_deployments WHERE environment='production' AND status='ACTIVE'")["n"] == 1
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM governance_audit_events WHERE action='release.rollback.applied'")["n"] == 1


def test_legacy_production_has_no_inferred_approval(runtime):
    client, services, _, deployment_id, _ = runtime
    services.db.execute("UPDATE agent_deployments SET environment='production' WHERE id=?", (deployment_id,))
    production = user_headers(services, "legacy_production_member", "member", "env_production")
    response = client.post("/api/v1/threads", headers=production, json={"agent_deployment_id": deployment_id})
    assert response.status_code == 409 and "reviewed release" in response.text
