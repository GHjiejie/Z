"""Real API and LangChain tool tests using only temporary SQLite databases."""

import asyncio
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from agent_permissions.api import create_app
from agent_permissions.config import Settings
from agent_permissions.models import UserCreate
from agent_permissions.operations import OPERATIONS
from agent_permissions.test_support import grant_tools
from agent_permissions.tooling import compile_tools
from agent_permissions.tools import main as tools_main
from api_tools.client import AgentToolClient
from api_tools.compiler import ContractError, digest

PASSWORD = "Offline tool tests password"


class UserToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.app = create_app(Settings(database_path=self.directory / "users.sqlite3"))
        self.client = self.enterContext(TestClient(self.app))
        self.app.state.service.bootstrap_admin(
            UserCreate(username="admin", display_name="管理员", password=PASSWORD)
        )
        login = self.client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}
        )
        self.headers = {
            "Authorization": "Bearer " + login.json()["data"]["session_token"]
        }
        grant_tools(self.client, self.headers, login.json()["data"]["user"]["id"])
        self.catalog = self.client.get("/agent-tools", headers=self.headers).json()
        self.called = set()

    def call(
        self,
        operation: str,
        arguments: dict | None = None,
        *,
        headers: dict | None = None,
        status: int = 200,
    ) -> dict:
        name = "agent_users_" + operation.replace(".", "_")
        response = self.client.post(
            "/agent-tools/call",
            headers=self.headers if headers is None else headers,
            json={
                "name": name,
                "arguments": arguments or {},
                "revision": self.catalog["bundleDigest"],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["httpStatus"], status, result)
        self.assertEqual(result["ok"], 200 <= status < 300)
        self.assertEqual(result["bundleDigest"], self.catalog["bundleDigest"])
        definition = next(
            item for item in self.catalog["tools"] if item["name"] == name
        )
        Draft202012Validator(definition["outputSchema"]).validate(result)
        self.called.add(operation)
        return result

    def test_all_21_operations_execute_through_generated_tools(self) -> None:
        self.assertEqual(len(self.catalog["tools"]), 21)
        login = self.call(
            "auth.login",
            {"body": {"username": "admin", "password": PASSWORD}},
            headers={},
        )
        self.headers = {
            "Authorization": "Bearer " + login["body"]["data"]["session_token"]
        }
        own = self.call("user.self.read")
        self.call("user.self.permissions")
        self.call(
            "user.self.update",
            {
                "header": {"If-Match": own["headers"]["etag"]},
                "body": {"display_name": "新管理员"},
            },
        )
        created = self.call(
            "user.create",
            {
                "body": {
                    "username": "member",
                    "display_name": "成员",
                    "password": PASSWORD,
                }
            },
            status=201,
        )
        user_id = created["body"]["data"]["id"]
        self.call("user.list", {"query": {"q": "member", "page_size": 1}})
        current = self.call("user.read", {"path": {"user_id": user_id}})
        for operation, body in [
            ("user.update", {"display_name": "更新成员"}),
            ("user.status.update", {"status": "disabled"}),
            ("user.roles.assign", {"roles": ["member"]}),
        ]:
            current = self.call(
                operation,
                {
                    "path": {"user_id": user_id},
                    "header": {"If-Match": current["headers"]["etag"]},
                    "body": body,
                },
            )
        self.call(
            "user.password.reset",
            {
                "path": {"user_id": user_id},
                "header": {"If-Match": current["headers"]["etag"]},
                "body": {"new_password": "replacement"},
            },
            status=204,
        )
        current = self.call("user.read", {"path": {"user_id": user_id}})
        self.call(
            "user.delete",
            {
                "path": {"user_id": user_id},
                "header": {"If-Match": current["headers"]["etag"]},
            },
            status=204,
        )
        self.call("role.list")
        self.call("role.options")
        self.call("permission.list")
        self.call(
            "role.create",
            {
                "body": {
                    "code": "viewer",
                    "display_name": "查看者",
                    "permissions": ["user.read"],
                }
            },
            status=201,
        )
        role = self.call("role.read", {"path": {"role_code": "viewer"}})
        role = self.call(
            "role.update",
            {
                "path": {"role_code": "viewer"},
                "header": {"If-Match": role["headers"]["etag"]},
                "body": {"description": "工具测试"},
            },
        )
        self.call(
            "role.delete",
            {
                "path": {"role_code": "viewer"},
                "header": {"If-Match": role["headers"]["etag"]},
            },
            status=204,
        )
        own = self.call("user.self.read")
        self.call(
            "user.self.password.change",
            {
                "header": {"If-Match": own["headers"]["etag"]},
                "body": {"current_password": PASSWORD, "new_password": PASSWORD},
            },
            status=204,
        )
        login = self.call(
            "auth.login",
            {"body": {"username": "admin", "password": PASSWORD}},
            headers={},
        )
        self.headers = {
            "Authorization": "Bearer " + login["body"]["data"]["session_token"]
        }
        self.call("auth.logout", status=204)
        self.assertEqual(
            self.called, {key for key, spec in OPERATIONS.items() if spec.expose}
        )
        self.assertEqual(
            self.client.get("/agent-tools", headers=self.headers).status_code, 401
        )
        with self.app.state.database.transaction() as connection:
            actions = {
                row[0] for row in connection.execute("SELECT action FROM audit_events")
            }
        self.assertTrue({"user.create", "role.create", "auth.logout"} <= actions)

    def test_anonymous_catalog_has_only_login_and_no_authorized_calls(self) -> None:
        response = self.client.get("/agent-tools")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["name"] for item in response.json()["tools"]],
            ["agent_users_auth_login"],
        )
        denied = self.client.post(
            "/agent-tools/call",
            json={
                "name": "agent_users_user_list",
                "arguments": {},
                "revision": self.catalog["bundleDigest"],
            },
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(
            self.client.get("/agent-tools", headers={"Authorization": ""}).status_code,
            401,
        )

    def test_live_permission_revocation_applies_to_existing_session_and_catalog(
        self,
    ) -> None:
        self.call(
            "role.create",
            {
                "body": {
                    "code": "viewer",
                    "display_name": "查看者",
                    "permissions": ["user.read"],
                }
            },
            status=201,
        )
        self.call(
            "user.create",
            {
                "body": {
                    "username": "viewer",
                    "display_name": "查看者",
                    "password": PASSWORD,
                    "roles": ["viewer"],
                }
            },
            status=201,
        )
        login = self.client.post(
            "/api/v1/auth/login", json={"username": "viewer", "password": PASSWORD}
        ).json()
        viewer = {"Authorization": "Bearer " + login["data"]["session_token"]}
        grant_tools(self.client, self.headers, login["data"]["user"]["id"])
        visible = self.client.get("/agent-tools", headers=viewer).json()
        self.assertIn(
            "agent_users_user_list", [item["name"] for item in visible["tools"]]
        )
        self.assertNotIn(
            "agent_users_role_list", [item["name"] for item in visible["tools"]]
        )
        self.call("user.list", headers=viewer)
        role = self.call("role.read", {"path": {"role_code": "viewer"}})
        self.call(
            "role.update",
            {
                "path": {"role_code": "viewer"},
                "header": {"If-Match": role["headers"]["etag"]},
                "body": {"permissions": []},
            },
        )
        denied = self.client.post(
            "/agent-tools/call",
            headers=viewer,
            json={
                "name": "agent_users_user_list",
                "arguments": {},
                "revision": visible["bundleDigest"],
            },
        )
        self.assertEqual(denied.status_code, 403)
        refreshed = self.client.get("/agent-tools", headers=viewer).json()
        self.assertEqual(refreshed["bundleDigest"], visible["bundleDigest"])
        self.assertNotIn(
            "agent_users_user_list", [item["name"] for item in refreshed["tools"]]
        )

    def test_business_errors_version_preconditions_and_argument_redaction(self) -> None:
        result = self.call(
            "user.self.update", {"body": {"display_name": "test"}}, status=428
        )
        self.assertEqual(result["body"]["error"]["code"], "PRECONDITION_REQUIRED")
        secret = "never-include-this-password"
        for arguments in (
            {"password": secret},
            {"header": {"Authorization": secret}},
            {"query": {"page_size": 101}},
            {"query": {"q": None}},
        ):
            denied = self.client.post(
                "/agent-tools/call",
                headers=self.headers,
                json={
                    "name": "agent_users_user_list",
                    "arguments": arguments,
                    "revision": self.catalog["bundleDigest"],
                },
            )
            self.assertEqual(denied.status_code, 422)
            self.assertNotIn(secret, denied.text)
        stale = self.client.post(
            "/agent-tools/call",
            headers=self.headers,
            json={
                "name": "agent_users_user_list",
                "arguments": {},
                "revision": "0" * 64,
            },
        )
        self.assertEqual(stale.json()["error"]["code"], "TOOL_VERSION_MISMATCH")
        unavailable = self.client.post(
            "/agent-tools/call",
            headers=self.headers,
            json={
                "name": "agent_users_removed",
                "arguments": {},
                "revision": self.catalog["bundleDigest"],
            },
        )
        self.assertEqual(unavailable.json()["error"]["code"], "TOOL_UNAVAILABLE")

    def test_langchain_adapter_calls_real_api_and_checks_bound_revision(self) -> None:
        token = self.headers["Authorization"].removeprefix("Bearer ")

        async def exercise() -> None:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://test",
                trust_env=False,
            ) as http:
                adapter = AgentToolClient(http, token_provider=lambda: token)
                tools = await adapter.langchain_tools()
                tool = next(
                    item for item in tools if item.name == "agent_users_user_self_read"
                )
                result = await tool.ainvoke({})
                self.assertEqual(result["body"]["data"]["username"], "admin")
                # A callable loaded in an earlier agent turn cannot silently change schema.
                bundle = copy.deepcopy(self.app.state.tool_registry.snapshot())
                bundle["sourceRevision"] = "candidate-version"
                bundle["bundleDigest"] = digest(
                    {
                        key: value
                        for key, value in bundle.items()
                        if key != "bundleDigest"
                    }
                )
                self.app.state.tool_registry.activate(bundle, expected=bundle)
                stale = await tool.ainvoke({})
                self.assertEqual(stale["error"]["code"], "TOOL_VERSION_MISMATCH")
                fresh = await adapter.langchain_tools()
                current = next(item for item in fresh if item.name == tool.name)
                self.assertEqual((await current.ainvoke({}))["httpStatus"], 200)

        asyncio.run(exercise())


