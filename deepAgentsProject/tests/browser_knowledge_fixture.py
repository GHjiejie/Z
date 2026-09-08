"""Disposable UI acceptance with synthetic identity and one lost create response.

Run PYTHONPATH=. .venv/bin/python3.13 tests/browser_knowledge_fixture.py after
building the console. No business credentials, databases or providers are used.
"""
from __future__ import annotations

import os
import secrets
import socket
import tempfile
from contextlib import asynccontextmanager
from datetime import timedelta

import uvicorn
from fastapi.responses import JSONResponse

from apps.platform_api.main import create_app
from packages.auth.models import UserCreate
from packages.runtime.model_gateway import DeterministicModelGateway
from packages.sandbox.fake_provider import FakeSandboxProvider


def main():
    if os.getenv("DEEPAGENT_ENVIRONMENT", "development") not in {"development", "test"}:
        raise RuntimeError("This synthetic fixture must not run in production")
    with socket.socket() as listener, tempfile.TemporaryDirectory(prefix="deepagent-knowledge-browser-") as directory:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        os.environ["DEEPAGENT_PROCESS_ROLE"] = "all"
        os.environ["DEEPAGENT_ALLOWED_HOSTS"] = "knowledge-ui.localhost"
        os.environ["DEEPAGENT_CORS_ORIGINS"] = f"http://knowledge-ui.localhost:{port}"
        app = create_app(directory + "/fixture.db", seed=True, load_env=False,
            model_gateway=DeterministicModelGateway(), sandbox_providers=[FakeSandboxProvider()])
        token = secrets.token_urlsafe(32)
        original = app.router.lifespan_context
        discarded = False

        @asynccontextmanager
        async def lifespan(application):
            async with original(application):
                services = application.state.services
                user = services.auth.create_user(UserCreate(username="knowledge.fixture", display_name="Knowledge UI Fixture",
                    password="Fixture-" + secrets.token_urlsafe(24) + "1!", roles=["owner"]))
                services.db.execute("UPDATE users SET must_change_password=0 WHERE id=?", (user["id"],))
                now = services.db.current_time()
                services.db.execute("""INSERT INTO auth_sessions
                    (id,token_hash,user_id,expires_at,created_at,last_seen_at) VALUES(?,?,?,?,?,?)""",
                    ("knowledge_ui_session", services.auth.hash_token(token), user["id"],
                     (now + timedelta(hours=1)).isoformat(), now.isoformat(), now.isoformat()))
                yield
                print("Knowledge fixture final counts:", {
                    table: services.db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
                    for table in ("knowledge_bases", "knowledge_documents", "knowledge_document_versions", "idempotency_records")}, flush=True)

        app.router.lifespan_context = lifespan

        @app.middleware("http")
        async def synthetic_identity_and_lost_response(request, call_next):
            nonlocal discarded
            request.scope["headers"] = [(key, value) for key, value in request.scope["headers"]
                if key.lower() not in {b"authorization", b"cookie"}]
            request.scope["headers"].append((b"authorization", f"Bearer {token}".encode()))
            response = await call_next(request)
            if not discarded and request.method == "POST" and request.url.path == "/api/v1/knowledge-bases" and response.status_code == 201:
                discarded = True
                print("Discarded successful create response; retry must reuse the existing resource.", flush=True)
                return JSONResponse(status_code=503, content={"error": {"message": "Synthetic lost response: retry the same creation."}})
            return response

        print(f"Knowledge UI fixture: http://knowledge-ui.localhost:{port}/knowledge", flush=True)
        uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, proxy_headers=False)).run(sockets=[listener])


if __name__ == "__main__":
    main()
