from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from apps.platform_api.main import create_app
from packages.runtime.model_gateway import DeterministicModelGateway, ModelGatewayError
from packages.sandbox.fake_provider import FakeSandboxProvider


class CountingGateway(DeterministicModelGateway):
    def __init__(self, *, fail=False):
        self.calls = 0
        self.fail = fail

    async def complete(self, messages, on_event=None):
        self.calls += 1
        if self.fail:
            raise ModelGatewayError("Synthetic failure after dispatch")
        return await super().complete(messages, on_event)


def wait_run(client, run_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = client.get(f"/api/v1/runs/{run_id}").json()
        if run["status"] in {"SUCCEEDED", "FAILED_BUDGET", "FAILED"}:
            return run
        time.sleep(.02)
    raise AssertionError("Run did not finish")


@pytest.mark.parametrize("max_cost, fail", [(0, False), (5, False), (5, True)])
def test_reference_model_admission_and_failed_call_accounting(tmp_path, max_cost, fail):
    gateway = CountingGateway(fail=fail)
    app = create_app(str(tmp_path / "budget.db"), seed=True, load_env=False,
        model_gateway=gateway, sandbox_providers=[FakeSandboxProvider()])
    with TestClient(app) as client:
        agent = client.post("/api/v1/agents", json={"name": "Budget checks", "draft": {
            "limits": {"max_cost": max_cost, "max_model_calls": 1},
            "capabilities": {"tools": [], "subagents": []},
        }}).json()
        revision = client.post(f"/api/v1/agents/{agent['id']}/revisions:publish").json()["revision"]
        deployment = client.post("/api/v1/agent-deployments", json={"agent_revision_id": revision["id"]}).json()
        thread = client.post("/api/v1/threads", json={"agent_deployment_id": deployment["id"]}).json()
        created = client.post(f"/api/v1/threads/{thread['id']}/runs", json={"input": "Explain what a checklist is."})
        assert created.status_code == 202
        run = wait_run(client, created.json()["id"])
        if max_cost == 0:
            assert gateway.calls == 0
            assert run["status"] == "FAILED_BUDGET"
            assert run["usage"]["model_calls"] == 0
        elif fail:
            assert run["status"] == "FAILED"
            assert run["usage"]["unsettled_model_calls"] == 1
            assert run["usage"]["cost"] > 0
            retried = client.post(f"/api/v1/runs/{run['id']}:retry")
            assert retried.status_code == 200
            recovered = wait_run(client, run["id"])
            assert recovered["status"] == "FAILED_BUDGET"
            assert recovered["usage"]["model_calls"] == 1
            assert gateway.calls == 1
        else:
            assert run["status"] == "SUCCEEDED"
            assert gateway.calls == run["usage"]["model_calls"] == 1
            assert run["usage"]["unsettled_model_calls"] == 0
            assert run["usage"]["cost"] > 0
