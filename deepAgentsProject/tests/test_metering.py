from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta

import httpx
import pytest

from packages.billing.calls import embed
from packages.billing.errors import BillingConfigurationError, BudgetExceeded, QuotaExceeded
from packages.billing.meter import Meter
from packages.knowledge.embedding import HashEmbeddingProvider, OpenAICompatibleEmbeddingProvider
from packages.persistence.fencing import execution_scope
from packages.runtime.budget import RunBudget
from packages.runtime.model_gateway import ModelGatewayError
from tests.test_runtime_concurrency import runtime, race


ADMIN = {"X-Roles":"tenant_admin","X-Tenant-ID":"tenant_demo","X-Project-ID":"project_atlas",
         "X-Environment-ID":"env_development","X-User-ID":"billing_admin_fixture"}
AMBIGUOUS = "Perhaps this is worth considering from another perspective."


def quota(client, *, version=0, **limits):
    response = client.put("/api/v1/billing/quotas",headers=ADMIN,json={
        "scope_type":"tenant","subject_id":"tenant_demo","period":"month","version":version,
        "reason":"Configure the isolated test allowance",**limits})
    assert response.status_code == 200, response.text
    return response.json()


def price(client, identity, *, version=0, **extra):
    response = client.put("/api/v1/billing/prices",json={
        "identity":Meter.identity(identity),"input_per_million":"1","output_per_million":"2",
        "version":version,"reason":"Approved synthetic provider price",**extra})
    assert response.status_code == 200, response.text
    return response.json()


def test_intent_quota_blocks_before_provider_and_records_usage(runtime, monkeypatch):
    client, services, _, _, _ = runtime
    gateway = services.model_gateway
    calls = []
    original = gateway.complete

    async def counted(messages, on_event=None):
        calls.append(messages)
        return await original(messages,on_event)

    monkeypatch.setattr(gateway,"complete",counted)
    quota(client,max_calls=0)
    denied = client.post("/api/v1/intent-routing:resolve",json={"input":AMBIGUOUS})
    assert denied.status_code == 429
    assert not calls
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM metered_calls")["n"] == 0
    quota(client,version=1,max_calls=1)
    assert client.post("/api/v1/intent-routing:resolve",json={"input":AMBIGUOUS}).status_code == 201
    assert client.post("/api/v1/intent-routing:resolve",json={"input":AMBIGUOUS}).status_code == 429
    assert len(calls) == 1
    row = services.db.fetch_one("SELECT * FROM metered_calls")
    assert row["purpose"] == "intent_classification" and row["billing_status"] == "ACTUAL"
    assert row["input_tokens"] > 0
    assert client.get("/api/v1/billing/quotas",headers=ADMIN).json()["items"][0]["usage"]["calls"] == 1


def test_paid_intent_requires_price_and_uncertain_calls_remain_charged(runtime, monkeypatch):
    client, services, context, _, _ = runtime
    gateway = services.model_gateway
    identity = {"provider":"synthetic-paid","route":"https://fixture.test/v1","model":"fixture-model"}
    monkeypatch.setattr(gateway,"identity",lambda:identity)
    attempts = []

    async def fail(messages, on_event=None):
        attempts.append(1)
        raise ModelGatewayError("Synthetic dispatch interruption")

    monkeypatch.setattr(gateway,"complete",fail)
    assert client.post("/api/v1/intent-routing:resolve",json={"input":AMBIGUOUS}).status_code == 503
    assert not attempts
    price(client,identity)
    quota(client,max_calls=1)
    assert client.post("/api/v1/intent-routing:resolve",json={"input":AMBIGUOUS}).status_code == 201
    row = services.db.fetch_one("SELECT * FROM metered_calls")
    assert row["billing_status"] == "UNCERTAIN" and row["charged_micro_usd"] > 0
    assert client.post("/api/v1/intent-routing:resolve",json={"input":AMBIGUOUS}).status_code == 429
    assert len(attempts) == 1


class CountingEmbedding(HashEmbeddingProvider):
    calls = 0

    def embed_with_usage(self,texts):
        self.calls += 1
        return super().embed_with_usage(texts)


def prepare_document(client):
    kb = client.post("/api/v1/knowledge-bases",json={"name":"Metered fixture"}).json()
    content = b"The deployment approval policy requires a reviewed release checklist."
    prepared = client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents:prepare-upload",json={
        "filename":"fixture.md","content_type":"text/markdown","size_bytes":len(content),
        "sha256":hashlib.sha256(content).hexdigest()}).json()
    assert client.put(prepared["upload"]["url"],content=content,headers={"Content-Type":"text/markdown"}).status_code == 200
    queued = client.post(f"/api/v1/knowledge-document-versions/{prepared['document_version_id']}:complete",json={})
    assert queued.status_code == 202, queued.text
    return kb, queued.json()


