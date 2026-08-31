"""Loopback-only release UI fixture using actual services and controlled model calls.

Not imported by the deployed app. The two synthetic hosts select requester and
reviewer sessions, so browser tests never use or modify real accounts.
"""
import os
import secrets
import socket
import tempfile
from datetime import timedelta

import uvicorn
from fastapi.testclient import TestClient

from apps.platform_api.main import create_app
from packages.sandbox.fake_provider import FakeSandboxProvider
from release_helpers import authorities
from test_evaluations import ControlledGateway, _prepare, _evaluate


def main():
    if os.getenv("DEEPAGENT_ENVIRONMENT", "development") not in {"development", "test"}:
        raise RuntimeError("This synthetic fixture must not run in production")
    os.environ["DEEPAGENT_PROCESS_ROLE"] = "all"
    os.environ["DEEPAGENT_ALLOWED_HOSTS"] = "testserver,release-requester.localhost,release-reviewer.localhost"
    with socket.socket() as listener, tempfile.TemporaryDirectory(prefix="deepagent-release-browser-") as directory:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        os.environ["DEEPAGENT_CORS_ORIGINS"] = ",".join(
            f"http://{host}:{port}" for host in ("release-requester.localhost", "release-reviewer.localhost"))
        app = create_app(directory + "/fixture.db", seed=True, load_env=False,
            model_gateway=ControlledGateway(), sandbox_providers=[FakeSandboxProvider()])
        tokens = {}

        @app.middleware("http")
        async def synthetic_browser_identity(request, call_next):
            token = tokens.get(request.url.hostname)
            if token:
                request.scope["headers"] = [(key, value) for key, value in request.scope["headers"]
                    if key.lower() not in {b"authorization", b"cookie"}]
                request.scope["headers"].append((b"authorization", f"Bearer {token}".encode()))
            return await call_next(request)

        # This owns the fixture workers and DB lifecycle; the loopback HTTP server
        # only serves the already-started app. No paid or external services run.
        with TestClient(app) as client:
            suite, revision, _, samples = _prepare(client)
            assert client.put("/api/v1/evaluation-policy", json={"suite_id": suite["id"], "version": 1,
                "max_age_seconds": 86400, "reason": "Keep the UI fixture evidence valid for this test session"}).status_code == 200
            assert _evaluate(client, revision, samples).json()["production_eligible"] == 1
            _, requester, reviewer = authorities(client)
            if os.getenv("DEEPAGENT_ROUTING_UI_FIXTURE") == "true":
                pending = client.post("/api/v1/release-requests", headers=requester, json={
                    "agent_revision_id": revision["id"], "expected_channel_version": 0,
                    "reason": "Prepare isolated routing UI acceptance fixture"})
                assert pending.status_code == 202, pending.text
                applied = client.post(f"/api/v1/release-requests/{pending.json()['id']}:decide", headers=reviewer,
                    json={"version": 1, "decision": "approve", "reason": "Approve isolated routing fixture deployment"})
                assert applied.status_code == 200, applied.text
            services = app.state.services
            for host, headers in (("release-requester.localhost", requester), ("release-reviewer.localhost", reviewer)):
                token = secrets.token_urlsafe(32)
                tokens[host] = token
                user_id = headers["X-User-ID"]
                services.db.execute("UPDATE users SET must_change_password=0 WHERE id=?", (user_id,))
                now = services.db.current_time()
                services.db.execute("""INSERT INTO auth_sessions
                    (id,token_hash,user_id,expires_at,created_at,last_seen_at) VALUES(?,?,?,?,?,?)""",
                    ("fixture_" + user_id, services.auth.hash_token(token), user_id,
                     (now + timedelta(hours=1)).isoformat(), now.isoformat(), now.isoformat()))
            print(f"Release UI fixture port: {port}", flush=True)
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                lifespan="off", proxy_headers=False))
            server.run(sockets=[listener])


if __name__ == "__main__":
    main()
