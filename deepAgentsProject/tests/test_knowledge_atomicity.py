from __future__ import annotations

import hashlib
import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from threading import Barrier, Event

import pytest

from packages.auth.models import UserUpdate
from packages.auth.service import AuthService, AuthAuthorizationError, AuthenticationError
from packages.domain.models import TenantContext
from packages.knowledge.errors import KnowledgeConflictError, KnowledgeValidationError
from packages.knowledge.models import KnowledgeBaseCreate, UploadPrepare
from packages.knowledge.ports import UploadAuthorization
from packages.knowledge.service import KnowledgeService
from packages.persistence import create_database
from test_auth_atomicity import account
from test_runtime_concurrency import runtime


def upload_payload(**changes):
    return UploadPrepare(**{
        "filename": "atomic.txt", "content_type": "text/plain", "size_bytes": 7,
        "sha256": hashlib.sha256(b"fixture").hexdigest(), **changes,
    })


def snapshot(db):
    return {table: db.fetch_all(f"SELECT * FROM {table} ORDER BY {order}")
            for table, order in (("knowledge_bases", "id"), ("knowledge_documents", "id"),
                                 ("knowledge_document_versions", "id"), ("knowledge_events", "id"),
                                 ("idempotency_records", "tenant_id,scope,key"))}


def setup_operation(runtime, operation):
    _, services, context, *_ = runtime
    service = services.knowledge
    if operation == "base":
        return lambda key=None: service.create_knowledge_base(
            KnowledgeBaseCreate(name="Atomic knowledge"), context,
            **({"idempotency_key": key} if key is not None else {})), "id"
    base = service.create_knowledge_base(KnowledgeBaseCreate(name="Atomic uploads"), context)
    return lambda key=None: service.prepare_upload(base["id"], upload_payload(), context,
        **({"idempotency_key": key} if key is not None else {})), "document_version_id"


@pytest.mark.parametrize("operation,table", [
    ("base", "knowledge_bases"), ("base", "knowledge_events"),
    ("upload", "knowledge_documents"), ("upload", "knowledge_document_versions"),
    ("upload", "knowledge_events"),
])
@pytest.mark.parametrize("after_write", [False, True])
def test_knowledge_writes_rollback_at_every_step(runtime, monkeypatch, operation, table, after_write):
    _, services, *_ = runtime
    invoke, _ = setup_operation(runtime, operation)
    before = snapshot(services.db)
    original = services.db.execute

    def fail(sql, params=()):
        targeted = f"INSERT INTO {table}" in sql
        if targeted and not after_write:
            raise RuntimeError("injected knowledge write failure")
        result = original(sql, params)
        if targeted:
            raise RuntimeError("injected knowledge write failure")
        return result

    monkeypatch.setattr(services.db, "execute", fail)
    with pytest.raises(RuntimeError, match="injected knowledge write failure"):
        invoke()
    assert snapshot(services.db) == before
    monkeypatch.setattr(services.db, "execute", original)
    invoke()


@pytest.mark.parametrize("operation", ["base", "upload"])
@pytest.mark.parametrize("after_write", [False, True])
def test_idempotency_record_failure_rolls_back_business_rows(runtime, monkeypatch, operation, after_write):
    _, services, *_ = runtime
    invoke, field = setup_operation(runtime, operation)
    before = snapshot(services.db)
    original = services.db.execute

    def fail(sql, params=()):
        targeted = "INSERT INTO idempotency_records" in sql
        if targeted and not after_write:
            raise RuntimeError("injected idempotency failure")
        result = original(sql, params)
        if targeted:
            raise RuntimeError("injected idempotency failure")
        return result

    monkeypatch.setattr(services.db, "execute", fail)
    with pytest.raises(RuntimeError, match="injected idempotency failure"):
        invoke("atomic-request")
    assert snapshot(services.db) == before
    monkeypatch.setattr(services.db, "execute", original)
    assert invoke("atomic-request")[field] == invoke("atomic-request")[field]


