from __future__ import annotations

import hashlib
import hmac
import time
import asyncio

import pytest
from fastapi.testclient import TestClient

from apps.platform_api.main import create_app
from packages.runtime.model_gateway import DeterministicModelGateway
from packages.sandbox.fake_provider import FakeSandboxProvider
from packages.auth import AuthAuthorizationError
from packages.domain.models import RunCreate, TenantContext, ThreadCreate
from packages.routing.models import RoutedRunCreate


def _app(tmp_path, **overrides):
    return create_app(
        str(tmp_path / "security.db"),
        seed=overrides.pop("seed", True),
        model_gateway=DeterministicModelGateway(),
        load_env=False,
        trust_identity_headers=overrides.pop("trust_identity_headers", True),
        allow_demo_identity=overrides.pop("allow_demo_identity", False),
        sandbox_providers=[FakeSandboxProvider()],
        **overrides,
    )


def _identity(role: str) -> dict[str, str]:
    return {
        "X-Tenant-ID": "tenant_demo",
        "X-Project-ID": "project_atlas",
        "X-Environment-ID": "env_development",
        "X-User-ID": f"user_{role}",
        "X-Roles": role,
    }


@pytest.mark.parametrize("role", ["viewer", "unknown_role"])
def test_read_only_roles_cannot_create_or_resume_runs_through_alternate_entries(tmp_path, role):
    app = _app(tmp_path)
    with TestClient(app) as client:
        member = _identity("member")
        restricted = _identity(role)
        text = "请把这个版本部署到生产环境，并准备发布记录。"
        decision = client.post("/api/v1/intent-routing:resolve", headers=member, json={"input": text}).json()
        assert decision["status"] == "READY"
        deployment_id = decision["selected_deployment_id"]
        routes = [
            ("/api/v1/intent-routing:resolve", {"input": text}),
            ("/api/v1/routed-runs", {"decision_id": decision["id"], "input": text}),
            ("/api/v1/threads", {"agent_deployment_id": deployment_id}),
            ("/api/v1/threads/missing/runs", {"input": text}),
            ("/api/v1/runs/missing/input", {"input": text}),
            ("/api/v1/runs/missing:retry", {}),
            ("/api/v1/runs/missing:cancel", {}),
        ]
        for route, payload in routes:
            assert client.post(route, headers=restricted, json=payload).status_code == 403, route
        context = TenantContext(tenant_id="tenant_demo", project_id="project_atlas", roles=[role])
        services = app.state.services
        with pytest.raises(AuthAuthorizationError):
            services.runs.create_thread(ThreadCreate(agent_deployment_id=deployment_id), context)
        with pytest.raises(AuthAuthorizationError):
            asyncio.run(services.runs.create_run("missing", RunCreate(input=text), context))
        with pytest.raises(AuthAuthorizationError):
            asyncio.run(services.routing.create_routed_run(
                RoutedRunCreate(decision_id=decision["id"], input=text), context,
            ))
        assert services.db.fetch_one("SELECT COUNT(*) AS n FROM runs")["n"] == 0
        accepted = client.post("/api/v1/routed-runs", headers=member,
            json={"decision_id": decision["id"], "input": text})
        assert accepted.status_code == 201, accepted.text