class ToolBuildTests(unittest.TestCase):
    def test_breaking_release_decision_is_bound_to_both_exact_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            baseline_path = Path(directory) / "previous.json"
            decision_path = Path(directory) / "release.json"
            candidate = compile_tools(create_app())
            baseline = copy.deepcopy(candidate)
            baseline["tools"][0]["binding"]["policy"]["effect"] = "destructive"
            baseline["bundleDigest"] = digest(
                {key: value for key, value in baseline.items() if key != "bundleDigest"}
            )
            baseline_path.write_text(json.dumps(baseline))
            arguments = ["diff", "--baseline", str(baseline_path)]
            self.assertEqual(tools_main(arguments), 1)
            decision = {
                "baselineDigest": baseline["bundleDigest"],
                "candidateDigest": candidate["bundleDigest"],
                "breakingTools": [candidate["tools"][0]["definition"]["name"]],
                "reason": "Reviewed effect correction",
            }
            decision_path.write_text(json.dumps(decision))
            self.assertEqual(
                tools_main([*arguments, "--compatibility", str(decision_path)]), 0
            )
            decision["candidateDigest"] = "0" * 64
            decision_path.write_text(json.dumps(decision))
            self.assertEqual(
                tools_main([*arguments, "--compatibility", str(decision_path)]), 1
            )

    def test_build_check_diff_and_drift_detection(self) -> None:
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            output = Path(directory) / "tools.json"
            arguments = ["--output", str(output)]
            self.assertEqual(tools_main(["build", *arguments]), 0)
            self.assertEqual(tools_main(["check", *arguments]), 0)
            self.assertEqual(tools_main(["diff", "--baseline", str(output)]), 0)
            bundle = json.loads(output.read_text())
            bundle["tools"].pop()
            output.write_text(json.dumps(bundle))
            self.assertEqual(tools_main(["check", *arguments]), 1)

    def test_pinned_manifest_rejects_mismatch_before_database_initialization(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "never-created.sqlite3"
            manifest = Path(directory) / "tools.json"
            bundle = compile_tools(create_app())
            bundle["tools"][0]["definition"]["description"] = "outdated"
            bundle["bundleDigest"] = digest(
                {key: value for key, value in bundle.items() if key != "bundleDigest"}
            )
            manifest.write_text(json.dumps(bundle))
            app = create_app(Settings(database_path=database), tools_manifest=manifest)
            with self.assertRaises(ContractError), TestClient(app):
                pass
            self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
