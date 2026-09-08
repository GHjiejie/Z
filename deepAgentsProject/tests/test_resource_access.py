from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest
from fastapi.testclient import TestClient

from apps.platform_api.main import create_app
from apps.platform_api.native_api.routes import stream_run_events
from packages.application.services import NotFoundError
from packages.auth.models import UserCreate
from packages.auth.resource_access import ResourceAccess
from packages.auth.service import AuthAuthorizationError
from packages.domain.models import TenantContext
from packages.runtime.model_gateway import DeterministicModelGateway
from packages.sandbox.fake_provider import FakeSandboxProvider
from test_knowledge import create_indexed_knowledge, create_thread_with_knowledge, wait_for_run
from test_platform import reference_deployment


def headers(user="reader", roles="viewer"):
    return {"X-Tenant-ID": "tenant_demo", "X-Project-ID": "project_atlas",
            "X-Environment-ID": "env_development", "X-User-ID": user, "X-Roles": roles}


def context(user="reader", roles=None):
    return TenantContext(tenant_id="tenant_demo", project_id="project_atlas", user_id=user, roles=roles or ["viewer"])


class EvidenceGateway(DeterministicModelGateway):
    def __init__(self):
        self.calls = 0
        self.waiting = False
        self.cancelled = False

    async def complete(self, messages, on_event=None):
        self.calls += 1
        try:
            while self.waiting:
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        response = await super().complete(messages, on_event)
        for message in messages:
            if message["content"].startswith("The following JSON is untrusted reference data"):
                references = json.loads(message["content"].split("\n", 1)[1])
                return replace(response, output=references[0]["text"] + " [cite_01]")
        return response


