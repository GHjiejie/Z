import asyncio
import hashlib

from packages.auth.models import UserCreate
from packages.domain.models import TenantContext
from test_runtime_concurrency import runtime


def identity(services, name, role="developer"):
    user = services.auth.create_user(UserCreate(username=name, display_name=name,
        password="Metadata-Test-2026!", roles=[role]))
    return {"X-Tenant-ID": "tenant_demo", "X-Project-ID": "project_atlas",
            "X-Environment-ID": "env_development", "X-User-ID": user["id"], "X-Roles": role}


def upload(client, kb, headers, *, visibility="private", roles=None):
    content = b"Private deployment knowledge metadata fixture."
    response = client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents:prepare-upload",
        headers=headers, json={"filename": "metadata.txt", "content_type": "text/plain",
        "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
        "visibility": visibility, "allowed_roles": roles or []})
    assert response.status_code == 201, response.text
    prepared = response.json()
    assert client.put(prepared["upload"]["url"], content=content,
        headers={**headers, "Content-Type": "text/plain"}).status_code == 200
    response = client.post(f"/api/v1/knowledge-document-versions/{prepared['document_version_id']}:complete",
        headers=headers, json={})
    assert response.status_code == 202, response.text
    return prepared, response.json()


def test_private_metadata_and_live_lease_are_never_public(runtime):
    client, services, _, _, _ = runtime
    owner = identity(services, "metadata_owner")
    reader = identity(services, "metadata_reader", "member")
    kb = client.post("/api/v1/knowledge-bases", headers=owner, json={"name": "Private metadata"}).json()
    document, job = upload(client, kb, owner)
    forbidden = {"lease_token", "worker_id", "requested_by", "requested_roles", "requested_environment_id", "heartbeat_at"}
    assert not forbidden.intersection(job)
    assert services.knowledge._claim_job(job["id"], "internal-lease-fixture")
    own_job = client.get(f"/api/v1/knowledge-ingestion-jobs/{job['id']}", headers=owner)
    assert own_job.status_code == 200
    assert own_job.json()["status"] == "RUNNING"
    assert not forbidden.intersection(own_job.json())
    replay = client.post(f"/api/v1/knowledge-document-versions/{document['document_version_id']}:complete",
        headers=owner, json={})
    assert replay.status_code == 202
    assert not forbidden.intersection(replay.json())
    for headers in (reader, {**reader, "X-Project-ID": "other_project"}):
        response = client.get(f"/api/v1/knowledge-ingestion-jobs/{job['id']}", headers=headers)
        assert response.status_code in {403, 404}
        assert client.post(f"/api/v1/knowledge-ingestion-jobs/{job['id']}:retry", headers=headers).status_code in {403, 404}
    events = client.get("/api/v1/knowledge-events", headers=reader).json()["items"]
    assert all(event.get("document_version_id") != document["document_version_id"] for event in events)
    assert all(event.get("ingestion_job_id") != job["id"] for event in events)
    assert client.get(f"/api/v1/knowledge-documents/{document['document_id']}", headers=reader).status_code == 404
    assert client.get("/api/v1/knowledge-bases", headers=reader).json()["items"][0]["document_count"] == 0
    assert client.get("/api/v1/knowledge-bases", headers=owner).json()["items"][0]["document_count"] == 1


def test_revision_manifest_search_events_and_pagination_respect_identity(runtime):
    client, services, _, _, _ = runtime
    owner = identity(services, "revision_owner")
    reader = identity(services, "revision_reader", "member")
    kb = client.post("/api/v1/knowledge-bases", headers=owner, json={"name": "Revision metadata"}).json()
    document, job = upload(client, kb, owner)
    asyncio.run(services.knowledge._process_job(job["id"]))
    assert client.get(f"/api/v1/knowledge-ingestion-jobs/{job['id']}", headers=owner).json()["status"] == "SUCCEEDED"
    detail = client.get(f"/api/v1/knowledge-bases/{kb['id']}", headers=reader).json()
    assert detail["documents"] == []
    assert detail["revisions"] and all("manifest" not in row for row in detail["revisions"])
    assert document["document_version_id"] not in str(detail)
    raw = services.db.fetch_one("SELECT * FROM knowledge_base_revisions WHERE knowledge_base_id=?", (kb["id"],))
    assert document["document_version_id"] in str(raw["manifest"])
    for headers in (owner, reader):
        assert client.post("/api/v1/knowledge:search", headers=headers,
            json={"knowledge_base_id": kb["id"], "query": "deployment"}).status_code == 200
    owner_context = TenantContext(tenant_id="tenant_demo", project_id="project_atlas",
        user_id=owner["X-User-ID"], roles=["developer"])
    # A later private event cannot crowd public rows out of an authorized page.
    for _ in range(60):
        services.knowledge._append_event(owner_context, "knowledge.ingestion.started", {"worker_id": "internal"},
            knowledge_base_id=kb["id"], document_version_id=document["document_version_id"], ingestion_job_id=job["id"])
    page = client.get("/api/v1/knowledge-events?limit=1", headers=reader).json()
    assert page["items"][0]["type"] == "knowledge.search.completed"
    assert page["has_more"]
    assert client.get("/api/v1/knowledge-events", headers=owner,
        params={"cursor": page["next_cursor"]}).status_code == 400
    second = client.get("/api/v1/knowledge-events", headers=reader,
        params={"limit": 1, "cursor": page["next_cursor"]}).json()
    assert second["items"][0]["type"] == "knowledge.base.created"
    assert not second["has_more"]
    services.db.execute("UPDATE knowledge_events SET actor_user_id=NULL WHERE type='knowledge.search.completed'")
    assert not any(event["type"] == "knowledge.search.completed" for event in
        client.get("/api/v1/knowledge-events", headers=reader).json()["items"])


def test_role_revocation_also_hides_previously_visible_metadata(runtime):
    client, services, _, _, _ = runtime
    owner = identity(services, "role_owner")
    reader = identity(services, "role_reader")
    kb = client.post("/api/v1/knowledge-bases", headers=owner, json={"name": "Role-bound metadata"}).json()
    _, job = upload(client, kb, owner, visibility="project", roles=["developer"])
    assert client.get(f"/api/v1/knowledge-ingestion-jobs/{job['id']}", headers=reader).status_code == 200
    services.db.execute("UPDATE users SET roles_json=? WHERE id=?", (services.db.encode(["member"]), reader["X-User-ID"]))
    headers = {**reader, "X-Roles": "member"}
    assert client.get(f"/api/v1/knowledge-ingestion-jobs/{job['id']}", headers=headers).status_code == 404
    assert all(event.get("ingestion_job_id") != job["id"] for event in
        client.get("/api/v1/knowledge-events", headers=headers).json()["items"])
