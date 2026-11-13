"""RBAC authorization, delegation boundaries, persistence and v1 migration."""

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_permissions.api import create_app
from agent_permissions.config import Settings
from agent_permissions.database import Database
from agent_permissions.models import UserCreate
from agent_permissions.security import Passwords, token_digest

BASE = ["user.self.read", "user.self.update", "user.self.password.change"]


class RbacTests(unittest.TestCase):
    def settings_for(self, path: Path) -> Settings:
        return Settings(database_path=path)

    def setUp(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.settings = self.settings_for(Path(directory) / "rbac.sqlite3")
        self.app = create_app(self.settings)
        self.client = self.enterContext(TestClient(self.app))
        self.app.state.service.bootstrap_admin(
            UserCreate(username="admin", display_name="管理员", password="12345")
        )
        self.admin = self.login("admin")

    def login(self, username: str) -> dict:
        response = self.client.post(
            "/api/v1/auth/login", json={"username": username, "password": "12345"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": f"Bearer {response.json()['data']['session_token']}"}

    def role(self, code: str, permissions: list[str]) -> dict:
        response = self.client.post(
            "/api/v1/roles",
            headers=self.admin,
            json={"code": code, "display_name": code, "permissions": permissions},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["data"]

    def user(self, username: str, roles: list[str]) -> dict:
        response = self.client.post(
            "/api/v1/users",
            headers=self.admin,
            json={
                "username": username,
                "display_name": username,
                "password": "12345",
                "roles": roles,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["data"]

    def change_role(self, code: str, payload: dict, headers: dict | None = None):
        headers = headers or self.admin
        current = self.client.get(f"/api/v1/roles/{code}", headers=headers)
        self.assertEqual(current.status_code, 200, current.text)
        return self.client.patch(
            f"/api/v1/roles/{code}",
            headers={**headers, "If-Match": current.headers["ETag"]},
            json=payload,
        )

    def test_role_lifecycle_versions_and_in_use_protection(self) -> None:
        role = self.role("auditor", BASE + ["user.read"])
        self.assertFalse(role["built_in"])
        self.assertEqual(role["user_count"], 0)
        endpoint = "/api/v1/roles/auditor"
        for headers, expected in [
            (self.admin, 428),
            ({**self.admin, "If-Match": '"v99"'}, 412),
        ]:
            self.assertEqual(
                self.client.patch(
                    endpoint, headers=headers, json={"description": "review"}
                ).status_code,
                expected,
            )
        self.assertEqual(
            self.change_role("auditor", {"display_name": "审计员"}).status_code, 200
        )
        user = self.user("audit_user", ["auditor", "member"])
        current = self.client.get(endpoint, headers=self.admin)
        self.assertEqual(current.json()["data"]["user_count"], 1)
        self.assertEqual(
            self.client.delete(
                endpoint, headers={**self.admin, "If-Match": current.headers["ETag"]}
            ).json()["error"]["code"],
            "ROLE_IN_USE",
        )
        self.assertEqual(
            self.client.put(
                f"/api/v1/users/{user['id']}/roles",
                headers={**self.admin, "If-Match": f'"v{user["version"]}"'},
                json={"roles": ["member"]},
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.delete(
                endpoint, headers={**self.admin, "If-Match": current.headers["ETag"]}
            ).status_code,
            204,
        )
        self.assertEqual(self.client.get(endpoint, headers=self.admin).status_code, 404)
        for code in ["admin", "member"]:
            current = self.client.get(f"/api/v1/roles/{code}", headers=self.admin)
            self.assertEqual(
                self.client.delete(
                    f"/api/v1/roles/{code}",
                    headers={**self.admin, "If-Match": current.headers["ETag"]},
                ).json()["error"]["code"],
                "SYSTEM_ROLE_PROTECTED",
            )
        self.assertEqual(
            self.change_role("admin", {"permissions": []}).status_code, 409
        )

    def test_multi_role_union_and_permission_revocation_affect_existing_session(
        self,
    ) -> None:
        self.role("viewer", BASE + ["user.read"])
        self.role("editor", BASE + ["user.update"])
        user = self.user("multi_user", ["viewer", "editor"])
        identity = self.login("multi_user")
        granted = self.client.get(
            "/api/v1/users/me/permissions", headers=identity
        ).json()["data"]
        self.assertEqual(
            set(granted["permissions"]), set(BASE + ["user.read", "user.update"])
        )
        self.assertEqual(
            self.client.get("/api/v1/users", headers=identity).status_code, 200
        )
        self.assertEqual(
            self.client.get("/api/v1/role-options", headers=identity).status_code, 200
        )
        self.assertEqual(
            self.client.get("/api/v1/roles", headers=identity).status_code, 403
        )
        self.assertEqual(
            self.change_role("viewer", {"permissions": BASE}).status_code, 200
        )
        self.assertEqual(
            self.client.get("/api/v1/users", headers=identity).status_code, 403
        )
        granted = self.client.get(
            "/api/v1/users/me/permissions", headers=identity
        ).json()["data"]
        self.assertNotIn("user.read", granted["permissions"])
        self.assertIn("user.update", granted["permissions"])
        current = self.client.get(f"/api/v1/users/{user['id']}", headers=self.admin)
        self.assertEqual(
            self.client.put(
                f"/api/v1/users/{user['id']}/roles",
                headers={**self.admin, "If-Match": current.headers["ETag"]},
                json={"roles": ["member"]},
            ).status_code,
            200,
        )
        self.assertNotIn(
            "user.update",
            self.client.get("/api/v1/users/me/permissions", headers=identity).json()[
                "data"
            ]["permissions"],
        )

    def test_delegated_manager_cannot_escalate_roles_or_reset_higher_privilege_user(
        self,
    ) -> None:
        allowed = BASE + [
            "user.read",
            "user.roles.assign",
            "user.password.reset",
            "role.read",
            "role.create",
            "role.update",
            "role.delete",
        ]
        self.role("delegated", allowed)
        self.user("delegate_user", ["delegated"])
        identity = self.login("delegate_user")
        target = self.user("target_user", ["member"])
        response = self.client.put(
            f"/api/v1/users/{target['id']}/roles",
            headers={**identity, "If-Match": f'"v{target["version"]}"'},
            json={"roles": ["admin"]},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            self.change_role(
                "delegated", {"permissions": allowed + ["user.delete"]}, identity
            ).status_code,
            403,
        )
        response = self.client.post(
            "/api/v1/roles",
            headers=identity,
            json={
                "code": "escalation",
                "display_name": "越权",
                "permissions": ["user.delete"],
            },
        )
        self.assertEqual(response.status_code, 403)
        admin = self.client.get("/api/v1/users/me", headers=self.admin)
        response = self.client.put(
            f"/api/v1/users/{admin.json()['data']['id']}/password",
            headers={**identity, "If-Match": admin.headers["ETag"]},
            json={"new_password": "12345"},
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            "/api/v1/roles",
            headers=identity,
            json={"code": "permitted", "display_name": "允许委派", "permissions": BASE},
        )
        self.assertEqual(response.status_code, 201)

    def test_unknown_roles_permissions_and_direct_user_grants_are_rejected(
        self,
    ) -> None:
        for payload in [
            {
                "code": "unknown_perm",
                "display_name": "未知权限",
                "permissions": ["shell.execute"],
            },
            {"code": "INVALID", "display_name": "非法编码"},
        ]:
            self.assertEqual(
                self.client.post(
                    "/api/v1/roles", headers=self.admin, json=payload
                ).status_code,
                422,
            )
        self.assertEqual(
            self.client.post(
                "/api/v1/roles",
                headers=self.admin,
                json={"code": "admin", "display_name": "重复编码"},
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/users",
                headers=self.admin,
                json={
                    "username": "bad_user",
                    "display_name": "用户",
                    "password": "12345",
                    "roles": ["unknown"],
                },
            ).status_code,
            422,
        )
        target = self.user("normal_user", ["member"])
        self.assertEqual(
            self.client.patch(
                f"/api/v1/users/{target['id']}",
                headers={**self.admin, "If-Match": f'"v{target["version"]}"'},
                json={"permissions": ["user.delete"]},
            ).status_code,
            422,
        )
        self.role("no_access", [])
        self.user("empty_user", ["no_access"])
        identity = self.login("empty_user")
        self.assertEqual(
            self.client.get("/api/v1/users/me", headers=identity).status_code, 403
        )
        self.assertEqual(
            self.client.get("/api/v1/users", headers=identity).status_code, 403
        )

    def test_permission_revocation_serializes_with_user_update(self) -> None:
        self.role("concurrent_editor", BASE + ["user.update"])
        self.user("editing_user", ["concurrent_editor"])
        identity = self.login("editing_user")
        target = self.user("race_target", ["member"])
        role = self.client.get("/api/v1/roles/concurrent_editor", headers=self.admin)
        barrier = Barrier(2)

        def update_user():
            barrier.wait()
            return self.client.patch(
                f"/api/v1/users/{target['id']}",
                headers={**identity, "If-Match": f'"v{target["version"]}"'},
                json={"display_name": "并发修改"},
            ).status_code

        def revoke_permission():
            barrier.wait()
            return self.client.patch(
                "/api/v1/roles/concurrent_editor",
                headers={**self.admin, "If-Match": role.headers["ETag"]},
                json={"permissions": BASE},
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            update = executor.submit(update_user)
            revoke = executor.submit(revoke_permission)
            self.assertEqual(revoke.result(), 200)
            self.assertIn(update.result(), {200, 403})
        current = self.client.get(f"/api/v1/users/{target['id']}", headers=self.admin)
        response = self.client.patch(
            f"/api/v1/users/{target['id']}",
            headers={**identity, "If-Match": current.headers["ETag"]},
            json={"display_name": "撤权后的修改"},
        )
        self.assertEqual(response.status_code, 403)

    def test_role_audit_failure_rolls_back_and_catalog_persists(self) -> None:
        self.role("stable_role", BASE + ["user.read"])
        with patch.object(
            self.app.state.service,
            "_audit",
            side_effect=sqlite3.OperationalError("audit unavailable"),
        ):
            self.assertEqual(
                self.change_role("stable_role", {"permissions": BASE}).status_code, 503
            )
            self.assertEqual(
                self.client.post(
                    "/api/v1/roles",
                    headers=self.admin,
                    json={"code": "rollback_role", "display_name": "回滚"},
                ).status_code,
                503,
            )
        self.assertIn(
            "user.read",
            self.client.get("/api/v1/roles/stable_role", headers=self.admin).json()[
                "data"
            ]["permissions"],
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/roles/rollback_role", headers=self.admin
            ).status_code,
            404,
        )
        self.assertEqual(
            self.change_role("member", {"permissions": ["user.self.read"]}).status_code,
            200,
        )
        self.app.state.database.initialize()
        with TestClient(create_app(self.settings)) as client:
            self.assertEqual(
                client.get("/api/v1/roles/member", headers=self.admin).json()["data"][
                    "permissions"
                ],
                ["user.self.read"],
            )
            self.assertEqual(
                client.get("/api/v1/roles/stable_role", headers=self.admin).status_code,
                200,
            )
            catalog = client.get("/api/v1/permissions", headers=self.admin).json()[
                "data"
            ]
            self.assertEqual(len(catalog), 14)


class RbacMigrationTests(unittest.TestCase):
    def legacy_database(self, directory: str, roles: list[str]) -> tuple[Path, str]:
        path = Path(directory) / "legacy.sqlite3"
        encoded = Passwords().hash("12345")
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.executescript(
                (Path(__file__).parent / "fixtures/schema_v1.sql").read_text()
            )
            connection.execute(
                "INSERT INTO users(id,username,display_name,password_hash,roles,status,must_change_password,version,created_at,updated_at) VALUES('usr_legacy','legacy','历史用户',?,?,'active',0,5,'2026-11-12T00:00:00Z','2026-11-12T00:00:00Z')",
                (encoded, json.dumps(roles)),
            )
            connection.execute(
                "INSERT INTO sessions VALUES(?,'usr_legacy','full',4102444800,'2026-11-12T00:00:00Z',NULL)",
                (token_digest("existing-session"),),
            )
            connection.execute(
                "INSERT INTO rate_limits VALUES('existing-limit',2,4102444800)"
            )
            connection.execute(
                "INSERT INTO audit_events VALUES('evt_legacy',NULL,'usr_legacy','user.create','success',NULL,'req_legacy','local-cli','{}','2026-11-12T00:00:00Z')"
            )
        return path, encoded

    def test_v1_upgrade_preserves_users_passwords_sessions_and_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, encoded = self.legacy_database(directory, ["admin", "member"])
            database = Database(path)
            database.initialize()
            database.initialize()
            with database.transaction() as connection:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 2
                )
                row = connection.execute("SELECT * FROM users").fetchone()
                self.assertEqual(row["password_hash"], encoded)
                self.assertEqual(row["version"], 5)
                self.assertNotIn("roles", row.keys())
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM user_roles").fetchone()[0],
                    2,
                )
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM sessions").fetchone()[0], 1
                )
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM audit_events").fetchone()[
                        0
                    ],
                    1,
                )
                self.assertEqual(
                    connection.execute("SELECT attempts FROM rate_limits").fetchone()[
                        0
                    ],
                    2,
                )
                self.assertEqual(
                    connection.execute("PRAGMA foreign_key_check").fetchall(), []
                )
            with TestClient(create_app(Settings(database_path=path))) as client:
                response = client.get(
                    "/api/v1/users/me/permissions",
                    headers={"Authorization": "Bearer existing-session"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn("role.update", response.json()["data"]["permissions"])
                self.assertEqual(
                    client.post(
                        "/api/v1/auth/login",
                        json={"username": "legacy", "password": "12345"},
                    ).status_code,
                    200,
                )

    def test_invalid_legacy_role_rolls_back_entire_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, encoded = self.legacy_database(directory, ["unknown"])
            with self.assertRaises(sqlite3.IntegrityError):
                Database(path).initialize()
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 1
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT roles,password_hash FROM users"
                    ).fetchone(),
                    ('["unknown"]', encoded),
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT name FROM sqlite_master WHERE name='user_roles'"
                    ).fetchone()
                )


if __name__ == "__main__":
    unittest.main()
