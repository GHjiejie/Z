from __future__ import annotations

import pytest

from packages.auth.models import UserCreate
from packages.auth.resource_access import ResourceAccess
from packages.auth.service import AuthAuthorizationError
from packages.domain.models import TenantContext
from packages.persistence.pagination import authorized_page, PageAccessChanged
from test_runtime_concurrency import runtime


def member(services, name, environment="env_development"):
    user = services.auth.create_user(UserCreate(username=name, display_name=name,
        password="Boundary-Test-2026!", roles=["member"], environment_id=environment))
    return {"X-Tenant-ID": "tenant_demo", "X-Project-ID": "project_atlas",
            "X-Environment-ID": environment, "X-User-ID": user["id"], "X-Roles": "member"}


def create_run(client, deployment_id, headers=None, text="Boundary check"):
    thread = client.post("/api/v1/threads", headers=headers, json={"agent_deployment_id": deployment_id})
    assert thread.status_code == 201, thread.text
    run = client.post(f"/api/v1/threads/{thread.json()['id']}/runs", headers=headers, json={"input": text})
    assert run.status_code == 202, run.text
    return thread.json(), run.json()


def test_environment_boundary_covers_manual_routing_sharing_and_saved_execution(runtime):
    client, services, _, deployment_id, _ = runtime
    source = services.db.fetch_one("SELECT * FROM agent_deployments WHERE id=?", (deployment_id,))
    staging = client.post("/api/v1/agent-deployments", headers={
        "X-Tenant-ID": "tenant_demo", "X-Project-ID": "project_atlas", "X-Environment-ID": "env_staging",
        "X-User-ID": "staging_fixture_operator", "X-Roles": "operator"}, json={
        "agent_revision_id": source["agent_revision_id"], "environment": "staging"}).json()
    development_user = member(services, "development_member")
    staging_user = member(services, "staging_member", "env_staging")
    assert staging["id"] not in {item["id"] for item in client.get("/api/v1/agent-deployments").json()["items"]}
    assert client.post("/api/v1/intent-routing:resolve", headers=development_user,
        json={"input": "write a poem", "preferred_deployment_id": staging["id"]}).status_code == 404
    assert client.post("/api/v1/threads", headers=development_user,
        json={"agent_deployment_id": staging["id"]}).status_code == 404
    thread, run = create_run(client, staging["id"], staging_user)
    shared = client.put(f"/api/v1/threads/{thread['id']}/access", headers=staging_user,
        json={"version": 1, "visibility": "project", "reason": "Share with this environment"})
    assert shared.status_code == 200
    for path in (f"/threads/{thread['id']}", f"/runs/{run['id']}", f"/runs/{run['id']}/events",
                 f"/runs/{run['id']}/stream", f"/runs/{run['id']}/artifacts"):
        assert client.get("/api/v1" + path, headers=development_user).status_code == 404, path
    assert client.get("/api/v1/threads", headers=development_user).json()["items"] == []
    assert client.get("/api/v1/runs", headers=development_user).json()["items"] == []
    assert client.post(f"/api/v1/runs/{run['id']}/input", headers=development_user,
        json={"input": "Continue"}).status_code == 404
    assert client.get(f"/api/v1/threads/{thread['id']}/sharing-candidates", headers=staging_user).json()["items"] == [
        {key: services.db.fetch_one("SELECT * FROM users WHERE id=?", (staging_user["X-User-ID"],))[key]
         for key in ("id", "username", "display_name")}]
    assert client.put(f"/api/v1/threads/{thread['id']}/access", headers=staging_user,
        json={"version": 2, "visibility": "members", "reason": "Invalid cross-environment share",
              "members": [{"user_id": development_user["X-User-ID"], "access": "read"}]}).status_code == 404
    # Existing bad records from before the fix must not execute after upgrade.
    services.db.execute("UPDATE runs SET principal_environment_id=?,principal_user_id=? WHERE id=?",
                       ("env_development", development_user["X-User-ID"], run["id"]))
    with pytest.raises(AuthAuthorizationError, match="deployment access"):
        ResourceAccess(services.db).require_execution(run["id"])


