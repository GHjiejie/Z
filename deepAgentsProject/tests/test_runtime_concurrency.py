from __future__ import annotations

import asyncio
import os
import multiprocessing
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest
from fastapi.testclient import TestClient

from apps.platform_api.main import create_app
from packages.domain.models import AgentCreate, AgentDraftSpec, AgentDraftUpdate, DeploymentCreate, RunCreate, TenantContext, ThreadCreate
from packages.application.services import ConflictError
from packages.evaluations.models import EvaluationCase, EvaluationPolicyUpdate, EvaluationRequest, EvaluationSuiteCreate
from packages.persistence.fencing import LeaseLostError, execution_scope
from packages.runtime.model_gateway import DeterministicModelGateway
from packages.runtime.budget import RunBudget, RunBudgetExceeded
from packages.runtime.orchestrator import RunOrchestrator
from packages.runtime.run_lease import RunLeaseManager
from packages.runtime.task_queue import InMemoryTaskQueue, PostgresTaskQueue, QueueDelivery
from packages.sandbox.fake_provider import FakeSandboxProvider


@pytest.fixture(params=["sqlite", "postgresql"])
def runtime(request, tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPAGENT_PROCESS_ROLE", "api")
    schema = None
    admin = None
    database_url = str(tmp_path / "concurrency.db")
    if request.param == "postgresql":
        url = os.getenv("DEEPAGENT_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("DEEPAGENT_TEST_POSTGRES_URL is required")
        import psycopg
        from psycopg import sql

        admin = psycopg.connect(url, autocommit=True)
        schema = "concurrency_" + secrets.token_hex(10)
        admin.execute("CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA public")
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        query["options"] = f"-csearch_path={schema},public"
        database_url = urlunsplit(parts._replace(query=urlencode(query)))
    try:
        app = create_app(
            database_url, seed=True, load_env=False,
            model_gateway=DeterministicModelGateway(),
            sandbox_providers=[FakeSandboxProvider()],
        )
        with TestClient(app) as client:
            services = app.state.services
            assert services.db.dialect == request.param
            deployment = next(
                item for item in client.get("/api/v1/agent-deployments").json()["items"]
                if not item["coding_enabled"]
            )
            context = TenantContext(
                tenant_id=deployment["tenant_id"], project_id=deployment["project_id"],
            )
            yield client, services, context, deployment["id"], database_url
    finally:
        if admin is not None:
            from psycopg import sql

            # This schema belongs exclusively to this test invocation.
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            admin.close()


def new_thread(runtime):
    _, services, context, deployment_id, _ = runtime
    return services.runs.create_thread(
        ThreadCreate(agent_deployment_id=deployment_id, title="concurrency probe"), context,
    )


def new_run(runtime):
    _, services, context, _, _ = runtime
    thread = new_thread(runtime)
    return asyncio.run(services.runs.create_run(
        thread["id"], RunCreate(input="Give a concise status summary"), context,
    ))


def race(function, count=8):
    barrier = Barrier(count)

    def invoke(index):
        barrier.wait(timeout=10)
        return function(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(invoke, range(count)))


def test_agent_draft_compare_and_swap_is_atomic(runtime):
    from packages.application.services import AgentService
    from packages.persistence import create_database

    client, services, context, _, location = runtime
    agent = services.agents.create_agent(AgentCreate(name="Concurrent draft"), context)
    original = services.agents.get_agent
    databases = [create_database(location) for _ in range(2)]
    peers = [AgentService(db, services.agents.compiler) for db in databases]
    # Two independent clients load version 1 before either starts its write.
    # A barrier inside a write transaction would deadlock SQLite's write lock.
    versions = [peer.get_agent(agent['id'], context)['version'] for peer in peers]
    assert versions == [1, 1]

    def write(index):
        try:
            return peers[index].update_draft(agent["id"], AgentDraftUpdate(
                version=versions[index], draft=AgentDraftSpec(system_prompt=f"writer-{index}"),
            ), context)
        except ConflictError:
            return None

    try:
        results = race(write, count=2)
    finally:
        for db in databases:
            db.close()
    assert sum(result is not None for result in results) == 1
    winner = next(result for result in results if result)
    stored = original(agent["id"], context)
    assert stored["version"] == winner["version"] == 2
    assert stored["draft"] == winner["draft"]
    response = client.patch(f"/api/v1/agents/{agent['id']}/draft", json={"draft": stored["draft"]})
    assert response.status_code == 422
    stale = client.patch(f"/api/v1/agents/{agent['id']}/draft", json={"draft": stored["draft"], "version": 1})
    assert stale.status_code == 409


def test_publish_rejects_a_draft_changed_during_compilation(runtime, monkeypatch):
    _, services, context, _, _ = runtime
    agent = services.agents.create_agent(AgentCreate(name="Publication race"), context)
    compile_original = services.compiler.compile

    def compile_with_edit(*args, **kwargs):
        plan = compile_original(*args, **kwargs)
        services.agents.update_draft(agent["id"], AgentDraftUpdate(
            version=1, draft=AgentDraftSpec(system_prompt="changed during compile"),
        ), context)
        return plan

    monkeypatch.setattr(services.compiler, "compile", compile_with_edit)
    with pytest.raises(ConflictError, match="changed during compilation"):
        services.agents.publish(agent["id"], context)
    current = services.agents.get_agent(agent["id"], context)
    assert current["status"] == "DRAFT"
    assert not current["revisions"]


def test_concurrent_publications_and_deployments_are_serialized(runtime):
    _, services, context, _, _ = runtime
    agent = services.agents.create_agent(AgentCreate(name="Parallel publication"), context)
    revisions = race(lambda _: services.agents.publish(agent["id"], context)["revision"], count=4)
    assert sorted(revision["revision_number"] for revision in revisions) == [1, 2, 3, 4]
    payload = DeploymentCreate(agent_revision_id=revisions[0]["id"], environment="development")
    deployments = race(lambda _: services.agents.deploy(payload, context))
    assert len({deployment["id"] for deployment in deployments}) == 1


def test_evaluation_policy_compare_and_swap_and_result_idempotency(runtime):
    _, services, context, deployment_id, _ = runtime
    cases = [
        EvaluationCase(id="answer", category="functional", input="Give a concise status summary", output_contains=["Analysis complete"]),
        EvaluationCase(id="safety", category="safety", input="Deploy safely"),
        EvaluationCase(id="recovery", category="recovery", input="Deploy after recovery"),
        EvaluationCase(id="cost", category="cost", input="Check cost", max_cost=1),
    ]
    suite = services.evaluations.create_suite(EvaluationSuiteCreate(name="Concurrent gate policy", cases=cases), context)

    def change_policy(_):
        try:
            return services.evaluations.update_policy(EvaluationPolicyUpdate(
                suite_id=suite["id"], version=0, reason="Configure one release acceptance policy",
            ), context)
        except ConflictError:
            return None

    policies = race(change_policy)
    assert sum(policy is not None for policy in policies) == 1
    smoke = services.evaluations.create_suite(EvaluationSuiteCreate(name="Recorded Run smoke", cases=cases[:1]), context)
    run = new_run(runtime)
    fence = services.orchestrator.run_leases.claim(run["id"])
    assert fence

    async def execute_sample():
        with execution_scope(fence):
            await services.orchestrator.executors.reference.execute(run["id"])

    asyncio.run(execute_sample())
    services.orchestrator.run_leases.release(fence)
    deployment = services.db.fetch_one("SELECT * FROM agent_deployments WHERE id=?", (deployment_id,))
    payload = EvaluationRequest(suite_id=smoke["id"], case_runs={"answer": run["id"]})
    results = race(lambda _: services.evaluations.evaluate(deployment["agent_revision_id"], payload, context, "same-evaluation"))
    assert len({result["id"] for result in results}) == 1
    assert results[0]["status"] == "PASSED"
    assert results[0]["production_eligible"] == 0
    assert services.evaluations.get_result(results[0]["id"], context)["result_hash"] == results[0]["result_hash"]


def test_evaluation_fractional_score_retains_its_integrity_in_both_databases(runtime):
    _, services, context, deployment_id, _ = runtime
    cases = [EvaluationCase(id=f"case_{index}", category="functional", input="Give a concise status summary",
                            output_contains=["Analysis complete" if index < 2 else "not-in-the-model-response"])
             for index in range(3)]
    suite = services.evaluations.create_suite(EvaluationSuiteCreate(name="Fractional score integrity", cases=cases), context)
    mapping = {}
    for case in cases:
        run = new_run(runtime)
        fence = services.orchestrator.run_leases.claim(run["id"])

        async def execute_sample():
            with execution_scope(fence):
                await services.orchestrator.executors.reference.execute(run["id"])

        asyncio.run(execute_sample())
        services.orchestrator.run_leases.release(fence)
        mapping[case.id] = run["id"]
    deployment = services.db.fetch_one("SELECT agent_revision_id FROM agent_deployments WHERE id=?", (deployment_id,))
    result = services.evaluations.evaluate(deployment["agent_revision_id"], EvaluationRequest(suite_id=suite["id"], case_runs=mapping), context)
    assert result["score"] == 2 / 3
    assert result["status"] == "FAILED"
    assert services.evaluations.get_result(result["id"], context)["result_hash"] == result["result_hash"]


def test_atomic_claim_recovery_and_stale_write_fencing(runtime):
    _, services, _, _, _ = runtime
    db = services.db
    run = new_run(runtime)
    managers = [RunLeaseManager(db, f"worker_{i}") for i in range(8)]
    claims = race(lambda i: managers[i].claim(run["id"]))
    owners = [item for item in claims if item]
    assert len(owners) == 1
    stale = owners[0]
    with execution_scope(stale):
        db.execute("UPDATE runs SET status='RUNNING' WHERE id=?", (run["id"],))
    db.execute(
        "UPDATE run_attempts SET expires_at=? WHERE id=?",
        ((db.current_time() - timedelta(seconds=1)).isoformat(), stale.attempt_id),
    )
    with pytest.raises(LeaseLostError):
        managers[0].heartbeat(stale)
    recoveries = race(lambda i: managers[i].recover(run["id"]))
    assert sum(item is not None for item in recoveries) == 1
    attempts = db.fetch_all("SELECT * FROM run_attempts WHERE run_id=?", (run["id"],))
    assert len(attempts) == 2
    assert {item["status"] for item in attempts} == {"ORPHANED", "PENDING"}
    with execution_scope(stale), pytest.raises(LeaseLostError):
        db.execute("UPDATE runs SET output='stale result' WHERE id=?", (run["id"],))
    with execution_scope(stale), pytest.raises(LeaseLostError):
        services.events.append(run["id"], "stale.event", {})
    owner = managers[0].claim(run["id"])
    assert owner and owner.attempt_id != stale.attempt_id
    with execution_scope(owner):
        db.execute("UPDATE runs SET output='new owner' WHERE id=?", (run["id"],))
    assert db.fetch_one("SELECT output FROM runs WHERE id=?", (run["id"],))["output"] == "new owner"


def test_model_budget_reservations_are_atomic_across_parallel_calls(runtime):
    _, services, _, _, _ = runtime
    run = new_run(runtime)
    db = services.db
    fence = RunLeaseManager(db, "budget-worker").claim(run["id"])
    plan = db.fetch_one("SELECT * FROM resolved_execution_plans WHERE id=?", (run["resolved_plan_id"],))["plan"]
    plan["limits"]["max_cost"] = 0.004
    meter = RunBudget(db, run["id"], plan, services.model_gateway.identity())

    def reserve(index):
        with execution_scope(fence):
            try:
                return meter.reserve(input_tokens=0, output_tokens=1000)
            except RunBudgetExceeded:
                return None

    reservations = [value for value in race(reserve) if value]
    assert len(reservations) == 1
    assert meter._totals() == (1, 3200)
    with execution_scope(fence):
        meter.settle(reservations[0], input_tokens=10, output_tokens=10)
        meter.settle(reservations[0], input_tokens=10, output_tokens=10)
        assert meter._totals() == (1, 40)
        meter.reserve(input_tokens=0, output_tokens=1000)
    assert meter._totals() == (2, 3240)


@pytest.mark.parametrize("known_usage", [True, False])
def test_model_budget_survives_attempt_recovery_and_fences_late_callbacks(runtime, known_usage):
    _, services, _, _, _ = runtime
    run = new_run(runtime)
    db = services.db
    manager = RunLeaseManager(db, "budget-worker")
    old = manager.claim(run["id"])
    plan = db.fetch_one("SELECT * FROM resolved_execution_plans WHERE id=?", (run["resolved_plan_id"],))["plan"]
    plan["limits"]["max_model_calls"] = 1
    meter = RunBudget(db, run["id"], plan, services.model_gateway.identity())
    with execution_scope(old):
        db.execute("UPDATE runs SET status='RUNNING' WHERE id=?", (run["id"],))
        reservation = meter.reserve(input_tokens=0, output_tokens=1000)
        if known_usage:
            meter.settle(reservation, input_tokens=10, output_tokens=10)
        else:
            meter.uncertain(reservation)
    db.execute("UPDATE run_attempts SET expires_at=? WHERE id=?",
        ((db.current_time() - timedelta(seconds=1)).isoformat(), old.attempt_id))
    manager.recover(run["id"])
    new = RunLeaseManager(db, "replacement-budget-worker").claim(run["id"])
    assert new is not None
    with execution_scope(new), pytest.raises(RunBudgetExceeded, match="call budget"):
        meter.reserve(input_tokens=0, output_tokens=1)
    with execution_scope(old), pytest.raises(LeaseLostError):
        meter.settle(reservation, input_tokens=0, output_tokens=0)
    assert meter._totals() == (1, 40 if known_usage else 3200)


def test_provider_overage_is_recorded_even_when_the_run_budget_fails(runtime):
    _, services, _, _, _ = runtime
    run = new_run(runtime)
    db = services.db
    owner = RunLeaseManager(db, "budget-worker").claim(run["id"])
    plan = db.fetch_one("SELECT * FROM resolved_execution_plans WHERE id=?", (run["resolved_plan_id"],))["plan"]
    plan["limits"]["max_cost"] = .004
    meter = RunBudget(db, run["id"], plan, services.model_gateway.identity())
    with execution_scope(owner):
        reservation = meter.reserve(input_tokens=0, output_tokens=1)
        with pytest.raises(RunBudgetExceeded, match="Provider usage"):
            meter.settle(reservation, input_tokens=0, output_tokens=2000)
        assert meter._totals() == (1, 6400)
        assert db.fetch_one("SELECT billing_status FROM usage_ledger WHERE id=?", (reservation,))["billing_status"] == "ACTUAL"


def test_idempotent_creation_and_serial_thread_admission(runtime):
    _, services, context, _, _ = runtime
    thread = new_thread(runtime)

    def create(index, same_key=True):
        from packages.application.services import ConflictError

        try:
            return asyncio.run(services.runs.create_run(
                thread["id"], RunCreate(input="status"), context,
                idempotency_key="same" if same_key else f"key-{index}",
            ))["id"]
        except ConflictError:
            return None

    result = race(create)
    assert len(set(result)) == 1 and None not in result
    assert len(services.db.fetch_all("SELECT id FROM runs WHERE thread_id=?", (thread["id"],))) == 1
    thread = new_thread(runtime)
    result = race(lambda i: create(i, False))
    assert sum(item is not None for item in result) == 1


def test_run_idempotency_binds_content_principal_and_preserves_legacy(runtime):
    client, services, context, deployment_id, _ = runtime
    thread = new_thread(runtime)
    path = f"/api/v1/threads/{thread['id']}/runs"
    headers = {"Idempotency-Key": "body-bound"}
    payload = {"input": "status", "metadata": {"client": {"a": 1, "b": 2}}}
    first = client.post(path, json=payload, headers=headers)
    assert first.status_code == 202, first.text
    reordered = {"metadata": {"client": {"b": 2, "a": 1}}, "input": "status"}
    assert client.post(path, json=reordered, headers=headers).json() == first.json()
    for changed in ({**payload, "input": "different"}, {**payload, "metadata": {}}):
        assert client.post(path, json=changed, headers=headers).status_code == 409
    # Even another writer of a shared conversation must not replay this
    # principal's cached response using a colliding client key.
    services.db.execute("UPDATE threads SET visibility='project' WHERE id=?", (thread['id'],))
    from release_helpers import user_headers
    other = {**headers, **user_headers(services, "other_fixture_writer", "developer")}
    assert client.post(path, json=payload, headers=other).status_code == 409
    saved = services.db.fetch_one("SELECT response_json FROM idempotency_records WHERE key=?", ("body-bound",))
    legacy = services.db.encode(first.json())
    services.db.execute("UPDATE idempotency_records SET response_json=? WHERE key=?", (legacy, "body-bound"))
    assert client.post(path, json=payload, headers=headers).status_code == 409
    assert services.db.fetch_one("SELECT response_json FROM idempotency_records WHERE key=?", ("body-bound",))["response_json"] == legacy
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM runs WHERE thread_id=?", (thread['id'],))["n"] == 1
    assert "request_hash" in saved["response_json"]


def test_run_and_queue_commit_or_rollback_together(runtime, monkeypatch):
    _, services, context, _, _ = runtime
    thread = new_thread(runtime)
    queue = PostgresTaskQueue(services.db, "transaction-probe")
    services.orchestrator.queue = queue
    original = queue.put_transactional

    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected failure after queue insert")

    monkeypatch.setattr(queue, "put_transactional", fail_after_insert)
    with pytest.raises(RuntimeError, match="injected failure"):
        asyncio.run(services.runs.create_run(thread["id"], RunCreate(input="status"), context))
    assert not services.db.fetch_all("SELECT id FROM runs WHERE thread_id=?", (thread["id"],))
    assert not services.db.fetch_all("SELECT id FROM task_queue WHERE queue_name='transaction-probe'")


def test_routed_request_is_concurrently_idempotent(runtime):
    client, services, context, _, _ = runtime
    from packages.routing.models import RoutedRunCreate

    response = client.post("/api/v1/intent-routing:resolve", json={"input": "你好"})
    assert response.status_code == 201
    decision = response.json()
    request = RoutedRunCreate(decision_id=decision["id"], input="你好", confirmed=True)
    results = race(lambda _: asyncio.run(services.routing.create_routed_run(request, context)))
    assert len({item["run"]["id"] for item in results}) == 1
    assert len({item["thread"]["id"] for item in results}) == 1
    assert len(services.db.fetch_all(
        "SELECT id FROM threads WHERE routing_decision_id=?", (decision["id"],),
    )) == 1


def test_queue_competing_claims_and_stale_generation_cannot_ack(runtime):
    _, services, _, _, _ = runtime
    if services.db.dialect != "postgresql":
        pytest.skip("SKIP LOCKED requires PostgreSQL")
    db = services.db
    queue = PostgresTaskQueue(db, "queue-probe")
    race(lambda _: queue.put_transactional("payload", dedupe_key="attempt-one"))
    assert queue.qsize() == 1
    deliveries = race(lambda _: queue._claim())
    first = next(item for item in deliveries if item)
    assert sum(item is not None for item in deliveries) == 1
    old = QueueDelivery(first["id"], first["dedupe_key"], first["attempts"])
    db.execute("UPDATE task_queue SET lease_expires_at=? WHERE id=?", (
        (db.current_time() - timedelta(seconds=1)).isoformat(), first["id"],
    ))
    second = queue._claim()
    assert second and second["attempts"] == first["attempts"] + 1
    queue._delivery.set(old)
    with pytest.raises(LeaseLostError):
        asyncio.run(queue.heartbeat())
    queue.task_done()
    assert db.fetch_one("SELECT status FROM task_queue WHERE id=?", (first["id"],))["status"] == "RUNNING"
    queue._delivery.set(QueueDelivery(second["id"], second["dedupe_key"], second["attempts"]))
    queue.task_done(failed=True, error="injected executor failure")
    assert db.fetch_one("SELECT status FROM task_queue WHERE id=?", (first["id"],))["status"] == "FAILED"


def test_worker_shutdown_fences_late_writes_and_recovery_resumes(runtime):
    _, services, _, _, _ = runtime
    run = new_run(runtime)
    db = services.db

    async def scenario():
        entered = asyncio.Event()
        rejected = asyncio.Event()
        finished = asyncio.Event()

        class BlockingExecutor:
            async def execute(self, run_id):
                db.execute("UPDATE runs SET status='RUNNING' WHERE id=?", (run_id,))
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    with pytest.raises(LeaseLostError):
                        db.execute("UPDATE runs SET output='late' WHERE id=?", (run_id,))
                    rejected.set()
                    raise

        class FinishingExecutor:
            async def execute(self, run_id):
                with db.transaction():
                    db.execute("UPDATE runs SET status='SUCCEEDED', output='recovered' WHERE id=?", (run_id,))
                    db.execute(
                        "UPDATE run_attempts SET status='SUCCEEDED' WHERE id=(SELECT current_attempt_id FROM runs WHERE id=?)",
                        (run_id,),
                    )
                finished.set()

        first = RunOrchestrator(db, services.events, None, DeterministicModelGateway(), queue=InMemoryTaskQueue())
        first.executors.reference = BlockingExecutor()
        await first.start()
        try:
            await asyncio.wait_for(entered.wait(), 5)
        finally:
            await first.stop()
        assert rejected.is_set()
        second = RunOrchestrator(db, services.events, None, DeterministicModelGateway(), queue=InMemoryTaskQueue())
        second.executors.reference = FinishingExecutor()
        await second.start()
        try:
            await asyncio.wait_for(finished.wait(), 5)
            assert db.fetch_one("SELECT output FROM runs WHERE id=?", (run["id"],))["output"] == "recovered"
            assert len(db.fetch_all("SELECT id FROM run_attempts WHERE run_id=?", (run["id"],))) == 2
        finally:
            await second.stop()

    asyncio.run(scenario())


def test_checkpoint_and_pending_writes_are_fenced(runtime):
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.graph import StateGraph

    _, services, _, _, _ = runtime
    run = new_run(runtime)
    config = {"configurable": {"thread_id": f"{run['tenant_id']}:{run['project_id']}:{run['thread_id']}"}}
    saver = services.checkpointer
    manager = RunLeaseManager(services.db, "checkpoint-owner")
    owner = manager.claim(run["id"])

    async def scenario():
        builder = StateGraph(int)
        builder.add_node("increment", lambda value: value + 1)
        builder.set_entry_point("increment")
        builder.set_finish_point("increment")
        graph = builder.compile(checkpointer=saver)
        with execution_scope(owner):
            assert await graph.ainvoke(1, config) == 2
        saved = await saver.aget_tuple(config)
        assert saved is not None
        with pytest.raises(LeaseLostError):
            await saver.aput(config, empty_checkpoint(), {}, {})
        services.db.execute("UPDATE run_attempts SET expires_at=? WHERE id=?", (
            (services.db.current_time() - timedelta(seconds=1)).isoformat(), owner.attempt_id,
        ))
        manager.recover(run["id"])
        with execution_scope(owner), pytest.raises(LeaseLostError):
            await saver.aput_writes(saved.config, [("stale", "pollution")], "old-task")
        replacement = manager.claim(run["id"])
        with execution_scope(replacement):
            assert await graph.ainvoke(5, config) == 6
        assert all(write[1] != "stale" for write in (await saver.aget_tuple(saved.config)).pending_writes)

    asyncio.run(scenario())


def prepare_ingestion(runtime, knowledge_base_id=None, text=b"enterprise knowledge evidence"):
    import hashlib
    from packages.knowledge.models import KnowledgeBaseCreate, UploadComplete, UploadPrepare

    _, services, context, _, _ = runtime
    knowledge = services.knowledge
    if knowledge_base_id is None:
        knowledge_base_id = knowledge.create_knowledge_base(
            KnowledgeBaseCreate(name="Concurrency evidence"), context,
        )["id"]
    prepared = knowledge.prepare_upload(
        knowledge_base_id,
        UploadPrepare(
            filename="evidence.txt", content_type="text/plain",
            size_bytes=len(text), sha256=hashlib.sha256(text).hexdigest(),
        ), context,
    )
    knowledge.upload_content(prepared["document_version_id"], text, "text/plain", context)
    job = asyncio.run(knowledge.complete_upload(prepared["document_version_id"], UploadComplete(), context))
    return knowledge_base_id, job


def test_ingestion_lease_loss_cannot_mutate_recovered_job(runtime):
    from packages.persistence.fencing import IngestionWriteFence

    _, services, _, _, _ = runtime
    knowledge, db = services.knowledge, services.db
    _, job = prepare_ingestion(runtime)
    claimed = race(lambda i: knowledge._claim_job(job["id"], f"token_{i}"))
    assert sum(claimed) == 1
    owner = IngestionWriteFence(job["id"], knowledge.worker_id, f"token_{claimed.index(True)}", knowledge.lease_seconds)
    db.execute("UPDATE knowledge_ingestion_jobs SET heartbeat_at=? WHERE id=?", (
        (db.current_time() - timedelta(seconds=knowledge.lease_seconds + 1)).isoformat(), job["id"],
    ))
    asyncio.run(knowledge.reconcile())
    with execution_scope(owner), pytest.raises(LeaseLostError):
        knowledge._set_job_stage(job["id"], "PARSING")
    with execution_scope(owner), pytest.raises(LeaseLostError):
        knowledge._fail_job(job["id"], RuntimeError("stale failure"))
    asyncio.run(knowledge._process_job(job["id"]))
    finished = db.fetch_one("SELECT status, attempts FROM knowledge_ingestion_jobs WHERE id=?", (job["id"],))
    assert finished["status"] == "SUCCEEDED" and finished["attempts"] == 2


def test_concurrent_ingestion_publication_preserves_both_documents(runtime):
    _, services, _, _, _ = runtime
    base_id, first = prepare_ingestion(runtime, text=b"first unique knowledge document")
    _, second = prepare_ingestion(runtime, base_id, text=b"second independent knowledge document")
    jobs = [first, second]
    race(lambda index: asyncio.run(services.knowledge._process_job(jobs[index]["id"])), count=2)
    revisions = services.db.fetch_all(
        "SELECT * FROM knowledge_base_revisions WHERE knowledge_base_id=? ORDER BY revision_number", (base_id,),
    )
    assert [row["revision_number"] for row in revisions] == [1, 2]
    assert [row["status"] for row in revisions] == ["DEPRECATED", "ACTIVE"]
    documents = services.db.fetch_all(
        "SELECT document_version_id FROM knowledge_revision_documents WHERE revision_id=?", (revisions[-1]["id"],),
    )
    assert {row["document_version_id"] for row in documents} == {job["document_version_id"] for job in jobs}


def _process_worker(database_url, run_id, block, pipe):
    """Real child process, intentionally terminated by the test after claiming."""
    from packages.persistence import create_database
    from packages.runtime.event_emitter import EventEmitter

    db = create_database(database_url)

    async def scenario():
        orchestrator = RunOrchestrator(
            db, EventEmitter(db), None, DeterministicModelGateway(),
            queue=PostgresTaskQueue(db, "runtime-runs", poll_seconds=0.05),
        )

        class BlockUntilKilled:
            async def execute(self, target):
                db.execute("UPDATE runs SET status='RUNNING' WHERE id=?", (target,))
                pipe.send("claimed")
                await asyncio.Event().wait()

        if block:
            orchestrator.executors.reference = BlockUntilKilled()
        await orchestrator.start()
        try:
            for _ in range(300):
                row = db.fetch_one("SELECT status FROM runs WHERE id=?", (run_id,))
                if row["status"] in {"SUCCEEDED", "FAILED"}:
                    pipe.send(row["status"])
                    return
                await asyncio.sleep(0.05)
            raise TimeoutError("Worker did not finish")
        finally:
            await orchestrator.stop()

    try:
        asyncio.run(scenario())
    except BaseException as exc:
        pipe.send(f"error: {exc}")
        raise
    finally:
        db.close()
        pipe.close()


def test_postgres_recovers_after_actual_worker_process_is_killed(runtime):
    _, services, _, _, database_url = runtime
    if services.db.dialect != "postgresql":
        pytest.skip("Split-process workers require PostgreSQL")
    run = new_run(runtime)
    processes = multiprocessing.get_context("spawn")
    parent, child = processes.Pipe()
    first = processes.Process(target=_process_worker, args=(database_url, run["id"], True, child))
    first.start()
    second = None
    try:
        assert parent.poll(15), "First worker did not claim"
        assert parent.recv() == "claimed"
        first.terminate()
        first.join(timeout=5)
        assert not first.is_alive()
        services.db.execute("UPDATE run_attempts SET expires_at=? WHERE id=?", (
            (services.db.current_time() - timedelta(seconds=1)).isoformat(), run["current_attempt_id"],
        ))
        second = processes.Process(target=_process_worker, args=(database_url, run["id"], False, child))
        second.start()
        assert parent.poll(15), "Recovery worker did not finish"
        assert parent.recv() == "SUCCEEDED"
        second.join(timeout=5)
        assert second.exitcode == 0
        attempts = services.db.fetch_all(
            "SELECT status FROM run_attempts WHERE run_id=? ORDER BY attempt_number", (run["id"],),
        )
        assert [item["status"] for item in attempts] == ["ORPHANED", "SUCCEEDED"]
    finally:
        for process in (first, second):
            if process and process.is_alive():
                process.terminate()
                process.join(timeout=5)
        parent.close()
        child.close()


def test_postgres_sandbox_authority_has_only_the_minimum_read_only_grant(runtime, tmp_path):
    from packages.sandbox.lease_authority import PostgresLeaseAuthority
    from packages.coding.models import RepositoryCreate, RepositorySnapshotCreate, WorkspaceBinding

    client, services, context, _, url = runtime
    if services.db.dialect != "postgresql":
        pytest.skip("PostgreSQL role/view contract")
    import psycopg
    from psycopg import sql

    role = "sandbox_reader_" + secrets.token_hex(10)
    schema = services.db.fetch_one("SELECT current_schema() AS name")["name"]
    admin = psycopg.connect(url, autocommit=True)
    authority = None
    granted = False
    source = tmp_path / "lease-source"
    source.mkdir()
    (source / "README.md").write_text("lease authorization fixture")
    services.repositories.allowed_local_roots.append(source)
    repository = services.repositories.create_repository(
        RepositoryCreate(name="lease source", provider="local_snapshot", canonical_uri=str(source)), context,
    )
    coding = next(item for item in client.get("/api/v1/agent-deployments").json()["items"] if item["coding_enabled"])
    thread = services.runs.create_thread(ThreadCreate(
        agent_deployment_id=coding["id"], title="sandbox lease",
        workspace=WorkspaceBinding(repository_id=repository["id"], source_mode="working_tree_snapshot"),
    ), context)
    run = asyncio.run(services.runs.create_run(thread["id"], RunCreate(input="Inspect the repository"), context))
    manager = services.orchestrator.run_leases
    owner = manager.claim(run["id"])
    # Allocate the sandbox identity exactly as bind() does before remote provisioning.
    services.db.execute(
        """INSERT INTO sandbox_instances(id, tenant_id, project_id, provider, profile_json, status, created_at, updated_at, expires_at)
           VALUES('authority-sandbox', ?, ?, 'remote', '{}', 'PROVISIONING', ?, ?, ?)""",
        (context.tenant_id, context.project_id, services.db.current_time().isoformat(),
         services.db.current_time().isoformat(), (services.db.current_time() + timedelta(hours=1)).isoformat()),
    )
    services.db.execute("UPDATE coding_workspaces SET sandbox_instance_id='authority-sandbox' WHERE id=?", (run["coding_workspace_id"],))
    try:
        with pytest.raises(ValueError, match="unprivileged"):
            PostgresLeaseAuthority(url, require_tls=False)
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
        granted = True
        admin.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
        admin.execute(sql.SQL("GRANT SELECT ON {}.sandbox_execution_leases TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
        admin.execute(sql.SQL("GRANT SELECT ON {}.sandbox_cancellation_leases TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
        parts = urlsplit(url)
        reader_url = urlunsplit(parts._replace(netloc=f"{role}@{parts.hostname}:{parts.port}"))
        authority = PostgresLeaseAuthority(reader_url, require_tls=False)
        row = authority.lookup("authority-sandbox")
        assert row["lease_live"] is True and row["attempt_id"] == owner.attempt_id
        assert row["lease_token"] == owner.lease_token
        with authority.pool.connection() as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute("SELECT * FROM users")
            assert connection.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                connection.execute("DELETE FROM sandbox_execution_leases")
        import httpx
        from apps.sandbox_service.main import create_sandbox_service
        from deepagents.backends.protocol import ExecuteResponse
        from packages.coding.models import SandboxProfileSpec
        from packages.sandbox.ports import SandboxProvisionRequest
        from packages.sandbox.remote_provider import RemoteSandboxProvider

        fake = FakeSandboxProvider(command_handler=lambda _: ExecuteResponse(output="accepted", exit_code=0))
        service = create_sandbox_service(
            provider=fake, state_path=str(tmp_path / "host-state.db"),
            service_token="service-token", image="coding:test", lease_authority=authority,
        )
        with TestClient(service) as host:
            def dispatch(request):
                response = host.request(
                    request.method, request.url.path, content=request.content, headers=dict(request.headers),
                )
                return httpx.Response(response.status_code, content=response.content, headers={"content-type": "application/json"})

            remote = RemoteSandboxProvider(
                base_url="https://sandbox.internal", service_token="service-token",
                transport=httpx.MockTransport(dispatch),
            )
            workspace = services.db.fetch_one("SELECT * FROM coding_workspaces WHERE id=?", (run["coding_workspace_id"],))
            source_snapshot = services.db.fetch_one("SELECT * FROM repository_snapshots WHERE id=?", (workspace["repository_snapshot_id"],))
            archive = services.repositories.read_archive(source_snapshot)
            profile = SandboxProfileSpec(
                provider="remote", image="coding:test", image_digest="sha256:" + "a" * 64,
                memory_mb=512, disk_mb=1024,
            ).model_dump()
            with execution_scope(owner):
                provisioned = asyncio.run(remote.provision(SandboxProvisionRequest(
                    sandbox_instance_id="authority-sandbox", tenant_id=context.tenant_id, project_id=context.project_id,
                    thread_id=thread["id"], workspace_id=workspace["id"], profile=profile,
                    source_archive=archive, source_sha256=source_snapshot["archive_sha256"],
                    base_commit_sha=source_snapshot["resolved_commit_sha"],
                )))
                assert provisioned.backend.execute("inspect").output == "accepted"
            manager.abandon(owner)
            assert authority.lookup("authority-sandbox")["lease_live"] is False
            with execution_scope(owner), pytest.raises(LeaseLostError):
                provisioned.backend.execute("stale")
            recovered, _ = manager.recover(run["id"])
            replacement = manager.claim(run["id"])
            assert replacement.attempt_id == recovered["current_attempt_id"]
            assert authority.lookup("authority-sandbox")["attempt_id"] == replacement.attempt_id
            with execution_scope(replacement):
                assert provisioned.backend.execute("new owner").exit_code == 0
            asyncio.run(remote.interrupt_attempt(provisioned.external_id, owner.attempt_id))
            with execution_scope(replacement):
                assert provisioned.backend.execute("still active").exit_code == 0
            from packages.runtime.cancellation import CancellationFinalizer
            from test_coding_recovery import executor
            services.db.execute("UPDATE runs SET status='CANCELLING' WHERE id=?", (run['id'],))
            cancellation = CancellationFinalizer(executor(services)).claim(run['id'])
            row = authority.lookup_cancellation('authority-sandbox')
            assert row['lease_live'] and row['lease_token'] == cancellation.lease_token
            assert row['attempt_id'] == replacement.attempt_id
            fake._backends[provisioned.external_id].command_handler = lambda _: ExecuteResponse(output='',exit_code=0)
            with execution_scope(cancellation):
                captured = asyncio.run(remote.capture_cancellation(provisioned.external_id, profile))
                assert captured.changes['patch'] == ''
                with pytest.raises(LeaseLostError):
                    provisioned.backend.execute('not an execution credential')
            with execution_scope(replacement), pytest.raises(LeaseLostError):
                provisioned.backend.execute('cancelled execution')
            asyncio.run(remote.destroy(provisioned.external_id))
        public_run = services.runs.get_run(run["id"], context)
        assert all("lease_token" not in attempt for attempt in public_run["attempts"])
    finally:
        if authority:
            authority.close()
        if granted:
            admin.execute(sql.SQL("REVOKE SELECT ON {}.sandbox_execution_leases FROM {}").format(sql.Identifier(schema), sql.Identifier(role)))
            admin.execute(sql.SQL("REVOKE SELECT ON {}.sandbox_cancellation_leases FROM {}").format(sql.Identifier(schema), sql.Identifier(role)))
            admin.execute(sql.SQL("REVOKE USAGE ON SCHEMA {} FROM {}").format(sql.Identifier(schema), sql.Identifier(role)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        admin.close()
