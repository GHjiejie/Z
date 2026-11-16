"""User delegation is independent of roles and checked on every tool call."""

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from agent_permissions import test_rbac
from agent_permissions.database import AGENT_GRANTS_SCHEMA, Database
from agent_permissions.operations import OperationSpec
from agent_permissions.test_support import grant_tools
from agent_permissions.tooling import activate_tools
from api_tools.compiler import digest


class UserAgentGrantTests(unittest.TestCase):
    setUp = test_rbac.RbacTests.setUp
    settings_for = test_rbac.RbacTests.settings_for
    login = test_rbac.RbacTests.login
    role = test_rbac.RbacTests.role
    user = test_rbac.RbacTests.user
    change_role = test_rbac.RbacTests.change_role

    def view(self, user_id: str, headers: dict | None = None) -> dict:
        response = self.client.get(
            f"/api/v1/users/{user_id}/agent-tools", headers=headers or self.admin
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def save(
        self,
        user_id: str,
        changes: dict[str, bool],
        *,
        view: dict | None = None,
        headers: dict | None = None,
    ):
        current = view or self.view(user_id)
        return self.client.patch(
            f"/api/v1/users/{user_id}/agent-tools",
            headers=headers or self.admin,
            json={
                "expected_version": current["version"],
                "catalog_revision": current["catalog_revision"],
                "changes": [
                    {"operation_id": key, "enabled": value}
                    for key, value in changes.items()
                ],
            },
        )

    def call(
        self,
        operation: str,
        headers: dict,
        *,
        arguments: dict | None = None,
        revision: str | None = None,
    ):
        return self.client.post(
            "/agent-tools/call",
            headers=headers,
            json={
                "name": "agent_users_" + operation.replace(".", "_"),
                "arguments": arguments or {},
                "revision": revision
                or self.app.state.tool_registry.snapshot()["bundleDigest"],
            },
        )

    @staticmethod
    def item(view: dict, operation: str) -> dict:
        return next(item for item in view["items"] if item["operation_id"] == operation)

    def test_default_deny_independent_users_and_direct_api_unchanged(self):
        self.role("viewer", ["user.read"])
        first = self.user("first", ["viewer"])
        second = self.user("second", ["viewer"])
        first_auth, second_auth = self.login("first"), self.login("second")
        self.assertFalse(
            any(item["enabled"] for item in self.view(first["id"])["items"])
        )
        self.assertEqual(
            self.client.get("/api/v1/users", headers=first_auth).status_code, 200
        )
        self.assertEqual(
            self.call("user.list", first_auth).json()["error"]["code"],
            "TOOL_NOT_GRANTED",
        )
        self.assertEqual(self.save(first["id"], {"user.list": True}).status_code, 200)
        self.assertTrue(self.call("user.list", first_auth).json()["ok"])
        self.assertEqual(
            self.call("user.read", first_auth).json()["error"]["code"],
            "TOOL_NOT_GRANTED",
        )
        self.assertFalse(self.item(self.view(second["id"]), "user.list")["enabled"])
        self.assertEqual(self.call("user.list", second_auth).status_code, 403)
        admin_id = self.client.get("/api/v1/users/me", headers=self.admin).json()[
            "data"
        ]["id"]
        self.assertFalse(any(item["enabled"] for item in self.view(admin_id)["items"]))

    def test_missing_business_permission_can_be_granted_then_role_restored(self):
        self.role("viewer", ["user.self.read"])
        user = self.user("viewer_user", ["viewer"])
        auth = self.login("viewer_user")
        saved = self.save(user["id"], {"user.list": True, "role.options": True}).json()[
            "data"
        ]
        self.assertEqual(
            self.item(saved, "user.list")["state"], "missing_business_permission"
        )
        self.assertEqual(
            self.call("user.list", auth).json()["error"]["code"], "PERMISSION_DENIED"
        )
        self.assertNotIn(
            "agent_users_user_list",
            str(self.client.get("/agent-tools", headers=auth).json()["tools"]),
        )
        self.change_role("viewer", {"permissions": ["user.self.read", "user.read"]})
        self.assertTrue(self.call("user.list", auth).json()["ok"])
        # anyPermissions: user.read alone is sufficient for role.options.
        self.assertTrue(self.call("role.options", auth).json()["ok"])
        self.change_role("viewer", {"permissions": []})
        revoked = self.view(user["id"])
        self.assertEqual(revoked["version"], saved["version"])
        self.assertTrue(self.item(revoked, "user.list")["enabled"])
        self.assertEqual(self.call("role.options", auth).status_code, 403)
        self.change_role("viewer", {"permissions": ["user.read"]})
        self.assertTrue(self.call("user.list", auth).json()["ok"])

    def test_user_role_changes_preserve_settings_and_union_permissions(self):
        self.role("viewer", ["user.read"])
        self.role("empty", [])
        user = self.user("union_user", ["viewer", "empty"])
        auth = self.login("union_user")
        saved = grant_tools(
            self.client, self.admin, user["id"], ["user.list", "auth.logout"]
        )
        for roles, status in [(["empty"], 403), (["empty", "viewer"], 200)]:
            current = self.client.get(f"/api/v1/users/{user['id']}", headers=self.admin)
            response = self.client.put(
                f"/api/v1/users/{user['id']}/roles",
                headers={**self.admin, "If-Match": current.headers["etag"]},
                json={"roles": roles},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.call("user.list", auth).status_code, status)
            self.assertEqual(self.view(user["id"])["version"], saved["version"])
        self.assertEqual(self.call("auth.logout", auth).json()["httpStatus"], 204)
        self.assertEqual(self.call("user.list", auth).status_code, 401)

    def test_revoke_blocks_old_catalog_and_noop_does_not_bump_versions(self):
        user = self.user("viewer_user", ["admin"])
        auth = self.login("viewer_user")
        saved = grant_tools(self.client, self.admin, user["id"], ["user.list"])
        catalog = self.client.get("/agent-tools", headers=auth)
        self.assertEqual(catalog.headers["Cache-Control"], "private, no-store")
        self.assertEqual(
            self.save(user["id"], {"user.list": True}).json()["data"]["version"],
            saved["version"],
        )
        self.assertEqual(self.save(user["id"], {"user.list": False}).status_code, 200)
        response = self.call("user.list", auth, revision=catalog.json()["bundleDigest"])
        self.assertEqual(response.json()["error"]["code"], "TOOL_NOT_GRANTED")
        with self.app.state.database.transaction() as connection:
            event = connection.execute(
                "SELECT changes FROM audit_events WHERE action='agent.tool.call' AND reason_code='TOOL_NOT_GRANTED'"
            ).fetchone()
            self.assertEqual(json.loads(event["changes"])["operation_id"], "user.list")
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM audit_events WHERE action='user.agent_tools.update'"
                ).fetchone()[0],
                2,
            )

    def test_manager_scope_readonly_and_control_routes_are_excluded(self):
        self.role("manager", ["user.read", "user.agent_tools.manage"])
        manager = self.user("manager_user", ["manager"])
        auth = self.login("manager_user")
        target = self.user("plain_user", ["member"])
        self.assertFalse(self.view(target["id"], auth)["can_manage"])
        self.assertEqual(
            self.save(target["id"], {"user.list": True}, headers=auth).status_code, 403
        )
        # A lower-privilege target can be granted tools it cannot currently execute.
        empty = self.user("empty_user", ["manager"])
        self.assertEqual(
            self.save(empty["id"], {"user.delete": True}, headers=auth).status_code, 200
        )
        self.role("reader", ["user.read"])
        reader = self.user("reader_user", ["reader"])
        readonly = self.login("reader_user")
        self.assertFalse(self.view(manager["id"], readonly)["can_manage"])
        self.assertEqual(
            self.save(reader["id"], {"user.list": True}, headers=readonly).status_code,
            403,
        )
        snapshot = self.app.state.tool_registry.snapshot()
        self.assertEqual(len(snapshot["tools"]), 21)
        self.assertFalse(
            any(
                tool["binding"]["operationId"].startswith("user.agent_tools.")
                for tool in snapshot["tools"]
            )
        )
        self.assertEqual(self.call("user.agent_tools.update", auth).status_code, 404)

    def test_validation_conflicts_and_atomic_batch(self):
        user = self.user("batch_user", ["member"])
        old = self.view(user["id"])
        self.assertEqual(self.save(user["id"], {"user.list": True}).status_code, 200)
        self.assertEqual(
            self.save(user["id"], {"user.delete": True}, view=old).status_code, 412
        )
        current = self.view(user["id"])
        self.assertEqual(
            self.save(user["id"], {"user.delete": True, "unknown": True}).status_code,
            422,
        )
        self.assertFalse(self.item(self.view(user["id"]), "user.delete")["enabled"])
        self.assertEqual(self.save(user["id"], {"auth.login": True}).status_code, 422)
        bad = {**current, "catalog_revision": "0" * 64}
        self.assertEqual(
            self.save(user["id"], {"user.delete": True}, view=bad).status_code, 409
        )
        base = {
            "expected_version": current["version"],
            "catalog_revision": current["catalog_revision"],
        }
        for changes in [
            [],
            [{"operation_id": "user.list", "enabled": "true"}],
            [{"operation_id": "user.list", "enabled": True}] * 2,
            [{"operation_id": "user.list", "enabled": True, "user_id": user["id"]}],
        ]:
            response = self.client.patch(
                f"/api/v1/users/{user['id']}/agent-tools",
                headers=self.admin,
                json={**base, "changes": changes},
            )
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.view(user["id"])["version"], current["version"])

    def test_concurrent_configuration_writes_conflict(self):
        user = self.user("racing_user", ["member"])
        view = self.view(user["id"])
        barrier = Barrier(2)

        def save(operation):
            barrier.wait()
            return self.save(user["id"], {operation: True}, view=view).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(save, ["user.list", "user.delete"]))
        self.assertEqual(sorted(result), [200, 412])

    def test_business_transaction_rechecks_rbac_after_tool_precheck(self):
        self.role("viewer", ["user.read"])
        user = self.user("timing_user", ["viewer"])
        auth = self.login("timing_user")
        grant_tools(self.client, self.admin, user["id"], ["user.list"])
        original = self.app.state.service.list_users

        def revoke(*args, **kwargs):
            self.change_role("viewer", {"permissions": []})
            return original(*args, **kwargs)

        with patch.object(self.app.state.service, "list_users", side_effect=revoke):
            response = self.call("user.list", auth)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(response.json()["httpStatus"], 403)

    def test_new_tool_defaults_off_and_removed_grant_survives_partial_save(self):
        user = self.user("catalog_user", ["admin"])
        grant_tools(self.client, self.admin, user["id"], ["user.list"])
        auth = self.login("catalog_user")

        @self.app.get(
            "/api/v1/example",
            operation_id="example.read",
            description="查看示例",
            openapi_extra={
                "x-agent": OperationSpec("查看示例", ("user.read",)).metadata()
            },
        )
        def example() -> dict:
            return {}

        activate_tools(self.app)
        registry = self.app.state.tool_registry
        candidate = registry.snapshot()
        candidate["tools"] = [
            tool
            for tool in candidate["tools"]
            if tool["binding"]["operationId"] != "user.list"
        ]
        candidate["bundleDigest"] = digest(
            {key: value for key, value in candidate.items() if key != "bundleDigest"}
        )
        registry.activate(candidate, expected=candidate)
        current = self.view(user["id"])
        self.assertFalse(self.item(current, "example.read")["enabled"])
        self.assertEqual(self.item(current, "user.list")["state"], "unavailable")
        self.assertTrue(self.item(current, "user.list")["enabled"])
        self.assertEqual(self.save(user["id"], {"example.read": True}).status_code, 200)
        self.assertTrue(self.item(self.view(user["id"]), "user.list")["enabled"])
        self.assertEqual(self.call("user.list", auth).status_code, 404)
        self.assertEqual(self.save(user["id"], {"user.list": True}).status_code, 422)
        self.assertEqual(self.save(user["id"], {"user.list": False}).status_code, 200)

    def test_disabled_account_preserves_grants_and_independent_profile_version(self):
        user = self.user("disabled_user", ["member"])
        saved = grant_tools(self.client, self.admin, user["id"], ["user.self.read"])
        auth = self.login("disabled_user")
        current = self.client.get(f"/api/v1/users/{user['id']}", headers=self.admin)
        profile_version = current.json()["data"]["version"]
        self.assertEqual(profile_version, user["version"] + 1)  # login only
        for status in ["disabled", "active"]:
            response = self.client.put(
                f"/api/v1/users/{user['id']}/status",
                headers={**self.admin, "If-Match": current.headers["etag"]},
                json={"status": status},
            )
            self.assertEqual(response.status_code, 200)
            current = response
            view = self.view(user["id"])
            self.assertEqual(view["version"], saved["version"])
            self.assertTrue(self.item(view, "user.self.read")["enabled"])
            self.assertEqual(self.call("user.self.read", auth).status_code, 401)
        self.assertTrue(
            self.call("user.self.read", self.login("disabled_user")).json()["ok"]
        )