def test_authorized_run_and_thread_pages_have_stable_cursors_and_server_filters(runtime):
    client, services, _, deployment_id, _ = runtime
    owner = member(services, "pagination_owner")
    stranger = member(services, "pagination_stranger")
    mine = [create_run(client, deployment_id, owner, f"needle-{index}") for index in range(3)]
    for thread, run in mine:
        services.db.execute("UPDATE threads SET created_at=? WHERE id=?", ("2026-01-01T00:00:00+00:00", thread["id"]))
        services.db.execute("UPDATE runs SET created_at=? WHERE id=?", ("2026-01-01T00:00:00+00:00", run["id"]))
    create_run(client, deployment_id, stranger, "A newer private request")
    for resource, offset in (("runs", 1), ("threads", 0)):
        expected = sorted([pair[offset]["id"] for pair in mine], reverse=True)
        path = "/api/v1/" + resource
        page = client.get(path, params={"limit": 1}, headers=owner).json()
        assert len(page["items"]) == 1 and page["has_more"]
        assert client.get(path, params={"cursor": page["next_cursor"]}, headers=stranger).status_code == 400
        assert client.get(path, params={"cursor": "not-a-cursor"}, headers=owner).status_code == 400
        ids = [page["items"][0]["id"]]
        while page["has_more"]:
            page = client.get(path, params={"limit": 1, "cursor": page["next_cursor"]}, headers=owner).json()
            ids.extend(item["id"] for item in page["items"])
        assert ids == expected
        assert page["next_cursor"] is None
    search = client.get("/api/v1/runs", params={"limit": 1, "q": "needle-1", "status": "QUEUED"}, headers=owner).json()
    assert [item["id"] for item in search["items"]] == [mine[1][1]["id"]]
    assert not search["has_more"]
    assert client.get("/api/v1/runs", params={"status": "UNKNOWN"}, headers=owner).status_code == 422


def test_page_fills_across_restricted_batches_and_checks_delivery_again(runtime):
    _, services, context, _, _ = runtime
    # Isolate the batching contract with an actual SQL result set. The source
    # ACL integration is separately exercised by test_resource_access.
    query = """WITH RECURSIVE numbers(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM numbers WHERE n<120)
        SELECT CAST(n AS TEXT) AS id,'2026-01-01T00:00:00+00:00' AS created_at FROM numbers"""
    parameters = dict(db=services.db, query=f"SELECT r.* FROM ({query}) r WHERE 1=1", params=(),
                      alias="r", resource="batch-test", context=context, limit=1)
    page = authorized_page(**parameters, visible=lambda row: row["id"] in {"1", "2"})
    assert [item["id"] for item in page["items"]] == ["2"] and page["has_more"]
    next_page = authorized_page(**parameters, cursor=page["next_cursor"], visible=lambda row: row["id"] in {"1", "2"})
    assert [item["id"] for item in next_page["items"]] == ["1"] and not next_page["has_more"]
    checked = set()
    def revoked(row):
        if row["id"] in checked:
            return False
        checked.add(row["id"])
        return True
    with pytest.raises(PageAccessChanged):
        authorized_page(**parameters, visible=revoked)


def test_routing_decisions_filter_before_pagination(runtime):
    client, services, _, _, _ = runtime
    owner = member(services, "routing_page_owner")
    stranger = member(services, "routing_page_stranger")
    first = client.post("/api/v1/intent-routing:resolve", headers=owner, json={"input": "write a poem"}).json()
    client.post("/api/v1/intent-routing:resolve", headers=stranger, json={"input": "write a poem"})
    page = client.get("/api/v1/intent-routing/decisions?limit=1", headers=owner).json()
    assert [item["id"] for item in page["items"]] == [first["id"]]
    assert not page["has_more"]
