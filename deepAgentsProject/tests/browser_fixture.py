"""Disposable loopback UI fixture; never import into a deployed application.

After building apps/web, run PYTHONPATH=. .venv/bin/python3.13 tests/browser_fixture.py.
Open http://deepagent-ui.localhost:<the printed port>. The OS allocates an
unused loopback port so the fixture never replaces an existing service.
All identities and requests are synthetic. This fixture validates UI behavior,
not the login flow, which is covered separately in test_auth/test_security.
"""
from __future__ import annotations

import os
import secrets
import tempfile
from contextlib import asynccontextmanager
from datetime import timedelta

import uvicorn

from apps.platform_api.main import create_app
from packages.auth.models import UserCreate
from packages.domain.models import RunCreate, TenantContext, ThreadCreate
from packages.runtime.model_gateway import DeterministicModelGateway
from packages.sandbox.fake_provider import FakeSandboxProvider


def fixture_app(directory):
    if os.getenv("DEEPAGENT_ENVIRONMENT", "development") not in {"development", "test"}:
        raise RuntimeError("The browser fixture cannot run in a production environment")
    os.environ["DEEPAGENT_PROCESS_ROLE"] = "api"
    os.environ["DEEPAGENT_ALLOWED_HOSTS"] = "deepagent-ui.localhost"
    app = create_app(str(directory) + "/fixture.db", seed=True, load_env=False,
                     model_gateway=DeterministicModelGateway(), sandbox_providers=[FakeSandboxProvider()])
    original_lifespan = app.router.lifespan_context
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            services = application.state.services
            user = services.auth.create_user(UserCreate(username="ui_fixture", display_name="UI Fixture",
                password="Fixture-" + secrets.token_urlsafe(24) + "1!", roles=["owner"]))
            services.db.execute("UPDATE users SET must_change_password=0 WHERE id=?", (user["id"],))
            now = services.db.current_time()
            services.db.execute("""INSERT INTO auth_sessions
                (id,token_hash,user_id,expires_at,created_at,last_seen_at) VALUES(?,?,?,?,?,?)""",
                ("browser_fixture_session", services.auth.hash_token(token), user["id"],
                 (now + timedelta(hours=1)).isoformat(), now.isoformat(), now.isoformat()))
            context = TenantContext(tenant_id=user["tenant_id"], project_id=user["project_id"], user_id=user["id"])
            deployment = next(row for row in services.agents.list_deployments(context) if not row["coding_enabled"])
            for index in range(55):
                thread = services.runs.create_thread(ThreadCreate(agent_deployment_id=deployment["id"],
                    title=f"Pagination fixture {index:03d}"), context)
                run = await services.runs.create_run(thread["id"], RunCreate(input=f"Search fixture {index:03d}"),
                                                    context, enqueue=False)
                services.db.execute("UPDATE runs SET status=?,output=? WHERE id=?",
                    ("FAILED" if index % 2 else "SUCCEEDED", "Synthetic UI fixture", run["id"]))
            other = services.auth.create_user(UserCreate(username="hidden_fixture", display_name="Hidden Fixture",
                password="Fixture-" + secrets.token_urlsafe(24) + "1!", roles=["member"]))
            other_context = context.model_copy(update={"user_id": other["id"], "roles": ["member"]})
            thread = services.runs.create_thread(ThreadCreate(agent_deployment_id=deployment["id"],
                title="Private fixture from another user"), other_context)
            await services.runs.create_run(thread["id"], RunCreate(input="Hidden from the UI reviewer"),
                                           other_context, enqueue=False)
            yield

    app.router.lifespan_context = lifespan

    @app.middleware("http")
    async def fixture_identity(request, call_next):
        request.scope["headers"] = [(key, value) for key, value in request.scope["headers"]
                                    if key.lower() not in {b"authorization", b"cookie"}]
        request.scope["headers"].append((b"authorization", f"Bearer {token}".encode()))
        return await call_next(request)

    return app


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="deepagent-browser-fixture-") as directory:
        uvicorn.run(fixture_app(directory), host="127.0.0.1", port=0, proxy_headers=False)