@pytest.fixture(params=["sqlite", "postgresql"])
def platform(request, tmp_path, monkeypatch):
    gateway = EvidenceGateway()
    location = str(tmp_path / "access.db")
    admin = None
    schema = None
    monkeypatch.setenv("DEEPAGENT_DATA_DIR", str(tmp_path))
    if request.param == "postgresql":
        url = os.getenv("DEEPAGENT_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("DEEPAGENT_TEST_POSTGRES_URL is required")
        import psycopg
        from psycopg import sql
        admin = psycopg.connect(url, autocommit=True)
        schema = "access_" + secrets.token_hex(10)
        admin.execute("CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA public")
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        query["options"] = f"-csearch_path={schema},public"
        location = urlunsplit(parts._replace(query=urlencode(query)))
    try:
        app = create_app(location, seed=True, load_env=False,
                         model_gateway=gateway, sandbox_providers=[FakeSandboxProvider()])
        with TestClient(app) as client:
            assert app.state.services.db.dialect == request.param
            yield client, app.state.services, gateway
    finally:
        if admin:
            from psycopg import sql
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            admin.close()


def share(client, thread_id, visibility="project", *, version=1, members=None):
    return client.put(f"/api/v1/threads/{thread_id}/access", json={
        "version": version, "visibility": visibility, "members": members or [],
        "reason": "Explicit collaboration consent for this conversation",
    })


def ordinary_run(client, request_headers=None):
    deployment = reference_deployment(client)
    thread = client.post("/api/v1/threads", headers=request_headers, json={"agent_deployment_id": deployment["id"]}).json()
    run = client.post(f"/api/v1/threads/{thread['id']}/runs", headers=request_headers, json={"input": "Summarize this task"}).json()
    return thread, run


def test_private_default_and_explicit_sharing_cover_read_surfaces(platform):
    client, services, _ = platform
    thread, run = ordinary_run(client)
    run = wait_for_run(client, run["id"])
    reader = headers()
    assert thread["visibility"] == "private"
    assert client.get("/api/v1/threads", headers=reader).json()["items"] == []
    assert client.get("/api/v1/runs", headers=reader).json()["items"] == []
    paths = [f"/threads/{thread['id']}", f"/threads/{thread['id']}/access"]
    paths += [f"/runs/{run['id']}" + suffix for suffix in ("", "/events", "/stream", "/artifacts", "/spans", "/children")]
    for path in paths:
        assert client.get("/api/v1" + path, headers=reader).status_code == 404, path
    assert share(client, thread["id"]).status_code == 200
    assert client.get(f"/api/v1/runs/{run['id']}", headers=reader).status_code == 200
    assert share(client, thread["id"], "private").status_code == 409
    assert share(client, thread["id"], "private", version=2).status_code == 200
    assert not ResourceAccess(services.db).can_thread(thread["id"], context())
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM governance_audit_events WHERE action='thread.sharing.updated'")["n"] == 2


@pytest.mark.parametrize("private", [False, True])
def test_source_permissions_survive_answers_sharing_and_future_turns(platform, private):
    client, services, gateway = platform
    kb, prepared, _ = create_indexed_knowledge(client, allowed_roles=["release_manager"])
    if private:
        services.db.execute("UPDATE knowledge_documents SET visibility='private' WHERE id=?", (prepared["document_id"],))
    thread = create_thread_with_knowledge(client, kb["current_revision_id"])
    assert share(client, thread["id"]).status_code == 200
    run = client.post(f"/api/v1/threads/{thread['id']}/runs", json={"input": "为什么发布流程需要人工审批？"}).json()
    run = wait_for_run(client, run["id"])
    assert "所有生产环境发布必须先完成自动化测试" in run["output"]
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM thread_knowledge_sources WHERE thread_id=?", (thread["id"],))["n"] > 0
    viewer = headers(roles="owner" if private else "viewer")
    search = client.post("/api/v1/knowledge:search", headers=viewer,
                         json={"query": "生产发布审批", "revision_ids": [kb["current_revision_id"]]}).json()
    assert search["hits"] == []
    for suffix in ("", "/events", "/stream", "/artifacts", "/spans"):
        assert client.get(f"/api/v1/runs/{run['id']}" + suffix, headers=viewer).status_code == 404
    assert client.get(f"/api/v1/threads/{thread['id']}", headers=viewer).status_code == 404
    assert client.get("/api/v1/runs", headers=viewer).json()["items"] == []
    artifact = services.db.fetch_one("SELECT id FROM artifacts WHERE run_id=? LIMIT 1", (run["id"],))
    assert artifact
    assert client.get(f"/api/v1/runs/{run['id']}/artifacts/{artifact['id']}", headers=viewer).status_code == 404
    assert client.post(f"/api/v1/threads/{thread['id']}/runs", headers=headers("reader", "member"),
                       json={"input": "Repeat the previous answer"}).status_code == 404
    assert gateway.calls == 1
    # Widening the document later must not retrospectively declassify evidence.
    services.db.execute("UPDATE knowledge_documents SET visibility='project',allowed_roles_json='[]' WHERE id=?", (prepared["document_id"],))
    assert client.get(f"/api/v1/runs/{run['id']}", headers=viewer).status_code == 404


def test_current_source_revocation_also_applies_to_the_creator(platform):
    client, services, _ = platform
    kb, prepared, _ = create_indexed_knowledge(client)
    thread = create_thread_with_knowledge(client, kb["current_revision_id"])
    run = client.post(f"/api/v1/threads/{thread['id']}/runs", json={"input": "为什么发布流程需要人工审批？"}).json()
    assert wait_for_run(client, run["id"])["status"] == "SUCCEEDED"
    services.db.execute("UPDATE knowledge_documents SET visibility='private',created_by='different_owner' WHERE id=?", (prepared["document_id"],))
    assert client.get(f"/api/v1/runs/{run['id']}").status_code == 404
    assert client.post(f"/api/v1/threads/{thread['id']}/runs", json={"input": "Continue"}).status_code == 404


def test_members_can_be_read_only_and_sharing_is_owner_only(platform):
    client, services, _ = platform
    member = services.auth.create_user(UserCreate(username="collaborator", display_name="Collaborator", password="Test1234!", roles=["member"]))
    thread, run = ordinary_run(client)
    wait_for_run(client, run["id"])
    assert share(client, thread["id"], "members", members=[{"user_id": member["id"], "access": "read"}]).status_code == 200
    collaborator = headers(member["id"], "member")
    assert client.get(f"/api/v1/runs/{run['id']}", headers=collaborator).status_code == 200
    assert client.post(f"/api/v1/threads/{thread['id']}/runs", headers=collaborator, json={"input": "continue"}).status_code == 404
    response = client.put(f"/api/v1/threads/{thread['id']}/access", headers=collaborator,
        json={"version": 2, "visibility": "project", "reason": "Try to expand sharing"})
    assert response.status_code == 403
    assert share(client, thread["id"], "members", version=2, members=[{"user_id": member["id"], "access": "write"}]).status_code == 200
    assert client.post(f"/api/v1/threads/{thread['id']}/runs", headers=collaborator, json={"input": "continue"}).status_code == 202


def test_stream_rechecks_access_before_the_next_event(platform):
    client, services, _ = platform
    thread, run = ordinary_run(client)
    wait_for_run(client, run["id"])
    assert share(client, thread["id"]).status_code == 200

    async def disconnected():
        return False

    async def consume():
        response = await stream_run_events(run["id"], SimpleNamespace(is_disconnected=disconnected),
            after_sequence=0, channel="all", last_event_id=None, context=context(), container=services)
        iterator = response.body_iterator
        assert "runtime.event" in await anext(iterator)
        services.db.execute("UPDATE threads SET visibility='private',access_version=access_version+1 WHERE id=?", (thread["id"],))
        assert await anext(iterator) == 'event: stream.access_revoked\ndata: {}\n\n'
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)

    asyncio.run(consume())