def test_signing_failure_leaves_no_partial_upload(runtime, monkeypatch):
    _, services, *_ = runtime
    invoke, _ = setup_operation(runtime, "upload")
    before = snapshot(services.db)

    def unavailable(*args, **kwargs):
        raise RuntimeError("signing unavailable")

    monkeypatch.setattr(services.knowledge.storage, "create_upload_authorization", unavailable)
    with pytest.raises(RuntimeError, match="signing unavailable"):
        invoke("signing-retry")
    assert snapshot(services.db) == before


@pytest.mark.parametrize("operation", ["base", "upload"])
def test_lost_response_retry_preserves_one_resource_and_one_event(runtime, operation):
    _, services, *_ = runtime
    invoke, field = setup_operation(runtime, operation)
    first = invoke("lost-response")
    before = snapshot(services.db)
    assert invoke("lost-response")[field] == first[field]
    assert snapshot(services.db) == before
    assert invoke("separate-action")[field] != first[field]


@pytest.mark.parametrize("operation", ["base", "upload"])
def test_independent_connections_serialize_same_idempotency_key(runtime, operation):
    _, services, context, _, location = runtime
    base = services.knowledge.create_knowledge_base(KnowledgeBaseCreate(name="Concurrent upload"), context)
    peers = [create_database(location) for _ in range(4)]
    barrier = Barrier(len(peers))

    def invoke(db):
        service = KnowledgeService(db, services.knowledge.storage, services.knowledge.embedding)
        barrier.wait(timeout=10)
        if operation == "base":
            return service.create_knowledge_base(KnowledgeBaseCreate(name="Concurrent base"), context, "concurrent-key")["id"]
        return service.prepare_upload(base["id"], upload_payload(), context, "concurrent-key")["document_version_id"]

    try:
        with ThreadPoolExecutor(max_workers=len(peers)) as pool:
            results = list(pool.map(invoke, peers))
        assert len(set(results)) == 1
        event_type = "knowledge.base.created" if operation == "base" else "knowledge.upload.prepared"
        resource = "knowledge_base_id" if operation == "base" else "document_version_id"
        assert services.db.fetch_one(f"SELECT COUNT(*) AS n FROM knowledge_events WHERE type=? AND {resource}=?",
                                     (event_type, results[0]))["n"] == 1
    finally:
        for db in peers:
            db.close()


def test_request_keys_bind_full_payload_and_principal_and_scope(runtime):
    _, services, context, *_ = runtime
    service = services.knowledge
    base = service.create_knowledge_base(KnowledgeBaseCreate(name="First"), context, "scope-key")
    with pytest.raises(KnowledgeConflictError):
        service.create_knowledge_base(KnowledgeBaseCreate(name="Changed"), context, "scope-key")
    with pytest.raises(KnowledgeConflictError):
        service.create_knowledge_base(KnowledgeBaseCreate(name="First"), context.model_copy(update={"user_id": "someone-else"}), "scope-key")
    first = service.prepare_upload(base["id"], upload_payload(), context, "scope-key")
    for change in ({"filename": "changed.txt"}, {"sha256": "0" * 64}, {"visibility": "private"},
                   {"allowed_roles": ["developer"]}, {"description": "changed"}, {"size_bytes": 8},
                   {"content_type": "text/markdown"}):
        with pytest.raises(KnowledgeConflictError):
            service.prepare_upload(base["id"], upload_payload(**change), context, "scope-key")
    for field in ("project_id", "tenant_id", "environment_id"):
        other = service.create_knowledge_base(KnowledgeBaseCreate(name="First"),
            context.model_copy(update={field: "different"}), "scope-key")
        assert other["id"] != base["id"]
    assert service.prepare_upload(base["id"], upload_payload(), context, "scope-key")["document_version_id"] == first["document_version_id"]


