"""Integration regressions across the tool, HITL, business transaction and audit reader."""

import secrets
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from agent_permissions.audit_contract import Event
from agent_permissions.audit_delivery import PREFIX
from agent_permissions.audit_service import create_audit_app
from agent_permissions.config import Settings


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