def test_read_batch_is_authorized_after_loading_not_only_before(platform, monkeypatch):
    client, services, _ = platform
    thread, run = ordinary_run(client)
    wait_for_run(client, run["id"])
    assert share(client, thread["id"]).status_code == 200
    original = services.events.list

    def raced_read(*args, **kwargs):
        events = original(*args, **kwargs)
        services.db.execute("UPDATE threads SET visibility='private' WHERE id=?", (thread["id"],))
        return events

    monkeypatch.setattr(services.events, "list", raced_read)
    assert client.get(f"/api/v1/runs/{run['id']}/events", headers=headers()).status_code == 404


def test_disabled_user_cannot_keep_a_model_call_running(platform):
    client, services, gateway = platform
    actor = services.auth.create_user(UserCreate(username="execution_owner", display_name="Execution owner", password="Test1234!", roles=["member"]))
    gateway.waiting = True
    _, run = ordinary_run(client, headers(actor["id"], "member"))
    deadline = time.monotonic() + 5
    while not gateway.calls and time.monotonic() < deadline:
        time.sleep(0.02)
    assert gateway.calls == 1
    services.db.execute("UPDATE users SET status='INACTIVE',version=version+1 WHERE id=?", (actor["id"],))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = services.db.fetch_one("SELECT status FROM runs WHERE id=?", (run["id"],))["status"]
        if state == "CANCELLED":
            break
        time.sleep(0.02)
    assert state == "CANCELLED"
    assert gateway.cancelled
    assert services.db.fetch_one("SELECT billing_status FROM usage_ledger WHERE run_id=?", (run["id"],))["billing_status"] in {"RESERVED", "UNCERTAIN"}
    with pytest.raises(AuthAuthorizationError):
        ResourceAccess(services.db).require_execution(run["id"])


def test_collaborator_cannot_supply_input_under_another_users_identity(platform):
    client, services, _ = platform
    thread, run = ordinary_run(client)
    wait_for_run(client, run["id"])
    assert share(client, thread["id"]).status_code == 200
    services.db.execute("UPDATE runs SET status='WAITING_FOR_INPUT' WHERE id=?", (run["id"],))
    response = client.post(f"/api/v1/runs/{run['id']}/input", headers=headers("contributor", "member"),
                           json={"input": "Execute using the owner's authority"})
    assert response.status_code == 403
    assert services.db.fetch_one("SELECT status FROM runs WHERE id=?", (run["id"],))["status"] == "WAITING_FOR_INPUT"