@pytest.mark.parametrize("key", ["", " ", "x" * 201, "a\nb", "汉字"])
def test_invalid_keys_rejected_before_writes(runtime, key):
    _, services, *_ = runtime
    for operation in ("base", "upload"):
        invoke, _ = setup_operation(runtime, operation)
        before = snapshot(services.db)
        with pytest.raises(KnowledgeValidationError):
            invoke(key)
        assert snapshot(services.db) == before


def test_upload_retry_renews_authorization_without_persisting_it(runtime, monkeypatch):
    _, services, *_ = runtime
    invoke, _ = setup_operation(runtime, "upload")
    issued = []

    def sign(key, content_type, expires_seconds=900, *, size_bytes):
        assert size_bytes == 7
        issued.append(key)
        return UploadAuthorization(method="PUT", url=f"https://upload.invalid/synthetic-secret-{len(issued)}",
                                   expires_at="2099-01-01T00:00:00+00:00")

    monkeypatch.setattr(services.knowledge.storage, "create_upload_authorization", sign)
    first, second = invoke("renew-url"), invoke("renew-url")
    assert len(issued) == 2 and issued[0] == issued[1]
    assert first["document_version_id"] == second["document_version_id"]
    assert first["upload"]["url"] != second["upload"]["url"]
    assert "synthetic-secret" not in services.db.encode(snapshot(services.db))


@pytest.mark.parametrize("status", ["UPLOADED", "INGESTING", "READY", "FAILED"])
def test_retry_never_reauthorizes_a_consumed_upload(runtime, monkeypatch, status):
    _, services, *_ = runtime
    invoke, _ = setup_operation(runtime, "upload")
    first = invoke("finished-upload")
    services.db.execute("UPDATE knowledge_document_versions SET status=? WHERE id=?", (status, first["document_version_id"]))

    def forbidden(*args, **kwargs):
        raise AssertionError("Consumed object must not receive another write grant")

    monkeypatch.setattr(services.knowledge.storage, "create_upload_authorization", forbidden)
    replay = invoke("finished-upload")
    assert replay["document_version_id"] == first["document_version_id"]
    assert replay["status"] == status and replay["upload"] is None


@pytest.mark.parametrize("change", ["roles", "inactive", "session"])
def test_retries_recheck_current_permissions(runtime, change):
    _, services, *_ = runtime
    owner = account(services, "knowledge.author", roles=["developer"])
    context = TenantContext(**{field: getattr(owner.actor, field) for field in TenantContext.model_fields})
    service = services.knowledge
    payload = KnowledgeBaseCreate(name="Private work")
    base = service.create_knowledge_base(payload, context, "author-base")
    source = upload_payload(visibility="private")
    service.prepare_upload(base["id"], source, context, "author-upload")
    if change == "roles":
        services.db.execute("UPDATE users SET roles_json='[\"viewer\"]' WHERE id=?", (context.user_id,))
    elif change == "inactive":
        services.db.execute("UPDATE users SET status='INACTIVE' WHERE id=?", (context.user_id,))
    else:
        services.db.execute("UPDATE auth_sessions SET revoked_at=created_at WHERE id=?", (context.session_id,))
    before = snapshot(services.db)
    with pytest.raises((AuthAuthorizationError, AuthenticationError)):
        service.create_knowledge_base(payload, context, "author-base")
    with pytest.raises((AuthAuthorizationError, AuthenticationError)):
        service.prepare_upload(base["id"], source, context, "author-upload")
    assert snapshot(services.db) == before


