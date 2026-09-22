"""HTTP integration coverage of tenant boundaries, policy and immutable runs."""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from agent_platform.apps.api.main import COOKIE, create_app
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.platform import append_event, hasher, now, uid
from agent_platform.modules.runtime.gateway import GatewayEvent, ModelReply

PASSWORD = "Strong-password-for-tests-42"


class FakeGateway:
    """An explicit offline test dependency, never a production fallback."""

    configured = True

    def __init__(self):
        self.calls = []

    async def stream(self, messages, model, max_tokens, temperature, tools):
        self.calls.append({"messages": messages, "model": model})
        yield GatewayEvent("text", {"text": "Verified test response"})
        yield GatewayEvent(
            "result",
            ModelReply(
                "Verified test response",
                [],
                10,
                5,
                {"prompt_tokens": 10, "completion_tokens": 5},
            ),
        )


class APIIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = Settings(
            database_url=f"sqlite:///{Path(self.temporary.name) / 'platform.db'}",
            admin_email="admin@example.test",
            admin_password=PASSWORD,
            embedded_worker=False,
            litellm_url="http://test",
            litellm_key="test",
            web_directory=Path(self.temporary.name) / "web",
        )
        self.gateway = FakeGateway()
        self.app = create_app(self.settings, gateway=self.gateway)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.platform = self.app.state.platform
        self.db = self.app.state.database
        self.admin = self.login()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temporary.cleanup()

    def login(self, email="admin@example.test", password=PASSWORD):
        response = self.client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return {**response.json(), "token": response.cookies[COOKIE]}

    def request(
        self, method, path, *, auth="admin", csrf=True, key=None, headers=None, **kwargs
    ):
        context = self.admin if auth == "admin" else auth
        request_headers = dict(headers or {})
        request_headers["Cookie"] = f"{COOKIE}={context['token']}" if context else ""
        if context and csrf:
            request_headers.setdefault("X-CSRF-Token", context["csrf_token"])
        if key is not None:
            request_headers["Idempotency-Key"] = key
        return self.client.request(
            method, f"/api/v1{path}", headers=request_headers, **kwargs
        )

    def create_user(
        self, *, email="member@example.test", role="member", password=PASSWORD
    ):
        response = self.request(
            "POST",
            "/users",
            json={
                "email": email,
                "name": "Test member",
                "password": password,
                "role": role,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def create_resources(self):
        model = self.request(
            "POST",
            "/models",
            json={
                "name": "Test model",
                "alias": "test-model",
                "input_price": "1",
                "output_price": "3",
                "context_window": 8192,
                "max_output_tokens": 1024,
            },
        )
        self.assertEqual(model.status_code, 201, model.text)
        model = model.json()
        agent = self.request(
            "POST",
            "/agents",
            json={
                "name": "Test agent",
                "model_id": model["id"],
                "system_prompt": "Original published prompt",
                "max_tokens": 512,
                "max_steps": 3,
                "tools": ["calculator"],
                "temperature": 0,
            },
        )
        self.assertEqual(agent.status_code, 201, agent.text)
        agent = agent.json()
        response = self.request("POST", f"/agents/{agent['id']}/publish")
        self.assertEqual(response.status_code, 200, response.text)
        session = self.request("POST", "/sessions", json={"agent_id": agent["id"]})
        self.assertEqual(session.status_code, 201, session.text)
        return model, agent, session.json()

    def enqueue(self, session, message="Test message", key="run-1", auth="admin"):
        return self.request(
            "POST",
            f"/sessions/{session['id']}/runs",
            auth=auth,
            key=key,
            json={"message": message},
        )

    def seed_second_tenant(self):
        tenant_id, user_id, model_id, agent_id, session_id, run_id = (
            uid() for _ in range(6)
        )
        stamp = now()
        user = {
            "id": user_id,
            "tenant_id": tenant_id,
            "created_at": stamp,
            "email": f"{user_id}@foreign.test",
            "name": "Foreign admin",
            "role": "admin",
            "active": True,
        }
        model = {
            "id": model_id,
            "tenant_id": tenant_id,
            "created_at": stamp,
            "name": "Foreign model",
            "alias": "foreign-model",
            "active": True,
            "input_price": "1",
            "output_price": "2",
            "context_window": 8192,
            "max_output_tokens": 1024,
            "price_version": 1,
        }
        agent = {
            "id": agent_id,
            "tenant_id": tenant_id,
            "created_at": stamp,
            "name": "Foreign agent",
            "description": "",
            "system_prompt": "Foreign private prompt",
            "model_id": model_id,
            "temperature": 0,
            "max_steps": 3,
            "max_tokens": 512,
            "tools": [],
            "published_version": 1,
        }
        session = {
            "id": session_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "agent_id": agent_id,
            "title": "Foreign conversation",
            "created_at": stamp,
        }
        run = {
            "id": run_id,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "user_id": user_id,
            "agent_name": agent["name"],
            "spec": {**agent, "model": model},
            "message": "Foreign message",
            "status": "succeeded",
            "created_at": stamp,
            "finished_at": stamp,
            "error": "",
            "idempotency_key": "foreign-run",
            "request_hash": "foreign-hash",
            "fence": 1,
        }
        with self.db.transaction(f"tenant:{tenant_id}") as conn:
            conn.execute(
                insert(t.tenants).values(
                    id=tenant_id, name="Foreign tenant", created_at=stamp
                )
            )
            conn.execute(
                insert(t.users).values(**user, password_hash=hasher.hash(PASSWORD))
            )
            conn.execute(insert(t.models).values(**model))
            conn.execute(insert(t.agents).values(**agent))
            conn.execute(
                insert(t.agent_versions).values(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    version=1,
                    spec=agent,
                    created_at=stamp,
                )
            )
            conn.execute(insert(t.sessions).values(**session))
            conn.execute(insert(t.runs).values(**run))
            conn.execute(
                insert(t.messages).values(
                    id=uid(),
                    tenant_id=tenant_id,
                    session_id=session_id,
                    run_id=run_id,
                    role="assistant",
                    content="Foreign secret",
                    created_at=stamp,
                )
            )
            append_event(conn, run_id, "run.completed", {"text": "Foreign secret"})
            conn.execute(
                insert(t.calls).values(
                    id=uid(),
                    tenant_id=tenant_id,
                    user_id=user_id,
                    run_id=run_id,
                    model_id=model_id,
                    model=model["name"],
                    agent_name=agent["name"],
                    user_email=user["email"],
                    status="confirmed",
                    input_tokens=10,
                    output_tokens=5,
                    cost="0.00002",
                    price_version=1,
                    created_at=stamp,
                    error="",
                )
            )
        self.platform.billing.credit(
            tenant_id, user_id, "99", "Foreign funds", "foreign-credit"
        )
        self.platform.billing.reserve(
            tenant_id, user_id, model_id, run_id, "foreign-call", 1, 1, "1", "1"
        )
        return {
            "tenant_id": tenant_id,
            "user": user,
            "model": model,
            "agent": agent,
            "session": session,
            "run": run,
        }

    def seed_unresolved_call(self, tenant=None):
        tenant = tenant or self.admin["user"]["tenant_id"]
        user_id = self.admin["user"]["id"]
        self.platform.billing.credit(
            tenant, user_id, "10", "Investigation funds", "credit"
        )
        call_id = uid()
        self.platform.billing.reserve(
            tenant, user_id, "model", "run", call_id, 100, 100, "1000", "1000"
        )
        self.platform.billing.unresolved(tenant, call_id, "Gateway dropped final usage")
        with self.db.transaction(f"tenant:{tenant}") as conn:
            conn.execute(
                insert(t.calls).values(
                    id=call_id,
                    tenant_id=tenant,
                    user_id=user_id,
                    run_id="run",
                    model_id="model",
                    model="Test model",
                    agent_name="Test agent",
                    user_email=self.admin["user"]["email"],
                    status="unresolved",
                    input_tokens=0,
                    output_tokens=0,
                    cost="0",
                    price_version=1,
                    created_at=now(),
                    error="Missing usage",
                )
            )
        return call_id

    def test_login_cookie_csrf_origin_and_logout_boundary(self):
        response = self.request("GET", "/auth/me")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("password_hash", response.text)
        self.assertNotIn("token", response.json()["user"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.request("GET", "/auth/me", auth=None).status_code, 401)
        response = self.request(
            "POST",
            "/billing/credits",
            csrf=False,
            key="bad",
            json={"amount": "1", "description": "Test"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "csrf_failed")
        response = self.request(
            "POST", "/auth/logout", headers={"Origin": "https://attacker.invalid"}
        )
        self.assertEqual(response.json()["error"]["code"], "invalid_origin")
        self.assertEqual(self.request("POST", "/auth/logout").status_code, 200)
        self.assertEqual(self.request("GET", "/auth/me").status_code, 401)

    def test_login_response_uses_httponly_strict_cookie(self):
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.test", "password": PASSWORD},
        )
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertNotIn("token", response.json())

    def test_validation_never_echoes_password_or_accepts_extra_tenant(self):
        secret = "top-secret-password"
        response = self.request(
            "POST",
            "/users",
            json={
                "email": "bad",
                "name": "Invalid",
                "password": secret,
                "tenant_id": "forged-tenant",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)
        self.assertNotIn("forged-tenant", response.text)
        self.assertEqual(len(self.request("GET", "/users").json()["items"]), 1)

    def test_login_failures_are_rate_limited(self):
        for _ in range(10):
            response = self.client.post(
                "/api/v1/auth/login",
                json={"email": "missing@example.test", "password": "Wrong"},
            )
            self.assertEqual(response.status_code, 401, response.text)
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": "missing@example.test", "password": "Wrong"},
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "60")

    def test_last_active_admin_cannot_disable_or_demote_itself(self):
        for values in ({"active": False}, {"role": "member"}):
            response = self.request(
                "PATCH", f"/users/{self.admin['user']['id']}", json=values
            )
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["error"]["code"], "last_admin")
        self.assertEqual(self.request("GET", "/auth/me").status_code, 200)

    def test_disabling_account_revokes_existing_sessions_and_prevents_login(self):
        member = self.create_user()
        context = self.login(member["email"])
        self.assertEqual(self.request("GET", "/auth/me", auth=context).status_code, 200)
        self.assertEqual(
            self.request(
                "PATCH", f"/users/{member['id']}", json={"active": False}
            ).status_code,
            200,
        )
        self.assertEqual(self.request("GET", "/auth/me", auth=context).status_code, 401)
        response = self.client.post(
            "/api/v1/auth/login", json={"email": member["email"], "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 401)

    def test_role_change_revokes_prior_admin_session(self):
        second = self.create_user(email="second-admin@example.test", role="admin")
        context = self.login(second["email"])
        self.assertEqual(
            self.request(
                "PATCH", f"/users/{second['id']}", json={"role": "member"}
            ).status_code,
            200,
        )
        self.assertEqual(self.request("GET", "/users", auth=context).status_code, 401)
        context = self.login(second["email"])
        self.assertEqual(self.request("GET", "/users", auth=context).status_code, 403)

    def test_password_change_revokes_all_sessions_and_preserves_password_whitespace(
        self,
    ):
        member = self.create_user(password="  Long password with spaces  ")
        context = self.login(member["email"], "  Long password with spaces  ")
        trimmed = self.client.post(
            "/api/v1/auth/login",
            json={"email": member["email"], "password": "Long password with spaces"},
        )
        self.assertEqual(trimmed.status_code, 401)
        response = self.request(
            "POST",
            "/auth/password",
            auth=context,
            json={
                "current_password": "  Long password with spaces  ",
                "new_password": "  Changed long password  ",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.request("GET", "/auth/me", auth=context).status_code, 401)
        self.login(member["email"], "  Changed long password  ")

    def test_member_cannot_manage_users_models_agents_finances_or_audit(self):
        member = self.create_user()
        context = self.login(member["email"])
        for path in (
            "/users",
            "/billing/ledger",
            "/billing/reservations",
            "/quotas",
            "/audit",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    self.request("GET", path, auth=context).status_code, 403
                )
        requests = [
            (
                "POST",
                "/users",
                {"email": "x@example.test", "name": "X", "password": PASSWORD},
            ),
            ("POST", "/models", {"name": "X", "alias": "x"}),
            ("POST", "/agents", {"name": "X", "model_id": "x"}),
            ("PATCH", f"/users/{member['id']}", {"role": "admin"}),
            ("POST", "/agents/unknown/publish", None),
            ("POST", "/billing/credits", {"amount": "100", "description": "Forged"}),
            ("POST", "/billing/unblock", {"reason": "Forged permission"}),
            (
                "POST",
                "/billing/reservations/unknown/resolve",
                {"action": "write_off", "reason": "Forged permission"},
            ),
            (
                "PUT",
                "/quotas",
                {"scope": "tenant", "subject_id": member["tenant_id"], "rpm": 1000},
            ),
        ]
        for method, path, payload in requests:
            with self.subTest(path=path):
                response = self.request(
                    method, path, auth=context, key="forged", json=payload
                )
                self.assertEqual(response.status_code, 403, response.text)

    def test_cross_tenant_lists_details_mutations_and_sse_are_isolated(self):
        foreign = self.seed_second_tenant()
        self.create_resources()
        for path in (
            "/users",
            "/models",
            "/agents",
            "/sessions",
            "/runs",
            "/usage/calls",
            "/billing/ledger",
            "/billing/reservations",
            "/audit",
        ):
            with self.subTest(path=path):
                response = self.request("GET", path)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn(foreign["tenant_id"], response.text)
                self.assertNotIn("Foreign secret", response.text)
        for path in (
            f"/sessions/{foreign['session']['id']}",
            f"/runs/{foreign['run']['id']}",
            f"/runs/{foreign['run']['id']}/events",
        ):
            response = self.request("GET", path)
            self.assertEqual(response.status_code, 404, response.text)
        for method, path, payload in (
            ("PATCH", f"/models/{foreign['model']['id']}", {"active": False}),
            ("PATCH", f"/users/{foreign['user']['id']}", {"active": False}),
            ("POST", f"/agents/{foreign['agent']['id']}/publish", None),
            ("POST", f"/runs/{foreign['run']['id']}/cancel", None),
            ("POST", "/sessions", {"agent_id": foreign["agent"]["id"]}),
        ):
            response = self.request(method, path, json=payload)
            self.assertEqual(response.status_code, 404, response.text)
        own_wallet = self.request("GET", "/billing/wallet").json()
        self.assertEqual(Decimal(own_wallet["balance"]), 0)

    def test_quota_subjects_must_belong_to_the_callers_tenant(self):
        foreign = self.seed_second_tenant()
        for scope, subject, status in (
            ("tenant", foreign["tenant_id"], 400),
            ("user", foreign["user"]["id"], 404),
            ("model", foreign["model"]["id"], 404),
        ):
            response = self.request(
                "PUT", "/quotas", json={"scope": scope, "subject_id": subject, "rpm": 1}
            )
            self.assertEqual(response.status_code, status, response.text)
        response = self.request(
            "PUT",
            "/quotas",
            json={
                "scope": "tenant",
                "subject_id": self.admin["user"]["tenant_id"],
                "rpm": 0,
                "max_budget": "0",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["rpm"], 0)
        self.assertEqual(Decimal(response.json()["max_budget"]), 0)

    def test_published_agent_and_run_model_prices_are_immutable_snapshots(self):
        model, agent, session = self.create_resources()
        self.request(
            "PATCH",
            f"/agents/{agent['id']}",
            json={"system_prompt": "New private draft", "temperature": 0.7},
        )
        member = self.create_user()
        context = self.login(member["email"])
        visible = self.request("GET", "/agents", auth=context).json()["items"]
        self.assertEqual(visible[0]["system_prompt"], "Original published prompt")
        self.assertEqual(visible[0]["published_version"], 1)
        queued = self.enqueue(session)
        self.assertEqual(queued.status_code, 202, queued.text)
        self.request(
            "PATCH",
            f"/models/{model['id']}",
            json={"input_price": "9", "output_price": "10"},
        )
        self.request("POST", f"/agents/{agent['id']}/publish")
        with self.db.read() as conn:
            spec = conn.scalar(
                select(t.runs.c.spec).where(t.runs.c.id == queued.json()["id"])
            )
        self.assertEqual(spec["system_prompt"], "Original published prompt")
        self.assertEqual(spec["temperature"], 0)
        self.assertEqual(spec["version"], 1)
        self.assertEqual(Decimal(spec["model"]["input_price"]), 1)
        self.assertEqual(Decimal(spec["model"]["output_price"]), 3)
        new_session = self.request(
            "POST", "/sessions", json={"agent_id": agent["id"]}
        ).json()
        queued = self.enqueue(new_session, key="new-price-run")
        with self.db.read() as conn:
            latest = conn.scalar(
                select(t.runs.c.spec).where(t.runs.c.id == queued.json()["id"])
            )
        self.assertEqual(latest["system_prompt"], "New private draft")
        self.assertEqual(latest["version"], 2)
        self.assertEqual(Decimal(latest["model"]["input_price"]), 9)

    def test_run_idempotency_body_conflicts_and_session_serialization(self):
        _, _, session = self.create_resources()
        first = self.enqueue(session)
        self.assertEqual(first.status_code, 202, first.text)
        repeated = self.enqueue(session)
        self.assertEqual(repeated.json()["id"], first.json()["id"])
        conflict = self.enqueue(session, message="Different input")
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "idempotency_conflict")
        busy = self.enqueue(session, key="another-run")
        self.assertEqual(busy.json()["error"]["code"], "session_busy")
        self.request("POST", f"/runs/{first.json()['id']}/cancel")
        self.assertEqual(
            self.enqueue(session, key="after-cancellation").status_code, 202
        )
        with self.db.read() as conn:
            messages = list(
                conn.execute(
                    select(t.messages).where(t.messages.c.session_id == session["id"])
                )
            )
        self.assertEqual(len(messages), 2)

    def test_member_cannot_read_other_members_conversation_or_impersonate_owner(self):
        _, agent, admin_session = self.create_resources()
        member = self.create_user()
        context = self.login(member["email"])
        self.assertEqual(
            self.request(
                "GET", f"/sessions/{admin_session['id']}", auth=context
            ).status_code,
            404,
        )
        member_session = self.request(
            "POST", "/sessions", auth=context, json={"agent_id": agent["id"]}
        ).json()
        response = self.enqueue(member_session)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "session_owner_required")
        own_list = self.request("GET", "/sessions").json()["items"]
        self.assertEqual([row["id"] for row in own_list], [admin_session["id"]])

    def test_sse_replays_durable_events_and_respects_last_event_id(self):
        _, _, session = self.create_resources()
        response = self.enqueue(session)
        run_id = response.json()["id"]
        self.request("POST", f"/runs/{run_id}/cancel")
        full = self.request("GET", f"/runs/{run_id}/events")
        self.assertEqual(full.status_code, 200)
        self.assertIn("event: run.queued", full.text)
        self.assertIn("event: run.cancelled", full.text)
        replay = self.request(
            "GET", f"/runs/{run_id}/events", headers={"Last-Event-ID": "1"}
        )
        self.assertNotIn("event: run.queued", replay.text)
        self.assertIn("event: run.cancelled", replay.text)
        malformed = self.request(
            "GET", f"/runs/{run_id}/events", headers={"Last-Event-ID": "invalid"}
        )
        self.assertEqual(malformed.status_code, 400)
        with self.db.read() as conn:
            self.assertEqual(len(list(conn.execute(select(t.runs)))), 1)

    def test_zero_balance_stays_zero_and_failed_admission_creates_no_charge(self):
        tenant = self.admin["user"]["tenant_id"]
        wallet = self.request("GET", "/billing/wallet").json()
        self.assertEqual(Decimal(wallet["available"]), 0)
        with self.assertRaises(PlatformError) as caught:
            self.platform.billing.reserve(
                tenant,
                self.admin["user"]["id"],
                "model",
                "run",
                "call",
                100,
                100,
                "1",
                "1",
            )
        self.assertEqual(caught.exception.code, "insufficient_balance")
        self.assertEqual(
            self.request("GET", "/billing/reservations").json()["items"], []
        )
        self.assertEqual(self.request("GET", "/billing/ledger").json()["items"], [])
        self.assertEqual(self.gateway.calls, [])

    def test_credit_requires_csrf_reason_and_idempotency_and_appears_in_audit(self):
        payload = {"amount": "1.23456789", "description": "Receipt TEST-42"}
        self.assertEqual(
            self.request("POST", "/billing/credits", json=payload).status_code, 400
        )
        response = self.request(
            "POST", "/billing/credits", key="receipt-42", json=payload
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.request(
                "POST", "/billing/credits", key="receipt-42", json=payload
            ).status_code,
            200,
        )
        conflict = self.request(
            "POST",
            "/billing/credits",
            key="receipt-42",
            json={**payload, "amount": "2"},
        )
        self.assertEqual(conflict.status_code, 409)
        wallet = self.request("GET", "/billing/wallet").json()
        self.assertEqual(Decimal(wallet["balance"]), Decimal(payload["amount"]))
        self.assertEqual(len(self.request("GET", "/billing/ledger").json()["items"]), 1)
        audit = self.request("GET", "/audit").json()["items"]
        financial = [row for row in audit if row["action"] == "billing.credit"]
        self.assertEqual(len(financial), 1)
        self.assertEqual(financial[0]["actor_email"], self.admin["user"]["email"])
        self.assertIn("Receipt TEST-42", financial[0]["details"])

    def test_manual_confirmation_updates_wallet_usage_and_audit_once(self):
        call_id = self.seed_unresolved_call()
        payload = {
            "action": "confirm",
            "reason": "Provider report verified #42",
            "input_tokens": 100,
            "output_tokens": 25,
        }
        response = self.request(
            "POST",
            f"/billing/reservations/{call_id}/resolve",
            key="resolution",
            json=payload,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.request(
                "POST",
                f"/billing/reservations/{call_id}/resolve",
                key="resolution",
                json=payload,
            ).status_code,
            200,
        )
        wallet = self.request("GET", "/billing/wallet").json()
        self.assertEqual(Decimal(wallet["balance"]), Decimal("9.875"))
        self.assertEqual(Decimal(wallet["reserved"]), 0)
        calls = self.request("GET", "/usage/calls").json()["items"]
        self.assertEqual(Decimal(calls[0]["cost"]), Decimal("0.125"))
        self.assertEqual(calls[0]["status"], "confirmed")
        self.assertEqual(
            Decimal(self.request("GET", "/dashboard").json()["cost"]), Decimal("0.125")
        )
        audits = self.request("GET", "/audit").json()["items"]
        self.assertEqual(
            sum(row["action"] == "billing.resolve.confirm" for row in audits), 1
        )

    def test_manually_written_off_unknown_call_is_visible_without_fabricated_refund(
        self,
    ):
        call_id = self.seed_unresolved_call()
        response = self.request(
            "POST",
            f"/billing/reservations/{call_id}/resolve",
            key="waiver",
            json={
                "action": "write_off",
                "reason": "Waived after provider investigation",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "written_off")
        self.assertEqual(
            Decimal(self.request("GET", "/billing/wallet").json()["balance"]), 10
        )
        self.assertEqual(len(self.request("GET", "/billing/ledger").json()["items"]), 1)

    def test_expired_server_session_cannot_be_reused(self):
        with self.db.transaction(f"tenant:{self.admin['user']['tenant_id']}") as conn:
            conn.execute(update(t.auth_sessions).values(expires_at=1))
        self.assertEqual(self.request("GET", "/auth/me").status_code, 401)
        self.assertEqual(
            self.request(
                "POST",
                "/billing/credits",
                key="expired",
                json={"amount": "1", "description": "Must not be accepted"},
            ).status_code,
            401,
        )

    def test_model_prices_and_agent_output_bounds_reject_invalid_configuration(self):
        for price in ("0", "-1", "NaN", "Infinity", "0.0000000000001"):
            with self.subTest(price=price):
                response = self.request(
                    "POST",
                    "/models",
                    json={
                        "name": "Invalid model",
                        "alias": "invalid-model",
                        "input_price": price,
                    },
                )
                self.assertEqual(response.status_code, 422, response.text)
        model, agent, _ = self.create_resources()
        response = self.request(
            "PATCH", f"/agents/{agent['id']}", json={"max_tokens": 2048}
        )
        self.assertEqual(response.status_code, 400, response.text)
        response = self.request(
            "PATCH", f"/models/{model['id']}", json={"context_window": 512}
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.request("PATCH", f"/models/{model['id']}", json={"active": False})
        self.assertEqual(
            self.request("POST", f"/agents/{agent['id']}/publish").status_code, 400
        )

    def test_usage_reads_financial_facts_even_when_worker_projection_is_stale(self):
        call_id = self.seed_unresolved_call()
        # Simulate a worker committing the authoritative ledger then crashing
        # before refreshing platform_calls. Display must remain reconstructable.
        self.platform.billing.settle(self.admin["user"]["tenant_id"], call_id, 100, 25)
        calls = self.request("GET", "/usage/calls").json()["items"]
        self.assertEqual(Decimal(calls[0]["cost"]), Decimal("0.125"))
        self.assertEqual(calls[0]["status"], "confirmed")
        dashboard = self.request("GET", "/dashboard").json()
        self.assertEqual(Decimal(dashboard["cost"]), Decimal("0.125"))

    def test_quota_mutation_records_the_administrator_audit(self):
        before = self.request("GET", "/audit").json()["items"]
        response = self.request(
            "PUT",
            "/quotas",
            json={
                "scope": "tenant",
                "subject_id": self.admin["user"]["tenant_id"],
                "rpm": 10,
                "tpm": 1000,
                "concurrent": 1,
                "max_budget": "20",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        after = self.request("GET", "/audit").json()["items"]
        previous_ids = {row["id"] for row in before}
        new_entries = [row for row in after if row["id"] not in previous_ids]
        self.assertEqual(len(new_entries), 1)
        self.assertEqual(new_entries[0]["actor_email"], self.admin["user"]["email"])


if __name__ == "__main__":
    unittest.main()