def test_ingestion_and_search_share_quota_and_preserve_initiator(runtime):
    client, services, context, _, _ = runtime
    embedding = CountingEmbedding()
    services.knowledge.embedding = embedding
    quota(client,max_calls=0)
    kb, job = prepare_document(client)
    internal_job = services.db.fetch_one("SELECT * FROM knowledge_ingestion_jobs WHERE id=?", (job["id"],))
    assert internal_job["requested_by"] == context.user_id
    assert internal_job["requested_environment_id"] == context.environment_id
    assert "requested_by" not in job and "lease_token" not in job
    asyncio.run(services.knowledge._process_job(job["id"]))
    assert client.get(f"/api/v1/knowledge-ingestion-jobs/{job['id']}").json()["status"] == "FAILED"
    assert embedding.calls == 0
    quota(client,version=1,max_calls=1)
    assert client.post(f"/api/v1/knowledge-ingestion-jobs/{job['id']}:retry").status_code == 202
    asyncio.run(services.knowledge._process_job(job["id"]))
    assert client.get(f"/api/v1/knowledge-ingestion-jobs/{job['id']}").json()["status"] == "SUCCEEDED"
    assert embedding.calls == 1
    search = {"knowledge_base_id":kb["id"],"query":"deployment approval policy"}
    assert client.post("/api/v1/knowledge:search",json=search).status_code == 429
    quota(client,version=2,max_calls=2)
    assert client.post("/api/v1/knowledge:search",json=search).status_code == 200
    rows = services.db.fetch_all("SELECT * FROM metered_calls ORDER BY admitted_at")
    assert [row["purpose"] for row in rows] == ["document_embedding","query_embedding"]
    assert {row["user_id"] for row in rows} == {context.user_id}
    assert {row["billing_status"] for row in rows} == {"ACTUAL"}


@pytest.mark.parametrize("usage",[{"prompt_tokens":7,"total_tokens":7},None,{"prompt_tokens":-1}])
def test_embedding_usage_is_settled_or_conservatively_retained(runtime, usage):
    client, services, context, _, _ = runtime
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200,headers={"x-request-id":"fixture-receipt"},json={
            "data":[{"index":0,"embedding":[.2,.3]}],"model":"fixture", "usage":usage})

    provider = OpenAICompatibleEmbeddingProvider("https://embedding.test/v1","synthetic-key","fixture",2,
        transport=httpx.MockTransport(handler))
    price(client,provider.identity())
    assert embed(services.db,provider,["synthetic document"],context,purpose="query_embedding",resource_id="fixture") == [[.2,.3]]
    row = services.db.fetch_one("SELECT * FROM metered_calls")
    if usage and usage["prompt_tokens"] == 7:
        assert row["billing_status"] == "ACTUAL"
        assert row["input_tokens"] == row["charged_micro_usd"] == 7
        assert row["active_until"] is None
    else:
        assert row["billing_status"] == "UNCERTAIN"
        assert row["charged_micro_usd"] == row["reserved_micro_usd"] > 7
    assert len(requests) == 1 and row["provider_receipt"] == "fixture-receipt"


def test_tenant_concurrency_is_atomic_across_requests(runtime):
    client, services, context, _, _ = runtime
    quota(client,max_concurrent_calls=1)
    meter = Meter(services.db)
    identity = services.model_gateway.identity()

    def reserve(index):
        try:
            return meter.reserve(context,identity,meter.pricing(context,identity),purpose="intent_classification",
                resource_id=str(index),input_tokens=100,output_tokens=10)
        except QuotaExceeded:
            return None

    admitted = [ticket for ticket in race(reserve) if ticket]
    assert len(admitted) == 1
    meter.settle(admitted[0],input_tokens=1,output_tokens=1)
    assert reserve(99) is not None


def test_price_change_between_quote_and_admission_requires_retry(runtime):
    client,services,context,_,_=runtime
    meter=Meter(services.db)
    identity={"provider":"synthetic-paid","route":"https://fixture.test/v1","model":"fixture"}
    price(client,identity)
    quoted=meter.pricing(context,identity)
    price(client,identity,version=1,input_per_million="9")
    with pytest.raises(BillingConfigurationError,match="changed"):
        meter.reserve(context,identity,quoted,purpose="intent_classification",resource_id="fixture",input_tokens=2,output_tokens=2)
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM metered_calls")["n"]==0


