import asyncio
import hashlib
from dataclasses import replace

import pytest

from packages.domain.models import RunCreate, ThreadCreate
from packages.runtime.admission import AdmissionSettings, CapacityExceeded
from test_runtime_concurrency import runtime, race, new_run, new_thread


def configure(services, **values):
    settings = replace(services.runs.admission.settings, **values)
    for service in (services.runs, services.approvals, services.knowledge):
        service.admission.settings = settings


@pytest.mark.parametrize("limit", ["runtime_tenant_active", "runtime_project_active", "runtime_user_active",
                                  "runtime_tenant_queued", "runtime_project_queued", "runtime_user_queued"])
def test_competing_threads_cannot_overbook_admission(runtime, limit):
    _, services, context, deployment_id, _ = runtime
    configure(services, **{limit: 1})
    threads = [new_thread(runtime) for _ in range(6)]

    def submit(index):
        try:
            return asyncio.run(services.runs.create_run(threads[index]["id"], RunCreate(input="bounded work"), context))
        except CapacityExceeded:
            return None

    results = race(submit, count=6)
    assert sum(result is not None for result in results) == 1
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM runs")["n"] == 1
    assert services.orchestrator.queue.qsize() == 1


def test_capacity_rejection_is_atomic_and_idempotent_replay_does_not_consume_slots(runtime):
    client, services, *_ = runtime
    configure(services, runtime_user_active=1)
    first_thread, second_thread = new_thread(runtime), new_thread(runtime)
    first_path = f"/api/v1/threads/{first_thread['id']}/runs"
    first = client.post(first_path, json={"input": "first"}, headers={"Idempotency-Key": "first"})
    assert first.status_code == 202
    assert client.post(first_path, json={"input": "first"}, headers={"Idempotency-Key": "first"}).json() == first.json()
    response = client.post(f"/api/v1/threads/{second_thread['id']}/runs",
                           json={"input": "second"}, headers={"Idempotency-Key": "second"})
    assert response.status_code == 429 and response.headers["retry-after"] == "5"
    assert response.json()["error"]["code"] == "CAPACITY_EXCEEDED"
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM runs WHERE thread_id=?", (second_thread["id"],))["n"] == 0
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM idempotency_records WHERE key='second'")["n"] == 0
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM run_attempts")["n"] == 1
    services.db.execute("UPDATE runs SET status='SUCCEEDED' WHERE id=?", (first.json()["id"],))
    assert client.post(f"/api/v1/threads/{second_thread['id']}/runs", json={"input": "second"}).status_code == 202


def test_retry_and_continuation_cannot_bypass_capacity(runtime):
    client, services, context, *_ = runtime
    run = new_run(runtime)
    configure(services, runtime_user_queued=0)
    services.db.execute("UPDATE runs SET status='WAITING_FOR_INPUT' WHERE id=?", (run["id"],))
    with pytest.raises(CapacityExceeded):
        asyncio.run(services.runs.provide_input(run["id"], RunCreate(input="continue"), context))
    assert services.runs.get_run(run["id"], context)["status"] == "WAITING_FOR_INPUT"
    services.db.execute("UPDATE runs SET status='FAILED' WHERE id=?", (run["id"],))
    with pytest.raises(CapacityExceeded):
        asyncio.run(services.runs.retry(run["id"], context))
    assert len(services.runs.get_run(run["id"], context)["attempts"]) == 1
    configure(services, runtime_user_queued=1)
    assert asyncio.run(services.runs.retry(run["id"], context))["status"] == "QUEUED"


def test_user_limit_isolated_but_project_limit_is_shared(runtime):
    _, services, context, deployment_id, _ = runtime
    configure(services, runtime_user_active=1, runtime_project_active=2)
    new_run(runtime)
    other = context.model_copy(update={"user_id": "other-admission-principal"})
    thread = services.runs.create_thread(ThreadCreate(agent_deployment_id=deployment_id), other)
    assert asyncio.run(services.runs.create_run(thread["id"], RunCreate(input="other user's slot"), other))
    third = context.model_copy(update={"user_id": "third-admission-principal"})
    thread = services.runs.create_thread(ThreadCreate(agent_deployment_id=deployment_id), third)
    with pytest.raises(CapacityExceeded, match="project"):
        asyncio.run(services.runs.create_run(thread["id"], RunCreate(input="full project"), third))
    with pytest.raises(RuntimeError, match="transaction"):
        services.runs.admission.run(third)