def test_source_provenance_failure_prevents_model_delivery(platform, monkeypatch):
    client, services, gateway = platform
    kb, _, _ = create_indexed_knowledge(client, allowed_roles=["release_manager"])
    thread = create_thread_with_knowledge(client, kb["current_revision_id"])
    original = services.db.execute

    def fail_provenance(sql, *args, **kwargs):
        if "INSERT INTO thread_knowledge_sources" in sql:
            raise RuntimeError("injected provenance persistence failure")
        return original(sql, *args, **kwargs)

    monkeypatch.setattr(services.db, "execute", fail_provenance)
    run = client.post(f"/api/v1/threads/{thread['id']}/runs", json={"input": "为什么发布流程需要人工审批？"}).json()
    result = wait_for_run(client, run["id"])
    assert result["status"] == "FAILED"
    assert gateway.calls == 0
    assert "所有生产环境发布必须先完成自动化测试" not in (result["output"] or "")


def test_hidden_event_pages_do_not_hide_later_visible_events(platform):
    client, services, _ = platform
    _, run = ordinary_run(client)
    wait_for_run(client, run["id"])
    cursor = services.db.fetch_one("SELECT MAX(sequence) AS n FROM run_events WHERE run_id=?", (run["id"],))["n"]
    with services.db.transaction():
        for index in range(500):
            services.events.append(run["id"], "internal.detail", {"private": index}, visibility="internal")
        services.events.append(run["id"], "public.detail", {"text": "visible"})
    first = client.get(f"/api/v1/runs/{run['id']}/events?after_sequence={cursor}").json()
    assert first["items"] == [] and first["has_more"]
    assert first["next_sequence"] == cursor + 500
    second = client.get(f"/api/v1/runs/{run['id']}/events?after_sequence={first['next_sequence']}").json()
    assert [event["type"] for event in second["items"]] == ["public.detail"]


def test_legacy_threads_recover_owner_and_sources_without_becoming_public(platform):
    client, services, _ = platform
    kb, _, _ = create_indexed_knowledge(client, allowed_roles=["release_manager"])
    thread = create_thread_with_knowledge(client, kb["current_revision_id"])
    run = client.post(f"/api/v1/threads/{thread['id']}/runs", json={"input": "为什么发布流程需要人工审批？"}).json()
    wait_for_run(client, run["id"])
    empty = client.post("/api/v1/threads", json={"agent_deployment_id": reference_deployment(client)["id"]}).json()
    client.portal.call(services.orchestrator.stop)
    client.portal.call(services.knowledge.stop)
    # Simulate the pre-ACL contents, retaining the genuine retrieval audit.
    with services.db.transaction():
        services.db.execute("DELETE FROM thread_knowledge_sources")
        services.db.execute("UPDATE threads SET owner_user_id=NULL,legacy_access=1,visibility='private'")
        services.db.execute("DELETE FROM schema_migrations WHERE version IN (10,11)")
    services.db.initialize()
    restored = services.runs.thread_access(thread["id"], context("user_demo", ["owner"]))
    assert restored["owner_user_id"] == "user_demo" and restored["source_restricted"] and restored["legacy_access"]
    assert share(client, thread["id"]).status_code == 409
    assert not ResourceAccess(services.db).can_thread(thread["id"], context())
    assert services.db.fetch_one("SELECT access_state FROM threads WHERE id=?", (empty["id"],))["access_state"] == "QUARANTINED"
    before = services.db.fetch_one("SELECT COUNT(*) AS n FROM thread_knowledge_sources")["n"]
    services.db.initialize()
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM thread_knowledge_sources")["n"] == before