def test_http_idempotency_contract(runtime):
    client, services, *_ = runtime
    headers = {"Idempotency-Key": "browser-retry"}
    first = client.post("/api/v1/knowledge-bases", json={"name": "HTTP retry"}, headers=headers)
    second = client.post("/api/v1/knowledge-bases", json={"name": "HTTP retry"}, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    path = f"/api/v1/knowledge-bases/{first.json()['id']}/documents:prepare-upload"
    prepared = client.post(path, json=upload_payload().model_dump(), headers=headers)
    replay = client.post(path, json=upload_payload().model_dump(), headers=headers)
    assert prepared.status_code == replay.status_code == 201
    assert prepared.json()["document_version_id"] == replay.json()["document_version_id"]
    conflict = client.post(path, json=upload_payload(filename="changed.txt").model_dump(), headers=headers)
    assert conflict.status_code == 409
    for endpoint, body in (("/api/v1/knowledge-bases", {"name": "Invalid key"}), (path, upload_payload().model_dump())):
        before = snapshot(services.db)
        assert client.post(endpoint, json=body, headers={"Idempotency-Key": "x" * 201}).status_code == 422
        assert snapshot(services.db) == before


@pytest.mark.parametrize("operation", ["base", "upload"])
def test_read_only_principal_cannot_create_even_without_http(runtime, operation):
    _, services, context, *_ = runtime
    base = services.knowledge.create_knowledge_base(KnowledgeBaseCreate(name="Read only"), context)
    viewer = context.model_copy(update={"roles": ["viewer"]})
    before = snapshot(services.db)
    with pytest.raises(AuthAuthorizationError):
        if operation == "base":
            services.knowledge.create_knowledge_base(KnowledgeBaseCreate(name="Forbidden"), viewer)
        else:
            services.knowledge.prepare_upload(base["id"], upload_payload(), viewer)
    assert snapshot(services.db) == before


@pytest.mark.parametrize("record", ['{}', '[]', 'not-json', '{"version":1,"resource_id":"arbitrary"}'])
def test_corrupt_replay_records_fail_closed(runtime, record):
    _, services, *_ = runtime
    invoke, _ = setup_operation(runtime, "upload")
    invoke("corrupt-record")
    services.db.execute("UPDATE idempotency_records SET response_json=? WHERE key=?", (record, "corrupt-record"))
    before = snapshot(services.db)
    with pytest.raises(KnowledgeConflictError):
        invoke("corrupt-record")
    assert snapshot(services.db) == before


def test_retry_rechecks_document_permissions_before_signing(runtime, monkeypatch):
    _, services, *_ = runtime
    owner = account(services, "document.author", roles=["developer"])
    context = TenantContext(**{field: getattr(owner.actor, field) for field in TenantContext.model_fields})
    base = services.knowledge.create_knowledge_base(KnowledgeBaseCreate(name="Document policy"), context)
    source = upload_payload(allowed_roles=["developer"])
    first = services.knowledge.prepare_upload(base["id"], source, context, "document-policy")
    services.db.execute("UPDATE knowledge_documents SET allowed_roles_json='[\"restricted\"]' WHERE id=?", (first["document_id"],))

    def forbidden(*args, **kwargs):
        raise AssertionError("An inaccessible source must never be signed")

    monkeypatch.setattr(services.knowledge.storage, "create_upload_authorization", forbidden)
    from packages.knowledge.errors import KnowledgeNotFoundError
    with pytest.raises(KnowledgeNotFoundError):
        services.knowledge.prepare_upload(base["id"], source, context, "document-policy")


def test_completion_winning_the_version_lock_prevents_upload_renewal(runtime, monkeypatch):
    _, services, *_ , location = runtime
    invoke, _ = setup_operation(runtime, "upload")
    first = invoke("completion-race")
    peer = create_database(location)
    entered, release, attempted = Event(), Event(), Event()

    def complete():
        with peer.transaction():
            peer.execute("UPDATE knowledge_document_versions SET status='UPLOADED' WHERE id=?", (first["document_version_id"],))
            entered.set()
            assert release.wait(10)

    def replay():
        attempted.set()
        return invoke("completion-race")

    def forbidden(*args, **kwargs):
        raise AssertionError("A completed upload must not be signed again")

    monkeypatch.setattr(services.knowledge.storage, "create_upload_authorization", forbidden)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            completing = pool.submit(complete)
            try:
                assert entered.wait(5)
                retry = pool.submit(replay)
                assert attempted.wait(5)
                with pytest.raises(FutureTimeout):
                    retry.result(timeout=.15)
            finally:
                release.set()
            completing.result(timeout=5)
            assert retry.result(timeout=5)["upload"] is None
    finally:
        release.set()
        peer.close()


@pytest.mark.parametrize("first", ["write", "revoke"])
def test_account_revocation_and_knowledge_write_are_linearized(runtime, monkeypatch, first):
    _, services, _, _, location = runtime
    author = account(services, "linear.author", roles=["developer"])
    admin = account(services, "linear.admin", super_admin=True)
    context = TenantContext(**{field: getattr(author.actor, field) for field in TenantContext.model_fields})
    peer = create_database(location)
    auth = AuthService(peer)
    entered, release, attempted = Event(), Event(), Event()
    original = services.knowledge._append_event if first == "write" else auth._audit

    def pause(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(10)
        return result

    if first == "write":
        monkeypatch.setattr(services.knowledge, "_append_event", pause)
    else:
        monkeypatch.setattr(auth, "_audit", pause)

    def write():
        return services.knowledge.create_knowledge_base(KnowledgeBaseCreate(name="Linearized write"), context, "linear-write")

    def revoke():
        return auth.update_user(author.user["id"], UserUpdate(version=author.user["version"], roles=["viewer"]), admin.actor)

    winner, loser = (write, revoke) if first == "write" else (revoke, write)

    def second():
        attempted.set()
        return loser()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            winning = pool.submit(winner)
            try:
                assert entered.wait(5)
                losing = pool.submit(second)
                assert attempted.wait(5)
                with pytest.raises(FutureTimeout):
                    losing.result(timeout=.15)
            finally:
                release.set()
            winning.result(timeout=5)
            if first == "write":
                losing.result(timeout=5)
            else:
                with pytest.raises((AuthAuthorizationError, AuthenticationError)):
                    losing.result(timeout=5)
        assert services.db.fetch_one("SELECT COUNT(*) AS n FROM knowledge_bases")["n"] == (1 if first == "write" else 0)
        with pytest.raises((AuthAuthorizationError, AuthenticationError)):
            write()
    finally:
        release.set()
        peer.close()


def test_complete_ingested_upload_retry_reuses_the_original_job_and_index(runtime):
    client, services, *_ = runtime
    headers = {"Idempotency-Key": "whole-upload-retry"}
    base = client.post("/api/v1/knowledge-bases", json={"name": "Full upload retry"}, headers=headers).json()
    path = f"/api/v1/knowledge-bases/{base['id']}/documents:prepare-upload"
    prepared = client.post(path, json=upload_payload().model_dump(), headers=headers).json()
    assert client.put(prepared["upload"]["url"], content=b"fixture", headers={"Content-Type": "text/plain"}).status_code == 200
    completion = f"/api/v1/knowledge-document-versions/{prepared['document_version_id']}:complete"
    original = client.post(completion, json={})
    assert original.status_code == 202
    job_id = original.json()["id"]
    asyncio.run(services.knowledge._process_job(job_id))
    assert client.get(f"/api/v1/knowledge-ingestion-jobs/{job_id}").json()["status"] == "SUCCEEDED"
    before = snapshot(services.db)
    replay = client.post(path, json=upload_payload().model_dump(), headers=headers)
    assert replay.status_code == 201
    assert replay.json()["status"] == "READY" and replay.json()["upload"] is None
    # The console skips PUT for upload:null and safely repeats completion.
    repeated = client.post(completion, json={})
    assert repeated.status_code == 202 and repeated.json()["id"] == job_id
    assert repeated.json()["status"] == "SUCCEEDED"
    assert snapshot(services.db) == before
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM knowledge_ingestion_jobs")["n"] == 1
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM knowledge_base_revisions")["n"] == 1