def test_admission_indexes_installed_without_modifying_history(runtime):
    _, services, *_ = runtime
    run = new_run(runtime)
    db = services.db
    names = {"idx_runs_outstanding_admission", "idx_runs_pending_health",
             "idx_ingestion_outstanding_admission", "idx_ingestion_pending_health"}
    if db.dialect == "postgresql":
        indexes = db.fetch_all("SELECT indexname AS name FROM pg_indexes WHERE schemaname=current_schema()")
    else:
        indexes = db.fetch_all("SELECT name FROM sqlite_master WHERE type='index'")
    assert names.issubset({row["name"] for row in indexes})
    db.initialize()
    assert db.schema_versions()[-1] == 20
    assert db.fetch_one("SELECT input FROM runs WHERE id=?", (run["id"],))["input"] == run["input"]


def test_approval_refusal_does_not_record_a_decision_or_new_attempt(runtime):
    client, services, context, *_ = runtime
    run = new_run(runtime)
    db = services.db
    db.execute("UPDATE runs SET status='WAITING_FOR_APPROVAL' WHERE id=?", (run["id"],))
    from packages.domain.models import DecisionCreate, utc_now
    now = utc_now()
    action = {"action_id": "write", "tool_name": "artifact_write", "arguments": {}, "allowed_decisions": ["approve", "reject"]}
    db.execute("""INSERT INTO interrupts
        (id,tenant_id,project_id,run_id,checkpoint_id,policy_reason,status,actions_json,version,created_at,updated_at)
        VALUES(?,?,?,?,'capacity-checkpoint','Capacity fixture','PENDING',?,1,?,?)""", ("capacity-interrupt", context.tenant_id, context.project_id,
        run["id"], db.encode([action]), now, now))
    configure(services, runtime_user_queued=0)
    payload = DecisionCreate(decisions=[{"action_id": "write", "type": "approve"}])
    with pytest.raises(CapacityExceeded):
        asyncio.run(services.approvals.decide("capacity-interrupt", payload, context, "capacity-approval", 1))
    assert services.approvals.get_interrupt("capacity-interrupt", context)["status"] == "PENDING"
    assert db.fetch_one("SELECT COUNT(*) AS n FROM idempotency_records WHERE key='capacity-approval'")["n"] == 0
    assert len(services.runs.get_run(run["id"], context)["attempts"]) == 1
    # Rejecting work must remain possible even while capacity is exhausted.
    rejected = DecisionCreate(decisions=[{"action_id": "write", "type": "reject"}])
    result = asyncio.run(services.approvals.decide("capacity-interrupt", rejected, context, "capacity-reject", 1))
    assert result["status"] == "RESOLVED"


def test_ingestion_admission_and_retry_leave_versions_consistent(runtime):
    client, services, *_ = runtime
    configure(services, knowledge_user_active=1)
    kb = client.post("/api/v1/knowledge-bases", json={"name": "Capacity fixture"}).json()
    versions = []
    for index in range(2):
        content = f"capacity fixture {index}".encode()
        prepared = client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents:prepare-upload", json={
            "filename": f"fixture{index}.txt", "content_type": "text/plain", "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest()}).json()
        assert client.put(prepared["upload"]["url"], content=content, headers={"Content-Type": "text/plain"}).status_code == 200
        versions.append(prepared["document_version_id"])
    path = lambda version: f"/api/v1/knowledge-document-versions/{version}:complete"
    first = client.post(path(versions[0]), json={})
    assert first.status_code == 202
    assert client.post(path(versions[0]), json={}).json() == first.json()
    assert client.post(path(versions[1]), json={}).status_code == 429
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM knowledge_ingestion_jobs")["n"] == 1
    services.db.execute("UPDATE knowledge_ingestion_jobs SET status='FAILED' WHERE id=?", (first.json()["id"],))
    second = client.post(path(versions[1]), json={})
    assert second.status_code == 202
    assert client.post(f"/api/v1/knowledge-ingestion-jobs/{first.json()['id']}:retry").status_code == 429
    assert services.db.fetch_one("SELECT status FROM knowledge_ingestion_jobs WHERE id=?", (first.json()["id"],))["status"] == "FAILED"


@pytest.mark.parametrize("value", [-1, 1.5, True, 1_000_001])
def test_admission_settings_reject_invalid_limits(value):
    with pytest.raises(ValueError):
        AdmissionSettings(runtime_tenant_active=value)
