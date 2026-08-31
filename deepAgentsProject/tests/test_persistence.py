from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from apps.platform_api.main import create_app
from packages.persistence import Database, create_database
from packages.runtime.model_gateway import DeterministicModelGateway
from packages.sandbox.fake_provider import FakeSandboxProvider


def test_sqlite_migration_gate_and_nested_transaction_rollback(tmp_path):
    path = str(tmp_path / "persistence.db")
    database = Database(path)
    try:
        with pytest.raises(RuntimeError, match="run the migration job"):
            database.initialize(auto_migrate=False)
        database.initialize()
        assert database.schema_versions() == list(range(1, 21))
        with pytest.raises(ValueError):
            with database.transaction():
                database.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) VALUES(?,?,?)",
                    (999, "must-roll-back", "now"),
                )
                with database.transaction():
                    assert database.fetch_one(
                        "SELECT version FROM schema_migrations WHERE version=?", (999,)
                    )
                raise ValueError("rollback")
        assert database.fetch_one(
            "SELECT version FROM schema_migrations WHERE version=?", (999,)
        ) is None
    finally:
        database.close()


def test_process_role_is_fixed_when_the_application_is_created(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPAGENT_PROCESS_ROLE", "api")
    application = create_app(
        str(tmp_path / "role.db"), seed=True,
        model_gateway=DeterministicModelGateway(), load_env=False,
        sandbox_providers=[FakeSandboxProvider()],
    )
    monkeypatch.setenv("DEEPAGENT_PROCESS_ROLE", "worker")
    with TestClient(application):
        services = application.state.services
        assert services.orchestrator.task is None
        assert services.knowledge.task is None


@pytest.mark.skipif(
    not os.getenv("DEEPAGENT_TEST_POSTGRES_URL"),
    reason="DEEPAGENT_TEST_POSTGRES_URL is required for PostgreSQL integration",
)
def test_postgres_database_checkpoint_and_split_worker_contract(monkeypatch):
    url = os.environ["DEEPAGENT_TEST_POSTGRES_URL"]
    database = create_database(url)
    try:
        database.initialize()
        assert database.dialect == "postgresql"
        assert database.ping() is True
        with pytest.raises(ValueError):
            with database.transaction():
                database.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) VALUES(?,?,?)",
                    (999, "must-roll-back", "now"),
                )
                raise ValueError("rollback")
        assert database.fetch_one(
            "SELECT version FROM schema_migrations WHERE version=?", (999,)
        ) is None
    finally:
        database.close()

    monkeypatch.setenv("DEEPAGENT_PROCESS_ROLE", "api")
    api_app = create_app(
        url,
        seed=True,
        model_gateway=DeterministicModelGateway(),
        load_env=False,
        sandbox_providers=[FakeSandboxProvider()],
    )
    monkeypatch.setenv("DEEPAGENT_PROCESS_ROLE", "worker")
    worker_app = create_app(
        url,
        seed=True,
        model_gateway=DeterministicModelGateway(),
        load_env=False,
        sandbox_providers=[FakeSandboxProvider()],
    )
    with TestClient(api_app) as client:
        assert client.get("/livez").status_code == 200
        assert client.get("/health").status_code == 503
        agents = client.get("/api/v1/agents")
        assert agents.status_code == 200
        assert len(agents.json()["items"]) >= 2
        deployment = next(
            item
            for item in client.get("/api/v1/agent-deployments").json()["items"]
            if not item["coding_enabled"]
        )
        thread = client.post(
            "/api/v1/threads",
            json={"agent_deployment_id": deployment["id"], "title": "durable queue"},
        ).json()
        run = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            json={"input": "Provide a concise status update."},
        ).json()
        time.sleep(0.3)
        assert client.get(f"/api/v1/runs/{run['id']}").json()["status"] == "QUEUED"
        assert client.app.state.services.orchestrator.queue.qsize() >= 1

        emitter = client.app.state.services.events
        with ThreadPoolExecutor(max_workers=8) as pool:
            emitted = list(pool.map(
                lambda index: emitter.append(run["id"], "concurrency.probe", {"index": index}),
                range(32),
            ))
        assert len({event["sequence"] for event in emitted}) == 32
        all_events = emitter.list(run["id"])
        assert [event["sequence"] for event in all_events] == list(range(1, len(all_events) + 1))

        with TestClient(worker_app):
            deadline = time.time() + 8
            while time.time() < deadline:
                status = client.get(f"/api/v1/runs/{run['id']}").json()["status"]
                if status in {"SUCCEEDED", "FAILED"}:
                    break
                time.sleep(0.05)
        assert status == "SUCCEEDED"