def test_governance_reconciliation_scope_and_cursor(runtime):
    client, services, context, _, _ = runtime
    meter = Meter(services.db)
    identity = services.model_gateway.identity()
    price(client,identity)
    conflict = client.put("/api/v1/billing/prices",json={
        "identity":Meter.identity(identity),"input_per_million":2,"output_per_million":3,"version":0,"reason":"Stale administrative update"})
    assert conflict.status_code == 409
    assert client.get("/api/v1/billing/calls",headers={**ADMIN,"X-Roles":"member"}).status_code == 403
    denied = client.put("/api/v1/billing/quotas",json={"version":0,"reason":"Not a tenant administrator",
        "scope_type":"tenant","subject_id":context.tenant_id,"max_calls":1})
    assert denied.status_code == 403
    ticket = meter.reserve(context,identity,meter.pricing(context,identity),purpose="intent_classification",
        resource_id="fixture",input_tokens=100,output_tokens=10)
    payload = {"version":1,"reason":"Provider receipt confirms final usage","input_tokens":3,"output_tokens":2,
        "actual_cost_micro_usd":7,"provider_receipt":"receipt-fixture-123"}
    endpoint = f"/api/v1/billing/calls/{ticket.call_id}:reconcile"
    assert client.post(endpoint,json=payload).status_code == 409
    services.db.execute("UPDATE metered_calls SET active_until=? WHERE id=?",
        ((services.db.current_time()-timedelta(seconds=1)).isoformat(),ticket.call_id))
    assert client.post(endpoint,json=payload,headers={**ADMIN,"X-Tenant-ID":"other-tenant"}).status_code == 404
    settled = client.post(endpoint,json=payload)
    assert settled.status_code == 200, settled.text
    assert settled.json()["charged_micro_usd"] == 7
    assert "owner_token_hash" not in settled.json()
    assert client.post(endpoint,json=payload).status_code == 409
    for index in range(3):
        meter.reserve(context,identity,meter.pricing(context,identity),purpose="intent_classification",
            resource_id=str(index),input_tokens=100,output_tokens=10)
    page = client.get("/api/v1/billing/calls?limit=2").json()
    assert page["has_more"] and len(page["items"]) == 2
    older = client.get("/api/v1/billing/calls",params={"limit":2,"cursor":page["next_cursor"]}).json()
    assert len(older["items"]) == 2 and not older["has_more"]
    assert {row["id"] for row in page["items"]}.isdisjoint({row["id"] for row in older["items"]})
    assert client.get("/api/v1/billing/calls",params={"cursor":page["next_cursor"],"status":"ACTUAL"}).status_code == 400
    audits = services.db.fetch_all("SELECT * FROM governance_audit_events WHERE action='billing.call.reconciled'")
    assert len(audits) == 1 and audits[0]["details"]["after"]["charged_micro_usd"] == 7


def test_embedding_inside_run_consumes_same_call_budget(runtime):
    client, services, context, _, _ = runtime
    agent = client.post("/api/v1/agents",json={"name":"One paid call only","draft":{
        "limits":{"max_model_calls":1},"capabilities":{"tools":[],"subagents":[]}}}).json()
    revision = client.post(f"/api/v1/agents/{agent['id']}/revisions:publish").json()["revision"]
    deployment = client.post("/api/v1/agent-deployments",json={"agent_revision_id":revision["id"]}).json()
    thread = client.post("/api/v1/threads",json={"agent_deployment_id":deployment["id"]}).json()
    run = client.post(f"/api/v1/threads/{thread['id']}/runs",json={"input":"Synthetic run"}).json()
    fence = services.orchestrator.run_leases.claim(run["id"])
    plan = services.db.fetch_one("SELECT plan_json FROM resolved_execution_plans WHERE id=?", (run["resolved_plan_id"],))["plan"]
    with execution_scope(fence):
        embed(services.db,HashEmbeddingProvider(),["synthetic"],context,purpose="query_embedding",resource_id=run["id"])
        with pytest.raises(BudgetExceeded):
            RunBudget(services.db,run["id"],plan,services.model_gateway.identity()).reserve(input_tokens=1,output_tokens=1)
    rows = services.db.fetch_all("SELECT * FROM usage_ledger WHERE run_id=?", (run["id"],))
    assert len(rows) == 1 and rows[0]["purpose"] == "query_embedding"
    assert rows[0]["billing_status"] == "ACTUAL"


def test_disabled_ingestion_initiator_cannot_make_provider_calls(runtime):
    client,services,context,_,_=runtime
    now=services.db.current_time().isoformat()
    services.db.execute("""INSERT INTO users(id,username,display_name,password_hash,tenant_id,project_id,
        environment_id,roles_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (context.user_id,"metering-fixture-owner","Synthetic owner","not-a-login-password",context.tenant_id,
         context.project_id,context.environment_id,services.db.encode(context.roles),now,now))
    embedding=CountingEmbedding()
    services.knowledge.embedding=embedding
    _,job=prepare_document(client)
    services.db.execute("UPDATE users SET status='INACTIVE' WHERE id=?",(context.user_id,))
    asyncio.run(services.knowledge._process_job(job["id"]))
    row=services.db.fetch_one("SELECT * FROM knowledge_ingestion_jobs WHERE id=?",(job["id"],))
    assert row["status"]=="FAILED" and "revoked" in row["error_message"]
    assert embedding.calls==0
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM metered_calls")["n"]==0
