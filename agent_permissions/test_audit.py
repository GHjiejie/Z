"""Integration regressions across the tool, HITL, business transaction and audit reader."""

import asyncio
import json
import secrets
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from agent_permissions import audit, test_hitl
from agent_permissions.audit_contract import Event
from agent_permissions.audit_delivery import PREFIX, AuditDelivery
from agent_permissions.audit_service import create_audit_app
from agent_permissions.config import Settings
from agent_permissions.errors import ApiError
from agent_permissions.test_support import grant_tools

OPS = "/api/v1/audit/agent-operations"
GRANTS = "/api/v1/audit/agent-grant-changes"


async def idle_relay(self):
    # The tests control when a batch is delivered and when an ACK is lost.
    await asyncio.Future()


class AuditTests(unittest.TestCase):
    application = test_hitl.HitlTests.application
    settings_for = test_hitl.HitlTests.settings_for
    start = test_hitl.HitlTests.start
    submit = test_hitl.HitlTests.submit
    final = test_hitl.HitlTests.final

    def setUp(self):
        self.enterContext(patch.object(AuditDelivery, "run", idle_relay))
        test_hitl.HitlTests.setUp(self)
        self.db = self.app.state.database
        self.relay = self.app.state.audit

    def flush(self):
        while self.client.portal.call(self.relay.flush):
            pass

    def query(self, path=OPS, **params):
        self.flush()
        response = self.client.get(path, params=params, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def detail(self, **params):
        items = self.query(**params)["items"]
        self.assertEqual(len(items), 1, items)
        return self.query(OPS + "/" + items[0]["call_id"])

    def call(self, operation, args=None, headers=None):
        return self.client.post(
            "/agent-tools/call",
            headers=self.headers if headers is None else headers,
            json={
                "name": "agent_users_" + operation.replace(".", "_"),
                "arguments": args or {},
                "revision": self.app.state.tool_registry.snapshot()["bundleDigest"],
            },
        )

    def member(self):
        response = self.client.post(
            "/api/v1/users",
            headers=self.headers,
            json={
                "username": "reader",
                "display_name": "Reader",
                "password": "member-password",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        login = self.client.post(
            "/api/v1/auth/login",
            json={"username": "reader", "password": "member-password"},
        ).json()["data"]
        return login["user"]["id"], {
            "Authorization": "Bearer " + login["session_token"]
        }

    def outbox(self):
        with self.db.transaction() as conn:
            return "\n".join(
                row[0] for row in conn.execute("SELECT payload FROM agent_audit_outbox")
            )

    def test_real_read_and_redaction_and_distinct_request_ids(self):
        secret = "private-search-term"
        response = self.call(
            "user.list",
            {"query": {"q": secret}},
            {**self.headers, "X-Agent-ID": "forged"},
        )
        self.assertTrue(response.json()["ok"], response.text)
        self.client.get("/api/v1/users", headers=self.headers)
        record = self.detail()
        self.assertEqual(record["call_id"], response.headers["X-Agent-Call-ID"])
        self.assertEqual(record["actor_user_id"], self.user_id)
        self.assertEqual(record["subject_user_id"], self.user_id)
        self.assertNotIn("agent_id", record)
        self.assertEqual(record["entrypoint"], "direct_tool_api")
        self.assertEqual(record["dispatch_state"], "received")
        self.assertEqual(record["business_outcome"], "succeeded")
        self.assertEqual(record["route_template"], "/api/v1/users")
        self.assertEqual(record["result_summary"]["returned_count"], 0)
        self.assertEqual(record["result_summary"]["page"], 1)
        self.assertTrue(all(e["schema_version"] == 1 for e in record["events"]))
        self.assertEqual(record["control_request_id"], response.headers["X-Request-ID"])
        self.assertNotEqual(record["business_request_id"], record["control_request_id"])
        self.assertEqual(record["grant_changed_by_user_id"], self.user_id)
        for stage in ("business", "tool_entry"):
            self.assertTrue(
                any(
                    c["stage"] == stage and "user.read" in c.get("matched", [])
                    for c in record["permission_checks"]
                )
            )
        for raw in (secret, self.headers["Authorization"].split()[1], "forged"):
            self.assertNotIn(raw, self.outbox() + json.dumps(record))

    def test_denial_snapshots_role_grant_changes_noop_and_read_permissions(self):
        member, headers = self.member()
        grant_tools(self.client, self.headers, member, ["user.list"])
        grant_tools(self.client, self.headers, member, ["user.list"])
        denied = self.call("user.list", headers=headers)
        self.assertEqual(denied.status_code, 403)
        record = self.detail(subject_user_id=member)
        self.assertEqual(
            (record["dispatch_state"], record["business_outcome"]),
            ("not_sent", "not_executed"),
        )
        checks = {
            c["check"]: c
            for c in record["permission_checks"]
            if c["stage"] == "tool_entry"
        }
        self.assertTrue(checks["user_grant"]["grant_enabled"])
        self.assertEqual(checks["rbac"]["roles"], ["member"])
        self.assertEqual(checks["rbac"]["missing"], ["user.read"])
        self.assertEqual(record["reason_code"], "PERMISSION_DENIED")
        rows = self.query(
            GRANTS, subject_user_id=member, operation_id="user.list", grant_version=2
        )["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor_user_id"], self.user_id)
        self.assertEqual(rows[0]["change_summary"], {"before": False, "after": True})
        self.assertTrue(rows[0]["business_audit_event_id"])
        for url in (OPS, OPS + "/" + record["call_id"], GRANTS):
            self.assertEqual(self.client.get(url, headers=headers).status_code, 403)
            self.assertEqual(self.client.get(url).status_code, 401)
        self.assertFalse(
            any(
                "audit" in x["binding"]["path"]
                for x in self.app.state.tool_registry.snapshot()["tools"]
            )
        )

    def test_unknown_tool_invalid_request_and_public_login(self):
        marker = "unknown-sensitive-marker"
        for body, status in (
            ({"name": marker, "revision": "0" * 64}, 404),
            ({"name": marker}, 422),
        ):
            result = self.client.post(
                "/agent-tools/call", headers=self.headers, json=body
            )
            self.assertEqual(result.status_code, status)
        self.assertNotIn(marker, self.outbox())
        result = self.call(
            "auth.login",
            {"body": {"username": "admin", "password": "test-password"}},
            headers={},
        )
        self.assertTrue(result.json()["ok"], result.text)
        login = self.detail(operation_id="auth.login")
        self.assertEqual(login["actor_user_id"], self.user_id)
        self.assertEqual(login["business_outcome"], "succeeded")
        self.assertEqual(
            len([e for e in login["events"] if e["event_type"] == "write.committed"]), 1
        )
        self.assertNotIn(result.json()["body"]["data"]["session_token"], self.outbox())
        failed = self.call(
            "auth.login",
            {"body": {"username": "private-login-input", "password": "never-logged"}},
            headers={},
        )
        self.assertFalse(failed.json()["ok"])
        rejected = self.detail(business_outcome="failed")
        self.assertEqual(rejected["control_http_status"], 200)
        self.assertEqual(rejected["business_http_status"], 401)
        self.assertNotIn("actor_user_id", rejected)
        self.assertNotIn("never-logged", self.outbox())
        items = self.query(limit=2)
        second = self.query(limit=2, cursor=items["next_cursor"])
        self.assertEqual(
            len({e["call_id"] for e in items["items"] + second["items"]}), 4
        )
        self.assertEqual(
            self.client.get(
                OPS, headers=self.headers, params={"cursor": "invalid"}
            ).status_code,
            422,
        )

    def test_revocation_before_confirmation_attempt_is_not_confirmation(self):
        run = self.start()
        grant_tools(
            self.client, self.headers, self.user_id, ["role.create"], enabled=False
        )
        self.assertEqual(self.submit(run).status_code, 403)
        detail = self.detail(run_id=run["id"])
        self.assertEqual(
            (detail["status"], detail["dispatch_state"]), ("waiting", "not_sent")
        )
        self.assertNotIn("confirmed_by_user_id", detail)
        self.assertEqual(detail["reason_code"], "TOOL_NOT_GRANTED")
        self.assertFalse(detail["grant_enabled"])
        self.assertTrue(
            any(
                c["stage"] == "confirmation" and c["decision"] == "denied"
                for c in detail["permission_checks"]
            )
        )
        self.assertEqual(
            self.submit(run, version=42, decision="cancel").status_code, 412
        )
        self.final(self.submit(run, decision="cancel"))
        detail = self.detail(run_id=run["id"])
        self.assertEqual(detail["status"], "cancelled")
        self.assertGreaterEqual(len(detail["attempts"]), 4)

    def test_foreign_confirmation_and_expiry_do_not_credit_owner(self):
        run = self.start()
        other, headers = self.member()
        with patch.object(self, "headers", headers):
            self.assertEqual(self.submit(run).status_code, 404)
        denied = self.detail(subject_user_id=other)
        self.assertNotIn("run_id", denied)
        self.now += timedelta(hours=2)
        self.client.get("/api/v1/chat/runs/" + run["id"], headers=self.headers)
        detail = self.detail(run_id=run["id"])
        self.assertEqual(detail["status"], "expired")
        self.assertFalse(any(e.get("actor_user_id") == other for e in detail["events"]))

    def test_password_reset_children_lost_response_and_once_only_receipt(self):
        target, _ = self.member()
        self.model.operation = "user.password.reset"
        self.model.arguments = {"path": {"user_id": target}}
        run = self.start()
        self.assertEqual(run["status"], "waiting", run)
        marker = "replacement-not-in-any-audit"
        original = httpx.AsyncClient.post

        async def lose_response(client, url, **kwargs):
            response = await original(client, url, **kwargs)
            if url == "/agent-tools/call":
                raise httpx.ReadError("discarded")
            return response

        with patch.object(httpx.AsyncClient, "post", lose_response):
            done = self.final(self.submit(run, secrets={"/body/new_password": marker}))
        self.assertEqual(done["executions"][0]["status"], "succeeded", done)
        self.final(self.submit(run, secrets={"/body/new_password": marker}))
        detail = self.detail(run_id=run["id"])
        self.assertEqual(detail["entrypoint"], "chat_runs")
        self.assertEqual(detail["agent_id"], "agent_permissions.chat")
        self.assertEqual(detail["confirmed_by_user_id"], self.user_id)
        self.assertEqual(detail["target_id"], target)
        self.assertTrue(detail["change_summary"]["password_changed"])
        self.assertEqual(detail["business_outcome"], "succeeded")
        self.assertEqual(
            sum(e["event_type"] == "write.committed" for e in detail["events"]), 1
        )
        self.assertIn("receipt.reconciled", [e["event_type"] for e in detail["events"]])
        self.assertTrue(detail["children"])
        self.assertTrue(
            all(
                c["parent_call_id"] == detail["call_id"]
                and c["purpose"] == "form_lookup"
                for c in detail["children"]
            )
        )
        self.assertGreater(
            len(self.query(run_id=run["id"], include_children=True)["items"]), 1
        )
        self.assertNotIn(marker, self.outbox() + json.dumps(detail))

    def test_local_failure_rolls_back_write_withholds_read_preserves_denial(self):
        run = self.start()
        write = audit.Call.write

        def fail(call, conn, kind, **kw):
            if (
                kind
                in {
                    "write.committed",
                    "read.succeeded",
                    "attempt.rejected",
                    "dispatch.attempted",
                }
                and kind in faults
            ):
                raise ApiError(503, "AUDIT_UNAVAILABLE", "offline")
            return write(call, conn, kind, **kw)

        faults = {"write.committed", "read.succeeded", "attempt.rejected"}
        with patch.object(audit.Call, "write", fail):
            done = self.final(self.submit(run))
            self.assertEqual(done["executions"][0]["status"], "failed")
            self.assertEqual(
                self.client.get(
                    "/api/v1/roles/new_role", headers=self.headers
                ).status_code,
                404,
            )
            self.assertEqual(self.call("user.list").status_code, 503)
            self.assertEqual(
                self.call(
                    "user.list", headers={"Authorization": "Bearer invalid"}
                ).status_code,
                401,
            )
            faults.add("dispatch.attempted")
            self.assertEqual(self.call("user.list").status_code, 503)
        detail = self.detail(run_id=run["id"])
        self.assertNotIn("write.committed", [e["event_type"] for e in detail["events"]])

    def test_remote_outage_ack_loss_and_restart_replay(self):
        from agent_permissions.api import create_app

        settings = replace(
            self.settings,
            audit_service_url="http://127.0.0.1:8011",
            audit_read_token=secrets.token_hex(24),
            audit_write_token=secrets.token_hex(24),
        )
        collector = create_audit_app(settings)
        self.enterContext(TestClient(collector))
        business = create_app(settings, clock=lambda: self.now)
        client = self.enterContext(TestClient(business))
        delivery = business.state.audit
        mode = "offline"
        transport = httpx.ASGITransport(app=collector)

        async def transmit(request):
            if mode == "offline":
                raise httpx.ConnectError("unavailable")
            response = await transport.handle_async_request(request)
            if mode == "lost" and request.method == "POST":
                raise httpx.ReadError("lost ACK")
            return response

        async def connect():
            await delivery.client.aclose()
            delivery.client = httpx.AsyncClient(
                base_url=settings.audit_service_url,
                transport=httpx.MockTransport(transmit),
            )

        client.portal.call(connect)
        result = client.post(
            "/agent-tools/call",
            headers=self.headers,
            json={
                "name": "agent_users_user_list",
                "revision": business.state.tool_registry.snapshot()["bundleDigest"],
            },
        )
        self.assertTrue(result.json()["ok"], result.text)
        self.assertEqual(client.get(OPS, headers=self.headers).status_code, 503)
        with self.assertRaises(httpx.ConnectError):
            client.portal.call(delivery.flush)
        mode = "lost"
        with (
            patch(
                "agent_permissions.audit_delivery.time.time",
                return_value=time.time() + 60,
            ),
            self.assertRaises(httpx.ReadError),
        ):
            client.portal.call(delivery.flush)
        mode = "online"
        with patch(
            "agent_permissions.audit_delivery.time.time", return_value=time.time() + 120
        ):
            self.assertGreater(client.portal.call(delivery.flush), 0)
        page = client.get(OPS, headers=self.headers).json()["data"]
        self.assertEqual(len(page["items"]), 1)
        detail = client.get(
            OPS + "/" + page["items"][0]["call_id"], headers=self.headers
        ).json()["data"]
        self.assertEqual(detail["business_outcome"], "succeeded")
        with self.db.transaction() as conn:
            source_count = conn.execute(
                "SELECT count(*) FROM agent_audit_outbox WHERE state='delivered' AND call_id=?",
                (detail["call_id"],),
            ).fetchone()[0]
        self.assertEqual(len(detail["events"]), source_count)
        self.assertEqual(len({e["event_id"] for e in detail["events"]}), source_count)

    def test_lease_expiry_and_poison_event_do_not_drop_adjacent_calls(self):
        self.call("user.list")
        now = time.time()
        lease, rows = self.relay.claim(now)
        self.assertTrue(rows)
        self.assertEqual(self.relay.claim(now + 1)[1], [])
        again, pending = self.relay.claim(now + 31)
        self.assertEqual(
            {r["event_id"] for r in rows}, {r["event_id"] for r in pending}
        )
        self.relay.acknowledge(lease, rows, {r["event_id"]: "saved" for r in rows}, now)
        self.relay.acknowledge(again, pending, {}, now - 60)
        # A collector already has a different fact with this id. Only this fact is quarantined.
        row = next(
            r for r in rows if json.loads(r["payload"])["event_type"] == "requested"
        )
        event = Event.model_validate_json(row["payload"]).model_copy(
            update={"event_type": "waiting"}
        )
        from agent_permissions.audit_contract import Batch

        self.client.portal.call(self.relay.ensure_local)
        self.relay.local.receive(
            Batch(
                schema_version=1,
                producer_id=self.settings.audit_producer_id,
                events=[event],
            )
        )
        self.flush()
        with self.db.transaction() as conn:
            quarantined = list(
                conn.execute(
                    "SELECT event_id FROM agent_audit_outbox WHERE state='quarantined'"
                )
            )
            self.assertEqual([r[0] for r in quarantined], [row["event_id"]])
            self.assertGreater(
                conn.execute(
                    "SELECT count(*) FROM agent_audit_outbox WHERE state='delivered'"
                ).fetchone()[0],
                0,
            )

    def test_accepted_confirmation_and_additional_role_assignment_permission(self):
        self.model.operation = "user.create"
        self.model.arguments = {
            "body": {
                "username": "created",
                "display_name": "Created",
                "roles": ["admin"],
            }
        }
        run = self.start()
        done = self.final(
            self.submit(run, secrets={"/body/password": "redacted-real-value"})
        )
        self.assertEqual(done["executions"][0]["status"], "succeeded", done)
        detail = self.detail(run_id=run["id"])
        self.assertEqual(detail["confirmed_by_user_id"], self.user_id)
        self.assertTrue(
            any(
                c["stage"] == "business"
                and "user.roles.assign" in c.get("required_all", [])
                for c in detail["permission_checks"]
            )
        )
        self.assertTrue(detail["target_id"].startswith("usr_"))
        self.assertNotIn("redacted-real-value", self.outbox())

    def test_agui_and_openai_calls_keep_the_real_entrypoint(self):
        self.model.operation, self.model.arguments = "user.self.read", {}
        for path, body in (
            (
                "/v1/chat/completions",
                {
                    "model": self.model.model_name,
                    "messages": [{"role": "user", "content": "查看本人"}],
                },
            ),
            (
                "/api/v1/ag-ui",
                {
                    "protocolVersion": "1.0",
                    "threadId": "audit-test",
                    "runId": "audit-run",
                    "messages": [
                        {"id": "human", "role": "user", "content": "查看本人"}
                    ],
                    "state": {},
                    "tools": [],
                    "context": [],
                    "forwardedProps": {"model": self.model.model_name},
                },
            ),
        ):
            response = self.client.post(path, headers=self.headers, json=body)
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            {op["entrypoint"] for op in self.query()["items"]},
            {"ag_ui", "chat_completions"},
        )
        self.assertTrue(
            all(op["target_id"] == self.user_id for op in self.query()["items"])
        )

    def test_grant_audit_failure_rolls_back_configuration_and_version(self):
        member, _ = self.member()
        url = f"/api/v1/users/{member}/agent-tools"
        before = self.client.get(url, headers=self.headers).json()["data"]
        original = audit.Call.write

        def unavailable(call, conn, kind, **kwargs):
            if kind == "grant.changed":
                raise ApiError(503, "AUDIT_UNAVAILABLE", "offline")
            return original(call, conn, kind, **kwargs)

        with patch.object(audit.Call, "write", unavailable):
            response = self.client.patch(
                url,
                headers=self.headers,
                json={
                    "expected_version": before["version"],
                    "catalog_revision": before["catalog_revision"],
                    "changes": [{"operation_id": "user.list", "enabled": True}],
                },
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            self.client.get(url, headers=self.headers).json()["data"], before
        )
        self.assertFalse(self.query(GRANTS, subject_user_id=member)["items"])

    def test_invalid_card_and_cancellation_of_queued_calls(self):
        self.model.second = True
        run = self.start()
        url = f"/api/v1/chat/runs/{run['id']}/interactions/{run['interaction']['id']}/resume"
        response = self.client.post(
            url, headers=self.headers, json={"unrecognized": "never-persist-input"}
        )
        self.assertEqual(response.status_code, 422)
        rows = self.query(run_id=run["id"])["items"]
        self.assertEqual(len(rows), 2)
        pending = next(r for r in rows if r["status"] == "waiting")
        record = self.query(OPS + "/" + pending["call_id"])
        self.assertTrue(
            any(
                e.get("reason_code") == "VALIDATION_ERROR"
                and e.get("interaction_action") == "resume"
                for e in record["events"]
            )
        )
        self.assertNotIn("never-persist-input", self.outbox())
        self.final(self.submit(run, decision="cancel"))
        rows = self.query(run_id=run["id"])["items"]
        self.assertEqual(
            [(r["status"], r["dispatch_state"]) for r in rows],
            [("cancelled", "not_sent")] * 2,
        )

    def test_original_business_rbac_can_reject_after_tool_allows(self):
        member, headers = self.member()
        grant_tools(self.client, self.headers, member, ["user.list"])
        with self.db.transaction(write=True) as conn:
            conn.execute(
                "INSERT INTO role_permissions(role_code,permission_code) VALUES('member','user.read')"
            )
        original = self.app.state.service.list_users

        def revoked(*args, **kwargs):
            with self.db.transaction(write=True) as conn:
                conn.execute(
                    "DELETE FROM role_permissions WHERE role_code='member' AND permission_code='user.read'"
                )
            return original(*args, **kwargs)

        with patch.object(self.app.state.service, "list_users", revoked):
            response = self.call("user.list", headers=headers)
        self.assertFalse(response.json()["ok"])
        detail = self.detail(subject_user_id=member)
        self.assertEqual(
            (detail["control_http_status"], detail["business_http_status"]), (200, 403)
        )
        self.assertEqual(detail["dispatch_state"], "received")
        checks = {
            (c["stage"], c["decision"])
            for c in detail["permission_checks"]
            if c["check"] == "rbac"
        }
        self.assertEqual(checks, {("tool_entry", "allowed"), ("business", "denied")})


def sample(kind, sequence, **fields):
    return Event(
        event_id="e" + str(sequence),
        call_id="call_test",
        attempt_id="attempt_test",
        sequence=sequence,
        event_type=kind,
        entrypoint="direct_tool_api",
        purpose="operation",
        occurred_at="2026-11-18T02:00:00Z",
        **fields,
    )


class CollectorTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings = Settings(
            database_path=folder / "business.sqlite3",
            audit_read_token=secrets.token_hex(24),
            audit_write_token=secrets.token_hex(24),
        )
        self.collector = create_audit_app(self.settings)
        self.client = self.enterContext(TestClient(self.collector))
        self.read = {"Authorization": "Bearer " + self.settings.audit_read_token}
        self.write = {"Authorization": "Bearer " + self.settings.audit_write_token}

    def batch(self, events, headers=None, producer="agent_permissions"):
        return self.client.post(
            PREFIX + "/events",
            headers=headers or self.write,
            json={"producer_id": producer, "schema_version": 1, "events": events},
        )

    def test_private_auth_out_of_order_conflict_and_invalid_rows(self):
        ok = sample("write.committed", 3).model_dump(exclude_none=True)
        wait = sample("waiting", 1).model_dump(exclude_none=True)
        invalid = {**wait, "password": "sensitive-invalid"}
        result = self.batch([ok, invalid, wait])
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(
            sorted(item["status"] for item in result.json()["acknowledgements"]),
            ["rejected", "saved", "saved"],
        )
        self.assertNotIn("sensitive-invalid", result.text)
        replay = self.batch([ok, {**wait, "event_type": "expired"}]).json()[
            "acknowledgements"
        ]
        self.assertEqual([r["status"] for r in replay], ["duplicate", "conflict"])
        self.assertEqual(self.batch([ok], self.read).status_code, 401)
        self.assertEqual(self.batch([ok], producer="spoofed").status_code, 403)
        path = PREFIX + "/operations/call_test"
        self.assertEqual(self.client.get(path, headers=self.write).status_code, 401)
        detail = self.client.get(path, headers=self.read).json()
        self.assertEqual(detail["business_outcome"], "succeeded")
        self.assertEqual([e["sequence"] for e in detail["events"]], [1, 3])

    def test_retention_leaves_unresolved_evidence(self):
        self.batch([sample("result.unknown", 1).model_dump(exclude_none=True)])
        self.collector.state.store.cleanup(datetime(2027, 9, 1, tzinfo=UTC), 90)
        self.assertEqual(
            self.client.get(
                PREFIX + "/operations/call_test", headers=self.read
            ).status_code,
            200,
        )
        self.batch([sample("write.committed", 2).model_dump(exclude_none=True)])
        self.collector.state.store.cleanup(datetime(2027, 9, 1, tzinfo=UTC), 90)
        self.assertEqual(
            self.client.get(
                PREFIX + "/operations/call_test", headers=self.read
            ).status_code,
            404,
        )

        replay = self.batch(
            [sample("write.committed", 2).model_dump(exclude_none=True)]
        )
        self.assertEqual(replay.json()["acknowledgements"][0]["status"], "duplicate")
        self.assertEqual(
            self.client.get(
                PREFIX + "/operations/call_test", headers=self.read
            ).status_code,
            404,
        )


class AuditMigrationTests(unittest.TestCase):
    def test_v5_upgrade_preserves_old_business_audit_and_is_idempotent(self):
        from agent_permissions.database import Database

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.sqlite3"
            database = Database(path)
            database.initialize()
            with database.transaction(write=True) as conn:
                conn.execute("DROP TABLE agent_audit_outbox")
                conn.execute("DROP TABLE agent_audit_call_refs")
                conn.execute(
                    "DELETE FROM role_permissions WHERE permission_code='agent.audit.read'"
                )
                conn.execute("DELETE FROM permissions WHERE code='agent.audit.read'")
                conn.execute(
                    "INSERT INTO audit_events(id,action,outcome,request_id,source,changes,created_at) VALUES('old','auth.login','success','req','ui','{}','2026-10-01')"
                )
                conn.execute("PRAGMA user_version=5")
            database.initialize()
            database.initialize()
            with database.transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 6)
                self.assertEqual(
                    conn.execute("SELECT id FROM audit_events").fetchone()[0], "old"
                )
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM agent_audit_outbox").fetchone()[
                        0
                    ],
                    0,
                )
                self.assertEqual(
                    [
                        r[0]
                        for r in conn.execute(
                            "SELECT role_code FROM role_permissions WHERE permission_code='agent.audit.read'"
                        )
                    ],
                    ["admin"],
                )
