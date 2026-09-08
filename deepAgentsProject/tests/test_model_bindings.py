from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from packages.application.services import ConflictError
from packages.auth.service import AuthAuthorizationError
from packages.runtime.budget import RunBudget, RunBudgetExceeded
from packages.runtime.model_registry import ModelProfile, ModelRegistry
from tests.test_runtime_concurrency import runtime


@pytest.fixture
def registry(runtime,tmp_path,monkeypatch):
    client,services,context,_,_=runtime
    profiles = [{"id":name,"name":name,"tenant_id":context.tenant_id,"project_id":context.project_id,
        "model":"same-model","base_url":f"https://approved.test/{name}/v1",
        "credential_env":f"DEEPAGENT_MODEL_KEY_{name.upper()}","input_per_million":str(index+1),
        "output_per_million":str(index+2),"capabilities":["streaming","tool_calling"]}
        for index,name in enumerate(["alpha","beta"])]
    profiles.append({**profiles[0],"id":"other-project","project_id":"other-project"})
    path=tmp_path / "profiles.json"
    path.write_text(json.dumps(profiles))
    monkeypatch.setenv("DEEPAGENT_MODEL_PROFILES_FILE",str(path))
    for name in ("ALPHA","BETA"):
        monkeypatch.setenv(f"DEEPAGENT_MODEL_KEY_{name}",f"synthetic-{name}")
    models = ModelRegistry(services.db,services.model_gateway,allow_test_override=True)
    services.models=models
    services.runs.model_registry=models
    services.orchestrator.executors.models=models
    try:
        yield client,services,context,models
    finally:
        asyncio.run(models.close())


def publish(client,profile_id):
    model=client.post("/api/v1/model-deployments",json={"profile_id":profile_id,"reason":"Register approved test profile"})
    assert model.status_code==201,model.text
    model=model.json()
    agent=client.post("/api/v1/agents",json={"name":profile_id,"draft":{"model_deployment_id":model["id"],
        "capabilities":{"tools":[],"subagents":[]}}}).json()
    revision=client.post(f"/api/v1/agents/{agent['id']}/revisions:publish")
    assert revision.status_code==201,revision.text
    deployment=client.post("/api/v1/agent-deployments",json={"agent_revision_id":revision.json()["revision"]["id"]}).json()
    thread=client.post("/api/v1/threads",json={"agent_deployment_id":deployment["id"]}).json()
    return model,deployment,thread