class AgentGrantMigrationTests(unittest.TestCase):
    setUp = test_rbac.RbacTests.setUp
    settings_for = test_rbac.RbacTests.settings_for
    login = test_rbac.RbacTests.login

    def downgrade(self):
        with self.app.state.database.transaction(write=True) as connection:
            connection.execute("DROP TABLE user_agent_tool_grants")
            connection.execute("ALTER TABLE users DROP COLUMN agent_tools_version")
            connection.execute(
                "DELETE FROM role_permissions WHERE permission_code='user.agent_tools.manage'"
            )
            connection.execute(
                "DELETE FROM permissions WHERE code='user.agent_tools.manage'"
            )
            connection.execute("PRAGMA user_version=2")

    def test_schema_two_upgrade_preserves_identity_and_defaults_off(self):
        before = self.client.get("/api/v1/users/me", headers=self.admin).json()["data"]
        self.downgrade()
        self.app.state.database.initialize()
        self.app.state.database.initialize()
        after = self.client.get("/api/v1/users/me", headers=self.admin).json()["data"]
        self.assertEqual(before, after)
        view = self.client.get(
            f"/api/v1/users/{before['id']}/agent-tools", headers=self.admin
        ).json()["data"]
        self.assertEqual(view["version"], 1)
        self.assertFalse(any(item["enabled"] for item in view["items"]))
        self.assertTrue(view["can_manage"])

    def test_schema_upgrade_rolls_back_column_catalog_and_version_on_failure(self):
        self.downgrade()
        original = Database._execute_schema

        def fail(connection, schema):
            original(connection, schema)
            if schema == AGENT_GRANTS_SCHEMA:
                raise RuntimeError("migration failure")

        with (
            patch.object(Database, "_execute_schema", side_effect=fail),
            self.assertRaisesRegex(RuntimeError, "migration failure"),
        ):
            self.app.state.database.initialize()
        with self.app.state.database.transaction() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertNotIn(
                "agent_tools_version",
                {row["name"] for row in connection.execute("PRAGMA table_info(users)")},
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM permissions WHERE code='user.agent_tools.manage'"
                ).fetchone()
            )
