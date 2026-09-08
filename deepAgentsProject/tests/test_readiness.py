import asyncio
from dataclasses import replace
from datetime import timedelta
from threading import Event

import pytest

from packages.operations.health import HealthSettings
from packages.runtime.worker_lease import WorkerLease
from test_runtime_concurrency import runtime, new_run


def online(services):
    now = services.db.current_time().isoformat()
    for kind in ("runtime", "knowledge"):
        services.db.execute("""INSERT INTO worker_nodes
            (id,worker_type,status,started_at,heartbeat_at,metadata_json)
            VALUES(?,?,'ONLINE',?,?,'{}')""", ("probe-" + kind, kind, now, now))


def test_readiness_distinguishes_liveness_workers_and_queue(runtime):
    client, services, *_ = runtime
    assert client.get("/livez").status_code == 200
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["workers"]["online"] == 0
    assert response.json()["status"] == "degraded"
    online(services)
    client.portal.call(services.health.refresh)
    assert client.get("/readyz").status_code == 200
    assert set(client.get("/readyz").json()) == {"status", "service", "checks", "checked_at"}
    run = new_run(runtime)
    services.health.settings = replace(services.health.settings, runtime_queue_limit=1)
    client.portal.call(services.health.refresh)
    assert client.get("/readyz").json()["checks"]["runtime_queue"] == "overloaded"
    assert client.get("/readyz").status_code == 503
    assert client.get("/api/v1/context").json()["runtime"]["status"] == "degraded"
    assert client.get("/api/v1/overview").json()["runtime"]["status"] == "degraded"
    services.health.settings = replace(services.health.settings, runtime_queue_limit=10)
    old = (services.db.current_time() - timedelta(seconds=301)).isoformat()
    services.db.execute("UPDATE run_attempts SET created_at=? WHERE id=?", (old, run["current_attempt_id"]))
    client.portal.call(services.health.refresh)
    assert client.get("/readyz").json()["checks"]["runtime_queue"] == "stalled"
    assert client.get("/livez").status_code == 200
    services.db.execute("UPDATE worker_nodes SET heartbeat_at=? WHERE worker_type='runtime'", (old,))
    client.portal.call(services.health.refresh)
    assert client.get("/readyz").json()["checks"]["runtime_workers"] == "unavailable"


def test_probe_requests_use_cache_and_never_expose_dependency_errors(runtime, monkeypatch):
    client, services, *_ = runtime
    online(services)
    client.portal.call(services.health.refresh)

    def unavailable():
        raise RuntimeError("secret-dsn-password-that-must-not-leak")

    monkeypatch.setattr(services.health, "collect", unavailable)
    assert client.get("/readyz").status_code == 200
    client.portal.call(services.health.refresh)
    for path in ("/readyz", "/health"):
        response = client.get(path)
        assert response.status_code == 503
        assert "secret-dsn" not in response.text
        assert response.headers["cache-control"] == "no-store"
    assert client.get("/livez").status_code == 200
    services.health.observed -= services.health.settings.stale_after + 1
    assert client.get("/readyz").json()["checks"]["observation"] == "stale_observation"


def test_timed_out_collections_are_single_flight_and_late_results_are_discarded(runtime, monkeypatch):
    client, services, *_ = runtime
    online(services)
    monitor = services.health
    monitor.settings = replace(monitor.settings, timeout=.05)
    original = monitor.collect
    release, started = Event(), Event()
    calls = []

    def slow_once():
        calls.append(1)
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
        return original()

    monkeypatch.setattr(monitor, "collect", slow_once)
    try:
        client.portal.call(monitor.refresh)
        assert started.is_set()
        for _ in range(3):
            client.portal.call(monitor.refresh)
            assert client.get("/readyz").status_code == 503
        assert len(calls) == 1
    finally:
        release.set()

    async def complete():
        await asyncio.wait_for(asyncio.shield(monitor.inflight), 3)
    client.portal.call(complete)
    assert client.get("/readyz").status_code == 503
    client.portal.call(monitor.refresh)
    assert len(calls) == 2
    assert client.get("/readyz").status_code == 200


def test_completed_consumer_cannot_keep_advertising_online(runtime):
    client, services, *_ = runtime

    async def probe():
        lease = WorkerLease(services.db, "failed-consumer", "runtime", {}, heartbeat_seconds=.01)
        done = asyncio.create_task(asyncio.sleep(0))
        await done
        lease.consumers = (done,)
        await lease.start()
        try:
            for _ in range(100):
                row = services.db.fetch_one("SELECT status FROM worker_nodes WHERE id=?", (lease.worker_id,))
                if row["status"] == "DEGRADED":
                    break
                await asyncio.sleep(.01)
            assert row["status"] == "DEGRADED"
            services.orchestrator.task = done
            assert services.health.snapshot()["checks"]["runtime_workers"] == "local_consumer_failed"
        finally:
            services.orchestrator.task = None
            await lease.stop()
    client.portal.call(probe)


@pytest.mark.parametrize("settings", [{"interval": 0}, {"timeout": float("nan")}, {"stale_after": 3}, {"knowledge_queue_limit": -1}])
def test_invalid_health_thresholds_fail_startup(settings):
    with pytest.raises(ValueError):
        HealthSettings(**settings)