def test_per_plan_gateway_endpoint_credentials_and_pricing(registry,monkeypatch):
    client,services,context,models=registry
    requests=[]
    original=ModelProfile.gateway

    def gateway(profile):
        instance=original(profile)

        def respond(request):
            requests.append(request)
            events=[{"model":"same-model","choices":[{"delta":{"content":profile.id+" response"},"finish_reason":"stop"}]},
                    {"model":"same-model","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}]
            body="".join("data: "+json.dumps(event)+"\n\n" for event in events)+"data: [DONE]\n\n"
            return httpx.Response(200,text=body,headers={"Content-Type":"text/event-stream"})

        instance.transport=httpx.MockTransport(respond)
        return instance

    monkeypatch.setattr(ModelProfile,"gateway",gateway)
    costs=[]
    for profile_id in ("alpha","beta"):
        model,deployment,thread=publish(client,profile_id)
        plan=services.db.fetch_one("SELECT plan_json FROM resolved_execution_plans WHERE id=?",(deployment["resolved_plan_id"],))["plan"]
        serialized=json.dumps(plan)
        assert "synthetic-" not in serialized and "credential_env" not in serialized
        assert plan["model_snapshot"]["runtime_binding"]==models.profiles[profile_id].binding()
        run=client.post(f"/api/v1/threads/{thread['id']}/runs",json={"input":"Give a concise status summary."})
        assert run.status_code==202,run.text
        run=run.json()
        fence=services.orchestrator.run_leases.claim(run["id"])
        executor=services.orchestrator.executors.resolve(plan)
        asyncio.run(services.orchestrator._execute(fence,executor))
        result=client.get(f"/api/v1/runs/{run['id']}").json()
        assert result["status"]=="SUCCEEDED",result
        assert profile_id in result["output"]
        row=services.db.fetch_one("SELECT * FROM metered_calls WHERE run_id=?",(run["id"],))
        assert row["model_identity"]==models.profiles[profile_id].identity()
        costs.append(row["charged_micro_usd"])
    assert [request.url.path for request in requests]==["/alpha/v1/chat/completions","/beta/v1/chat/completions"]
    assert [request.headers["authorization"] for request in requests]==["Bearer synthetic-ALPHA","Bearer synthetic-BETA"]
    assert costs==[7,12]
    assert services.orchestrator.executors.reference.model_gateway is services.model_gateway


def test_native_coding_model_is_bound_and_cleaned_up(registry):
    client,services,context,models=registry
    for profile_id in ("alpha","beta"):
        _,deployment,_=publish(client,profile_id)
        plan=services.db.fetch_one("SELECT plan_json FROM resolved_execution_plans WHERE id=?",(deployment["resolved_plan_id"],))["plan"]
        gateway=models.gateway(plan)
        native=models.coding_model(plan,gateway,None)
        assert native.model_name=="same-model"
        assert native.openai_api_base==f"https://approved.test/{profile_id}/v1"
        assert native.openai_api_key.get_secret_value()==f"synthetic-{profile_id.upper()}"
        assert models.coding_model(plan,gateway,None) is native


def test_profile_scope_status_and_optimistic_lock(registry):
    client,services,context,models=registry
    assert {item["id"] for item in client.get("/api/v1/model-profiles").json()["items"]}=={"alpha","beta"}
    assert client.post("/api/v1/model-deployments",json={"profile_id":"other-project","reason":"Denied cross-project registration"}).status_code==404
    model,deployment,thread=publish(client,"alpha")
    run=client.post(f"/api/v1/threads/{thread['id']}/runs",json={"input":"Pending model disable check"}).json()
    payload={"version":1,"enabled":False,"reason":"Stop this model for maintenance"}
    endpoint=f"/api/v1/model-deployments/{model['id']}/status"
    assert client.put(endpoint,json=payload).status_code==200
    assert client.put(endpoint,json=payload).status_code==409
    with pytest.raises(AuthAuthorizationError,match="disabled"):
        services.runs.access.require_execution(run["id"])
    new_thread=client.post("/api/v1/threads",json={"agent_deployment_id":deployment["id"]}).json()
    assert client.post(f"/api/v1/threads/{new_thread['id']}/runs",json={"input":"Must not dispatch"}).status_code==409
    assert client.put(endpoint,json={**payload,"version":2,"enabled":True}).status_code==200
    assert client.post(f"/api/v1/threads/{new_thread['id']}/runs",json={"input":"Allowed again"}).status_code==202


def test_binding_rejects_changed_profile_price_and_same_name_other_endpoint(registry,monkeypatch):
    client,services,context,models=registry
    _,deployment,_=publish(client,"alpha")
    plan=services.db.fetch_one("SELECT plan_json FROM resolved_execution_plans WHERE id=?",(deployment["resolved_plan_id"],))["plan"]
    identity=models.profiles["alpha"].identity()
    for changed in ({**identity,"route":"https://approved.test/beta/v1"},{**identity,"provider":"responses"}):
        with pytest.raises(RunBudgetExceeded,match="endpoint"):
            RunBudget(services.db,"unused",plan,changed)._pricing()
    tampered=copy.deepcopy(plan)
    tampered["model_snapshot"]["pricing"]["input_per_million"]="0"
    with pytest.raises(ConflictError,match="pricing"):
        models.validate_plan(tampered)
    models.profiles["alpha"]=models.profiles["alpha"].model_copy(update={"base_url":"https://approved.test/changed/v1"})
    with pytest.raises(ConflictError,match="changed"):
        models.validate_plan(plan)
    legacy=services.db.fetch_one("SELECT plan_json FROM resolved_execution_plans WHERE id!=? ORDER BY created_at LIMIT 1",(deployment["resolved_plan_id"],))["plan"]
    monkeypatch.setenv("DEEPAGENT_ENVIRONMENT","production")
    with pytest.raises(ConflictError,match="immutable"):
        models.validate_plan(legacy)
    with pytest.raises(RunBudgetExceeded,match="immutable"):
        RunBudget(services.db,"unused",legacy,identity)._pricing()


def test_unapproved_config_does_not_echo_inline_credentials(runtime,tmp_path,monkeypatch):
    _,services,_,_,_=runtime
    path=tmp_path / "invalid-profiles.json"
    path.write_text(json.dumps([{"id":"bad","api_key":"synthetic-must-not-leak"}]))
    monkeypatch.setenv("DEEPAGENT_MODEL_PROFILES_FILE",str(path))
    with pytest.raises(RuntimeError) as captured:
        ModelRegistry(services.db,services.model_gateway)
    assert "synthetic-must-not-leak" not in str(captured.value)