def test_central_permissions_enforce_read_author_publish_and_approval(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        viewer = _identity("viewer")
        developer = _identity("developer")
        operator = _identity("operator")

        visible = client.get("/api/v1/agents", headers=viewer)
        assert visible.status_code == 200
        denied_create = client.post(
            "/api/v1/agents", headers=viewer, json={"name": "Denied Agent"}
        )
        assert denied_create.status_code == 403
        assert denied_create.json()["detail"] == "Permission is required: agent.author"

        created = client.post(
            "/api/v1/agents", headers=developer, json={"name": "Governed Agent"}
        )
        assert created.status_code == 201, created.text
        agent_id = created.json()["id"]
        denied_publish = client.post(
            f"/api/v1/agents/{agent_id}/revisions:publish", headers=developer
        )
        assert denied_publish.status_code == 403
        published = client.post(
            f"/api/v1/agents/{agent_id}/revisions:publish", headers=operator
        )
        assert published.status_code == 201, published.text

        denied_decision = client.post(
            "/api/v1/interrupts/not-present/decisions",
            headers=viewer,
            json={"action": "approve", "items": []},
        )
        assert denied_decision.status_code == 403
        assert denied_decision.json()["detail"] == "Permission is required: approval.decide"

        context = client.get("/api/v1/context", headers=operator).json()
        assert "approval.decide" in context["user"]["permissions"]
        assert "agent.author" not in context["user"]["permissions"]


def test_cookie_authenticated_writes_require_double_submit_csrf(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPAGENT_BOOTSTRAP_ADMIN_PASSWORD", "Console1@")
    with TestClient(
        _app(
            tmp_path,
            seed=False,
            trust_identity_headers=False,
            allow_demo_identity=False,
        )
    ) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "Console1@"},
        )
        assert login.status_code == 200
        user = login.json()["user"]
        rejected = client.put(
            "/api/v1/auth/password",
            json={
                "current_password": "Console1@",
                "new_password": "SecureReady2@",
                "version": user["version"],
            },
        )
        assert rejected.status_code == 403
        assert rejected.json()["error"]["code"] == "CSRF_VALIDATION_FAILED"

        csrf = client.cookies.get("deepagent_csrf")
        accepted = client.put(
            "/api/v1/auth/password",
            headers={"X-CSRF-Token": csrf},
            json={
                "current_password": "Console1@",
                "new_password": "SecureReady2@",
                "version": user["version"],
            },
        )
        assert accepted.status_code == 200, accepted.text


def test_security_headers_origin_and_request_size_are_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPAGENT_MAX_REQUEST_BYTES", "1024")
    with TestClient(_app(tmp_path)) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.headers["x-content-type-options"] == "nosniff"
        assert health.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in health.headers["content-security-policy"]

        rejected_origin = client.post(
            "/api/v1/auth/login",
            headers={"Origin": "https://attacker.example"},
            json={"username": "admin", "password": "Console1@"},
        )
        assert rejected_origin.status_code == 403
        assert rejected_origin.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"

        too_large = client.post(
            "/api/v1/auth/login",
            content=b"x" * 2048,
            headers={"Content-Type": "application/json"},
        )
        assert too_large.status_code == 413


def test_signed_trusted_identity_headers_reject_spoofing(tmp_path, monkeypatch):
    secret = "identity-signing-secret-for-tests"
    monkeypatch.setenv("DEEPAGENT_IDENTITY_HEADER_SECRET", secret)
    with TestClient(_app(tmp_path)) as client:
        headers = _identity("viewer")
        assert client.get("/api/v1/context", headers=headers).status_code == 401

        timestamp = str(int(time.time()))
        canonical = "\n".join((*headers.values(), timestamp))
        signature = hmac.new(
            secret.encode(), canonical.encode(), hashlib.sha256
        ).hexdigest()
        headers.update(
            {"X-Identity-Timestamp": timestamp, "X-Identity-Signature": signature}
        )
        accepted = client.get("/api/v1/context", headers=headers)
        assert accepted.status_code == 200, accepted.text

        headers["X-User-ID"] = "spoofed"
        assert client.get("/api/v1/context", headers=headers).status_code == 401


def test_production_startup_rejects_unsafe_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPAGENT_ENVIRONMENT", "production")
    monkeypatch.setenv("DEEPAGENT_CORS_ORIGINS", "https://console.example.com")
    monkeypatch.setenv("DEEPAGENT_ALLOWED_HOSTS", "console.example.com")
    monkeypatch.setenv("DEEPAGENT_SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("DEEPAGENT_BOOTSTRAP_ADMIN_PASSWORD", "Console1@")
    app = _app(
        tmp_path,
        seed=False,
        trust_identity_headers=False,
        allow_demo_identity=False,
    )
    with pytest.raises(RuntimeError, match="Unsafe production configuration"):
        with TestClient(app):
            pass
