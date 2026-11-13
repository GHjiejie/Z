"""Offline contract tests against real HTTP routes and temporary SQLite files."""

import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_permissions.__main__ import main as cli_main
from agent_permissions.api import create_app
from agent_permissions.config import Settings
from agent_permissions.errors import ApiError
from agent_permissions.models import UserCreate
from agent_permissions.security import token_digest
from agent_permissions.service import Authority, RequestInfo

INITIAL_PASSWORD = "Temporary user passphrase 2026!"
FINAL_PASSWORD = "Permanent user passphrase 2026!"
RESET_PASSWORD = "Replacement user passphrase 2026!"
USER_FIELDS = {
    "id",
    "username",
    "display_name",
    "email",
    "roles",
    "status",
    "must_change_password",
    "version",
    "last_login_at",
    "created_at",
    "updated_at",
}


class TestClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 12, 8, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class UserApiTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.settings = Settings(database_path=Path(directory) / "users.sqlite3")
        self.clock = TestClock()
        self.app = create_app(self.settings, clock=self.clock)
        self.client = self.enterContext(TestClient(self.app))
        self.service = self.app.state.service
        self.database = self.app.state.database
        self.service.bootstrap_admin(
            UserCreate(
                username="admin", display_name="管理员", password=INITIAL_PASSWORD
            )
        )
        self.admin = self.activate("admin")
        self.serial = 0

    def assert_error(self, response, status: int, code: str) -> None:
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"]["code"], code)
        self.assertIsInstance(response.json()["error"]["details"], list)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(
            response.headers["X-Request-ID"], response.json()["request_id"]
        )
        if status == 401:
            self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")

    def login(self, username: str, password: str = FINAL_PASSWORD) -> dict:
        response = self.client.post(
            "/api/v1/auth/login", json={"username": username, "password": password}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    @staticmethod
    def headers(login: dict, version: int | None = None) -> dict:
        headers = {"Authorization": f"Bearer {login['session_token']}"}
        if version is not None:
            headers["If-Match"] = f'"v{version}"'
        return headers

    def activate(self, username: str) -> dict:
        restricted = self.login(username, INITIAL_PASSWORD)
        response = self.client.put(
            "/api/v1/users/me/password",
            headers=self.headers(restricted, restricted["user"]["version"]),
            json={
                "current_password": INITIAL_PASSWORD,
                "new_password": FINAL_PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(response.content, b"")
        return self.login(username)

    def create_user(self, *, roles=None, **overrides) -> dict:
        self.serial += 1
        payload = {
            "username": f"member{self.serial}",
            "display_name": f"用户{self.serial}",
            "password": INITIAL_PASSWORD,
            **overrides,
        }
        if roles is not None:
            payload["roles"] = roles
        response = self.client.post(
            "/api/v1/users", headers=self.headers(self.admin), json=payload
        )
        self.assertEqual(response.status_code, 201, response.text)
        user = response.json()["data"]
        self.assertEqual(set(user), USER_FIELDS)
        self.assertEqual(response.headers["Location"], f"/api/v1/users/{user['id']}")
        self.assertEqual(response.headers["ETag"], '"v1"')
        return user

    def get_user(self, user_id: str) -> dict:
        response = self.client.get(
            f"/api/v1/users/{user_id}", headers=self.headers(self.admin)
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def test_create_normalize_query_and_no_credential_leak(self) -> None:
        user = self.create_user(
            username="  ZhangSan  ",
            display_name="  张三  ",
            email=" zhangsan@example.com ",
            roles=["member", "member"],
        )
        self.assertEqual(user["username"], "zhangsan")
        self.assertEqual(user["display_name"], "张三")
        self.assertEqual(user["email"], "zhangsan@example.com")
        self.assertEqual(user["roles"], ["member"])
        self.assertFalse(user["must_change_password"])
        self.assertIsNone(user["last_login_at"])
        self.assertEqual(self.get_user(user["id"]), user)
        response = self.client.get("/api/v1/users", headers=self.headers(self.admin))
        self.assertEqual(response.json()["data"]["total"], 2)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE id=?", (user["id"],)
            ).fetchone()
            self.assertTrue(row["password_hash"].startswith("$argon2id$"))
            self.assertNotIn(row["password_hash"], response.text)
        self.assertNotIn(INITIAL_PASSWORD, response.text)

    def test_duplicate_username_and_reserved_deleted_username(self) -> None:
        user = self.create_user(username="zhangsan")
        body = {
            "username": " ZHANGSAN ",
            "display_name": "另一个人",
            "password": INITIAL_PASSWORD,
        }
        for deleted in (False, True):
            with self.subTest(deleted=deleted):
                if deleted:
                    response = self.client.delete(
                        f"/api/v1/users/{user['id']}",
                        headers=self.headers(self.admin, user["version"]),
                    )
                    self.assertEqual(response.status_code, 204)
                self.assert_error(
                    self.client.post(
                        "/api/v1/users", headers=self.headers(self.admin), json=body
                    ),
                    409,
                    "USERNAME_ALREADY_EXISTS",
                )

    def test_first_login_has_full_access_and_password_revokes_every_session(
        self,
    ) -> None:
        user = self.create_user(roles=["admin"])
        first = self.login(user["username"], INITIAL_PASSWORD)
        second = self.login(user["username"], INITIAL_PASSWORD)
        self.assertEqual(first["auth_scope"], "full")
        self.assertEqual(first["expires_in"], 28_800)
        self.assertFalse(first["user"]["must_change_password"])
        for method, path, body in (
            ("GET", "/api/v1/users", None),
            ("GET", "/api/v1/users/me/permissions", None),
            ("PATCH", "/api/v1/users/me", {"display_name": "尝试修改"}),
        ):
            with self.subTest(path=path):
                response = self.client.request(
                    method,
                    path,
                    headers=self.headers(first, second["user"]["version"]),
                    json=body,
                )
                self.assertEqual(response.status_code, 200, response.text)
        me = self.client.get("/api/v1/users/me", headers=self.headers(first))
        self.assertEqual(me.status_code, 200)
        response = self.client.put(
            "/api/v1/users/me/password",
            headers=self.headers(first, me.json()["data"]["version"]),
            json={"current_password": INITIAL_PASSWORD, "new_password": FINAL_PASSWORD},
        )
        self.assertEqual(response.status_code, 204)
        for login in (first, second):
            self.assert_error(
                self.client.get("/api/v1/users/me", headers=self.headers(login)),
                401,
                "UNAUTHENTICATED",
            )
        full = self.login(user["username"])
        self.assertEqual(full["auth_scope"], "full")
        self.assertEqual(full["expires_in"], 28_800)
        self.assertFalse(full["user"]["must_change_password"])

    def test_legacy_password_change_flag_does_not_restrict_login(self) -> None:
        with self.database.transaction(write=True) as connection:
            connection.execute(
                "UPDATE users SET must_change_password=1 WHERE id=?",
                (self.admin["user"]["id"],),
            )
        login = self.login("admin")
        self.assertEqual(login["auth_scope"], "full")
        self.assertFalse(login["user"]["must_change_password"])
        self.assertFalse(self.get_user(login["user"]["id"])["must_change_password"])
        response = self.client.get("/api/v1/users", headers=self.headers(login))
        self.assertEqual(response.status_code, 200, response.text)

    def test_member_cannot_manage_users_or_roles_even_for_self(self) -> None:
        user = self.create_user()
        member = self.activate(user["username"])
        for method, path, body in (
            ("GET", "/api/v1/users", None),
            ("GET", f"/api/v1/users/{user['id']}", None),
            ("GET", "/api/v1/roles", None),
            ("DELETE", f"/api/v1/users/{self.admin['user']['id']}", None),
            ("PUT", f"/api/v1/users/{user['id']}/roles", {"roles": ["admin"]}),
            (
                "POST",
                "/api/v1/users",
                {
                    "username": "attacker",
                    "display_name": "无权限",
                    "password": INITIAL_PASSWORD,
                },
            ),
        ):
            with self.subTest(method=method, path=path):
                self.assert_error(
                    self.client.request(
                        method, path, headers=self.headers(member, 1), json=body
                    ),
                    403,
                    "PERMISSION_DENIED",
                )

    def test_update_self_and_admin_profile_null_and_noop(self) -> None:
        user = self.create_user(email="before@example.com")
        member = self.activate(user["username"])
        version = member["user"]["version"]
        response = self.client.patch(
            "/api/v1/users/me",
            headers=self.headers(member, version),
            json={"display_name": "  新名称  ", "email": None},
        )
        self.assertEqual(response.status_code, 200)
        after = response.json()["data"]
        self.assertEqual(after["display_name"], "新名称")
        self.assertIsNone(after["email"])
        self.assertEqual(after["version"], version + 1)
        response = self.client.patch(
            f"/api/v1/users/{user['id']}",
            headers=self.headers(self.admin, after["version"]),
            json={"display_name": "新名称"},
        )
        self.assertEqual(response.json()["data"]["version"], after["version"])

    def test_strict_body_query_and_field_validation(self) -> None:
        user = self.create_user()
        for body in (
            {},
            {"display_name": None},
            {"display_name": " "},
            {"email": "not-an-email"},
            {"roles": ["admin"]},
            {"status": "disabled"},
            {"password": "Never echo this secret 2026!"},
            {"username": "renamed"},
        ):
            with self.subTest(body=tuple(body)):
                response = self.client.patch(
                    f"/api/v1/users/{user['id']}",
                    headers=self.headers(self.admin, user["version"]),
                    json=body,
                )
                self.assert_error(response, 422, "VALIDATION_ERROR")
                self.assertNotIn("Never echo this secret", response.text)
        for path in (
            "/api/v1/users?tenant_id=another",
            "/api/v1/users?page=1&page=2",
            "/api/v1/users?page_size=101",
            "/api/v1/users?sort_by=password_hash",
            "/api/v1/users/me?user_id=another",
            "/api/v1/roles?extra=1",
            "/api/v1/users?role=unknown",
        ):
            with self.subTest(path=path):
                self.assert_error(
                    self.client.get(path, headers=self.headers(self.admin)),
                    422,
                    "VALIDATION_ERROR",
                )
        response = self.client.request(
            "GET", "/api/v1/users/me", headers=self.headers(self.admin), json={}
        )
        self.assert_error(response, 422, "VALIDATION_ERROR")
        self.assertEqual(self.get_user(user["id"])["version"], user["version"])

    def test_roles_and_create_validation(self) -> None:
        for overrides in (
            {"username": "a"},
            {"username": "汉字用户"},
            {"username": 12345},
            {"display_name": 12345},
            {"roles": []},
            {"roles": ["owner"]},
            {"roles": None},
            {"status": "active"},
            {"must_change_password": False},
        ):
            body = {
                "username": "valid-user",
                "display_name": "用户",
                "password": INITIAL_PASSWORD,
                **overrides,
            }
            with self.subTest(fields=tuple(overrides)):
                self.assert_error(
                    self.client.post(
                        "/api/v1/users", headers=self.headers(self.admin), json=body
                    ),
                    422,
                    "VALIDATION_ERROR",
                )

    def test_version_missing_invalid_stale_and_not_found(self) -> None:
        user = self.create_user()
        path = f"/api/v1/users/{user['id']}"
        for header, status, code in (
            (None, 428, "PRECONDITION_REQUIRED"),
            ("v1", 422, "VALIDATION_ERROR"),
            ('W/"v1"', 422, "VALIDATION_ERROR"),
            ("*", 422, "VALIDATION_ERROR"),
            ('"v99"', 412, "VERSION_CONFLICT"),
        ):
            headers = self.headers(self.admin)
            if header is not None:
                headers["If-Match"] = header
            self.assert_error(
                self.client.patch(path, headers=headers, json={"display_name": "修改"}),
                status,
                code,
            )
        response = self.client.patch(
            path, headers=self.headers(self.admin, 1), json={"display_name": "成功修改"}
        )
        self.assertEqual(response.status_code, 200)
        self.assert_error(
            self.client.delete(path, headers=self.headers(self.admin, 1)),
            412,
            "VERSION_CONFLICT",
        )
        self.assert_error(
            self.client.delete(
                "/api/v1/users/absent", headers=self.headers(self.admin)
            ),
            404,
            "USER_NOT_FOUND",
        )

    def test_disable_enable_and_soft_delete_revoke_sessions(self) -> None:
        user = self.create_user()
        member = self.activate(user["username"])
        version = member["user"]["version"]
        response = self.client.put(
            f"/api/v1/users/{user['id']}/status",
            headers=self.headers(self.admin, version),
            json={"status": "disabled"},
        )
        self.assertEqual(response.status_code, 200)
        disabled = response.json()["data"]
        self.assert_error(
            self.client.get("/api/v1/users/me", headers=self.headers(member)),
            401,
            "UNAUTHENTICATED",
        )
        self.assert_error(
            self.client.post(
                "/api/v1/auth/login",
                json={
                    "username": user["username"],
                    "password": FINAL_PASSWORD,
                },
            ),
            401,
            "INVALID_CREDENTIALS",
        )
        response = self.client.put(
            f"/api/v1/users/{user['id']}/status",
            headers=self.headers(self.admin, disabled["version"]),
            json={"status": "active"},
        )
        self.assertEqual(response.status_code, 200)
        self.assert_error(
            self.client.get("/api/v1/users/me", headers=self.headers(member)),
            401,
            "UNAUTHENTICATED",
        )
        member = self.login(user["username"])
        response = self.client.delete(
            f"/api/v1/users/{user['id']}",
            headers=self.headers(self.admin, member["user"]["version"]),
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assert_error(
            self.client.get("/api/v1/users/me", headers=self.headers(member)),
            401,
            "UNAUTHENTICATED",
        )
        for method in ("get", "delete"):
            self.assert_error(
                getattr(self.client, method)(
                    f"/api/v1/users/{user['id']}", headers=self.headers(self.admin, 1)
                ),
                404,
                "USER_NOT_FOUND",
            )
        page = self.client.get(
            "/api/v1/users", headers=self.headers(self.admin)
        ).json()["data"]
        self.assertNotIn(user["id"], [item["id"] for item in page["items"]])

    def test_admin_reset_preserves_status_and_allows_direct_login(self) -> None:
        user = self.create_user()
        member = self.activate(user["username"])
        version = member["user"]["version"]
        response = self.client.put(
            f"/api/v1/users/{user['id']}/password",
            headers=self.headers(self.admin, version),
            json={"new_password": RESET_PASSWORD},
        )
        self.assertEqual(response.status_code, 204)
        self.assert_error(
            self.client.get("/api/v1/users/me", headers=self.headers(member)),
            401,
            "UNAUTHENTICATED",
        )
        reset_login = self.login(user["username"], RESET_PASSWORD)
        self.assertEqual(reset_login["auth_scope"], "full")
        self.assertFalse(reset_login["user"]["must_change_password"])
        response = self.client.put(
            f"/api/v1/users/{user['id']}/status",
            headers=self.headers(self.admin, reset_login["user"]["version"]),
            json={"status": "disabled"},
        )
        version = response.json()["data"]["version"]
        response = self.client.put(
            f"/api/v1/users/{user['id']}/password",
            headers=self.headers(self.admin, version),
            json={"new_password": INITIAL_PASSWORD},
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.get_user(user["id"])["status"], "disabled")

    def test_password_wrong_and_invalid_input_errors_do_not_leak_secrets(self) -> None:
        user = self.create_user()
        restricted = self.login(user["username"], INITIAL_PASSWORD)
        version = restricted["user"]["version"]
        for old, new, status, code in (
            ("wrong", FINAL_PASSWORD, 400, "CURRENT_PASSWORD_INCORRECT"),
            (INITIAL_PASSWORD, "", 422, "VALIDATION_ERROR"),
            (INITIAL_PASSWORD, None, 422, "VALIDATION_ERROR"),
            (INITIAL_PASSWORD, 12345, 422, "VALIDATION_ERROR"),
        ):
            response = self.client.put(
                "/api/v1/users/me/password",
                headers=self.headers(restricted, version),
                json={"current_password": old, "new_password": new},
            )
            self.assert_error(response, status, code)
            for secret in (old, new):
                if isinstance(secret, str) and secret:
                    self.assertNotIn(secret, response.text)
        self.assertEqual(self.get_user(user["id"])["version"], version)

    def test_simple_long_and_unchanged_passwords_work_through_the_lifecycle(
        self,
    ) -> None:
        for password in (
            "1",
            "12345",
            "passwordpassword",
            "AbcdAbcdAbcdAbcd",
            "long-password-" * 20,
            "  ",
        ):
            with self.subTest(password=password):
                user = self.create_user(password=password)
                restricted = self.login(user["username"], password)
                response = self.client.put(
                    "/api/v1/users/me/password",
                    headers=self.headers(restricted, restricted["user"]["version"]),
                    json={"current_password": password, "new_password": password},
                )
                self.assertEqual(response.status_code, 204, response.text)
                self.assert_error(
                    self.client.get(
                        "/api/v1/users/me", headers=self.headers(restricted)
                    ),
                    401,
                    "UNAUTHENTICATED",
                )
                full = self.login(user["username"], password)
                self.assertEqual(full["auth_scope"], "full")
                self.assertFalse(full["user"]["must_change_password"])
                response = self.client.put(
                    f"/api/v1/users/{user['id']}/password",
                    headers=self.headers(self.admin, full["user"]["version"]),
                    json={"new_password": password},
                )
                self.assertEqual(response.status_code, 204, response.text)
                self.assert_error(
                    self.client.get("/api/v1/users/me", headers=self.headers(full)),
                    401,
                    "UNAUTHENTICATED",
                )
                reset = self.login(user["username"], password)
                self.assertEqual(reset["auth_scope"], "full")
                self.assertFalse(reset["user"]["must_change_password"])

    def test_admin_self_protection(self) -> None:
        user = self.admin["user"]
        for method, suffix, body in (
            ("DELETE", "", None),
            ("PUT", "/status", {"status": "disabled"}),
            ("PUT", "/roles", {"roles": ["member"]}),
            ("PUT", "/password", {"new_password": RESET_PASSWORD}),
        ):
            with self.subTest(suffix=suffix):
                self.assert_error(
                    self.client.request(
                        method,
                        f"/api/v1/users/{user['id']}{suffix}",
                        headers=self.headers(self.admin, user["version"]),
                        json=body,
                    ),
                    409,
                    "SELF_OPERATION_NOT_ALLOWED",
                )

    def test_unicode_passwords_preserve_spaces_and_reject_surrogates(self) -> None:
        password = "  支持中文的 Unicode passphrase 2026!  "
        user = self.create_user(password=password)
        login = self.login(user["username"], password)
        self.assertEqual(login["auth_scope"], "full")
        self.assert_error(
            self.client.post(
                "/api/v1/auth/login",
                json={
                    "username": user["username"],
                    "password": password.strip(),
                },
            ),
            401,
            "INVALID_CREDENTIALS",
        )
        for field in ("display_name", "password"):
            payload = {
                "username": "unicode-test",
                "display_name": "用户",
                "password": INITIAL_PASSWORD,
                field: "\ud800" * 15,
            }
            response = self.client.post(
                "/api/v1/users",
                headers={
                    **self.headers(self.admin),
                    "Content-Type": "application/json",
                },
                content=json.dumps(payload),
            )
            self.assert_error(response, 422, "VALIDATION_ERROR")

    def test_role_change_applies_to_existing_sessions(self) -> None:
        user = self.create_user(roles=["admin"])
        other = self.activate(user["username"])
        response = self.client.put(
            f"/api/v1/users/{user['id']}/roles",
            headers=self.headers(self.admin, other["user"]["version"]),
            json={"roles": ["member"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assert_error(
            self.client.get("/api/v1/users", headers=self.headers(other)),
            403,
            "PERMISSION_DENIED",
        )
        permissions = self.client.get(
            "/api/v1/users/me/permissions", headers=self.headers(other)
        ).json()["data"]
        self.assertNotIn("user.delete", permissions["permissions"])
        self.assertEqual(permissions["roles"], ["member"])
        self.assertEqual(
            self.client.get(
                "/api/v1/users/me", headers=self.headers(other)
            ).status_code,
            200,
        )

    def test_role_query_and_noop_role_status_updates(self) -> None:
        response = self.client.get("/api/v1/roles", headers=self.headers(self.admin))
        self.assertEqual(response.status_code, 200)
        roles = response.json()["data"]
        self.assertEqual([role["code"] for role in roles], ["admin", "member"])
        self.assertIn("user.password.reset", roles[0]["permissions"])
        user = self.create_user()
        for suffix, body in (
            ("roles", {"roles": ["member", "member"]}),
            ("status", {"status": "active"}),
        ):
            response = self.client.put(
                f"/api/v1/users/{user['id']}/{suffix}",
                headers=self.headers(self.admin, user["version"]),
                json=body,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["data"]["version"], user["version"])

    def test_list_filters_search_literals_and_pagination(self) -> None:
        first = self.create_user(
            display_name="Straße 100%", email="special@example.com"
        )
        self.create_user(display_name="其他名称", roles=["admin"])
        for q in ("STRASSE", "%", "SPECIAL@EXAMPLE.COM"):
            response = self.client.get(
                "/api/v1/users",
                headers=self.headers(self.admin),
                params={"q": q, "role": "member", "status": "active"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["data"]["total"], 1)
            self.assertEqual(response.json()["data"]["items"][0]["id"], first["id"])
        response = self.client.get(
            "/api/v1/users",
            headers=self.headers(self.admin),
            params={"page": 99, "page_size": 1, "sort_by": "created_at"},
        )
        self.assertEqual(response.json()["data"]["total"], 3)
        self.assertEqual(response.json()["data"]["items"], [])
        page = self.client.get(
            "/api/v1/users",
            headers=self.headers(self.admin),
            params={"sort_by": "last_login_at", "sort_order": "asc"},
        ).json()["data"]
        self.assertIsNotNone(page["items"][0]["last_login_at"])
        self.assertIsNone(page["items"][-1]["last_login_at"])

    def test_logout_only_current_session_and_session_expiry(self) -> None:
        another = self.login("admin")
        response = self.client.post(
            "/api/v1/auth/logout", headers=self.headers(another)
        )
        self.assertEqual(response.status_code, 204)
        self.assert_error(
            self.client.get("/api/v1/users/me", headers=self.headers(another)),
            401,
            "UNAUTHENTICATED",
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/users/me", headers=self.headers(self.admin)
            ).status_code,
            200,
        )
        self.clock.advance(self.settings.session_seconds)
        self.assert_error(
            self.client.get("/api/v1/users/me", headers=self.headers(self.admin)),
            401,
            "UNAUTHENTICATED",
        )

    def test_login_limits_persist_across_apps_and_expire(self) -> None:
        for _ in range(self.settings.login_account_limit):
            response = self.client.post(
                "/api/v1/auth/login",
                json={
                    "username": "absent",
                    "password": "wrong",
                },
            )
            self.assert_error(response, 401, "INVALID_CREDENTIALS")
        other_app = create_app(self.settings, clock=self.clock)
        with TestClient(other_app) as other_client:
            response = other_client.post(
                "/api/v1/auth/login",
                json={
                    "username": "absent",
                    "password": "wrong",
                },
            )
            self.assert_error(response, 429, "RATE_LIMITED")
            self.assertGreater(int(response.headers["Retry-After"]), 0)
            self.clock.advance(self.settings.account_window_seconds)
            response = other_client.post(
                "/api/v1/auth/login",
                json={
                    "username": "absent",
                    "password": "wrong",
                },
            )
            self.assert_error(response, 401, "INVALID_CREDENTIALS")

    def test_source_login_limit_handles_varying_usernames(self) -> None:
        self.service.settings = replace(self.settings, login_source_limit=4)
        # Administrator setup already consumed two source attempts.
        for index in range(2):
            response = self.client.post(
                "/api/v1/auth/login",
                json={
                    "username": f"unknown{index}",
                    "password": "wrong",
                },
            )
            self.assert_error(response, 401, "INVALID_CREDENTIALS")
        response = self.client.post(
            "/api/v1/auth/login",
            json={
                "username": "another-unknown",
                "password": "wrong",
            },
        )
        self.assert_error(response, 429, "RATE_LIMITED")

    def test_password_failures_count_toward_rate_limit(self) -> None:
        self.service.settings = replace(self.settings, password_limit=2)
        user = self.create_user()
        login = self.login(user["username"], INITIAL_PASSWORD)
        for status, code in (
            (400, "CURRENT_PASSWORD_INCORRECT"),
            (400, "CURRENT_PASSWORD_INCORRECT"),
            (429, "RATE_LIMITED"),
        ):
            response = self.client.put(
                "/api/v1/users/me/password",
                headers=self.headers(login, login["user"]["version"]),
                json={"current_password": "wrong", "new_password": FINAL_PASSWORD},
            )
            self.assert_error(response, status, code)

    def test_concurrent_updates_share_a_database_version(self) -> None:
        user = self.create_user()
        barrier = Barrier(2)

        def update(name: str) -> int:
            barrier.wait(timeout=5)
            return self.client.patch(
                f"/api/v1/users/{user['id']}",
                headers=self.headers(self.admin, user["version"]),
                json={"display_name": name},
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(update, ("名称 A", "名称 B")))
        self.assertEqual(sorted(results), [200, 412])
        self.assertEqual(self.get_user(user["id"])["version"], user["version"] + 1)

    def test_two_apps_cannot_concurrently_disable_each_other(self) -> None:
        other_user = self.create_user(roles=["admin"])
        other = self.activate(other_user["username"])
        admin_user = self.get_user(self.admin["user"]["id"])
        other_app = create_app(self.settings, clock=self.clock)
        barrier = Barrier(2)
        with TestClient(other_app) as second_client:

            def disable(args) -> int:
                client, actor, target = args
                barrier.wait(timeout=5)
                return client.put(
                    f"/api/v1/users/{target['id']}/status",
                    headers=self.headers(actor, target["version"]),
                    json={"status": "disabled"},
                ).status_code

            with ThreadPoolExecutor(max_workers=2) as executor:
                statuses = list(
                    executor.map(
                        disable,
                        (
                            (self.client, self.admin, other["user"]),
                            (second_client, other, admin_user),
                        ),
                    )
                )
        self.assertEqual(sorted(statuses), [200, 401])
        with self.database.transaction() as connection:
            remaining = connection.execute(
                "SELECT count(*) FROM users WHERE status='active' AND roles LIKE '%admin%' AND deleted_at IS NULL"
            ).fetchone()[0]
        self.assertEqual(remaining, 1)

    def test_two_admins_cannot_concurrently_remove_each_others_roles(self) -> None:
        user = self.create_user(roles=["admin"])
        other = self.activate(user["username"])
        current = self.get_user(self.admin["user"]["id"])
        barrier = Barrier(2)

        def demote(args) -> int:
            actor, target = args
            barrier.wait(timeout=5)
            return self.client.put(
                f"/api/v1/users/{target['id']}/roles",
                headers=self.headers(actor, target["version"]),
                json={"roles": ["member"]},
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(
                executor.map(
                    demote,
                    (
                        (self.admin, other["user"]),
                        (other, current),
                    ),
                )
            )
        self.assertEqual(sorted(statuses), [200, 403])

    def test_unavailable_authority_fails_closed(self) -> None:
        with (
            patch.object(
                self.database,
                "transaction",
                side_effect=sqlite3.OperationalError("unavailable"),
            ),
            self.assertLogs("agent_permissions.service", level="ERROR"),
        ):
            response = self.client.get(
                "/api/v1/users/me", headers=self.headers(self.admin)
            )
        self.assert_error(response, 503, "SERVICE_UNAVAILABLE")
        self.assertNotIn("unavailable", response.text)

    def test_audit_failure_rolls_back_user_creation(self) -> None:
        with (
            patch.object(
                self.service,
                "_audit",
                side_effect=sqlite3.OperationalError("audit unavailable"),
            ),
            self.assertLogs("agent_permissions.service", level="ERROR"),
        ):
            response = self.client.post(
                "/api/v1/users",
                headers=self.headers(self.admin),
                json={
                    "username": "rolledback",
                    "display_name": "不可写入",
                    "password": INITIAL_PASSWORD,
                },
            )
        self.assert_error(response, 503, "SERVICE_UNAVAILABLE")
        with self.database.transaction() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM users WHERE username='rolledback'"
                ).fetchone()
            )

    def test_audit_failure_rolls_back_password_and_session_revocation(self) -> None:
        user = self.create_user()
        member = self.activate(user["username"])
        with (
            patch.object(
                self.service,
                "_audit",
                side_effect=sqlite3.OperationalError("audit unavailable"),
            ),
            self.assertLogs("agent_permissions.service", level="ERROR"),
        ):
            response = self.client.put(
                f"/api/v1/users/{user['id']}/password",
                headers=self.headers(self.admin, member["user"]["version"]),
                json={"new_password": RESET_PASSWORD},
            )
        self.assert_error(response, 503, "SERVICE_UNAVAILABLE")
        response = self.client.get("/api/v1/users/me", headers=self.headers(member))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["version"], member["user"]["version"])
        self.assertEqual(self.login(user["username"])["auth_scope"], "full")

    def test_audit_and_sessions_do_not_store_raw_secrets(self) -> None:
        user = self.create_user()
        member = self.activate(user["username"])
        self.client.post(
            "/api/v1/auth/login",
            json={"username": "absent", "password": "sensitive-invalid-password"},
        )
        with self.database.transaction() as connection:
            audit = json.dumps(
                [dict(row) for row in connection.execute("SELECT * FROM audit_events")]
            )
            sessions = [
                dict(row) for row in connection.execute("SELECT * FROM sessions")
            ]
            encoded = connection.execute(
                "SELECT password_hash FROM users WHERE id=?", (user["id"],)
            ).fetchone()[0]
        for secret in (
            INITIAL_PASSWORD,
            FINAL_PASSWORD,
            member["session_token"],
            encoded,
            "sensitive-invalid-password",
        ):
            self.assertNotIn(secret, audit)
            self.assertNotIn(secret, json.dumps(sessions))
        self.assertIn("password_changed", audit)
        self.assertIn("INVALID_CREDENTIALS", audit)
        self.assertTrue(
            any(
                row["token_digest"] == token_digest(member["session_token"])
                for row in sessions
            )
        )

    def test_non_http_service_cannot_bypass_profile_fields_or_self_identity(
        self,
    ) -> None:
        user = self.create_user()
        authority = Authority(token_digest(self.admin["session_token"]))
        info = RequestInfo("direct-service", "test")
        with self.assertRaises(ApiError) as raised:
            self.service.mutate_user(
                authority, "user.update", user["id"], '"v1"', {"roles": ["admin"]}, info
            )
        self.assertEqual(raised.exception.status, 422)
        own = self.get_user(self.admin["user"]["id"])
        self.service.mutate_user(
            authority,
            "user.self.update",
            user["id"],
            f'"v{own["version"]}"',
            {"display_name": "管理员新名称"},
            info,
        )
        self.assertEqual(
            self.get_user(user["id"])["display_name"], user["display_name"]
        )
        self.assertEqual(self.get_user(own["id"])["display_name"], "管理员新名称")

    def test_unexpected_errors_are_redacted(self) -> None:
        with patch.object(
            self.service, "get_me", side_effect=RuntimeError("secret exception body")
        ):
            response = self.client.get(
                "/api/v1/users/me", headers=self.headers(self.admin)
            )
        self.assert_error(response, 500, "INTERNAL_ERROR")
        self.assertNotIn("secret exception body", response.text)

    def test_openapi_matches_all_documented_operations(self) -> None:
        schema = self.client.get("/openapi.json").json()
        operations = {
            (method.upper(), path)
            for path, value in schema["paths"].items()
            for method in value
            if method in {"get", "post", "patch", "put", "delete"}
        }
        document = Path(__file__).parent / "docs/user-management-api.md"
        expected = set(
            re.findall(
                r"^\| API-\d{2} \| `(\w+)` \| `([^`]+)`",
                document.read_text(),
                re.MULTILINE,
            )
        )
        self.assertEqual(operations, expected)
        self.assertEqual(len(operations), 15)
        login_schema = schema["components"]["schemas"]["LoginRequest"]
        self.assertTrue(login_schema["properties"]["password"]["writeOnly"])

    def test_bootstrap_cannot_be_repeated(self) -> None:
        with self.assertRaises(ApiError) as error:
            self.service.bootstrap_admin(
                UserCreate(
                    username="second", display_name="管理员", password=INITIAL_PASSWORD
                )
            )
        self.assertEqual(error.exception.code, "BOOTSTRAP_ALREADY_COMPLETED")


class BootstrapCliTests(unittest.TestCase):
    def test_default_initialization_uses_12345_without_prompting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cli.sqlite3"
            with (
                patch.dict(os.environ, {"AGENT_USERS_DATABASE_PATH": str(path)}),
                patch(
                    "agent_permissions.__main__.getpass.getpass",
                    side_effect=AssertionError("默认初始化不应提示输入密码"),
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli_main(["init-admin", "--username", "admin"]), 0)
            with TestClient(create_app(Settings(database_path=path))) as client:
                response = client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "12345"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()["data"]
                self.assertEqual(data["auth_scope"], "full")
                self.assertEqual(data["user"]["roles"], ["admin"])
                response = client.get(
                    "/api/v1/users",
                    headers={"Authorization": f"Bearer {data['session_token']}"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                response = client.put(
                    "/api/v1/users/me/password",
                    headers={
                        "Authorization": f"Bearer {data['session_token']}",
                        "If-Match": f'"v{data["user"]["version"]}"',
                    },
                    json={
                        "current_password": "12345",
                        "new_password": "Updated initial password",
                    },
                )
                self.assertEqual(response.status_code, 204, response.text)
                with (
                    patch.dict(os.environ, {"AGENT_USERS_DATABASE_PATH": str(path)}),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(cli_main(["init-admin", "--username", "admin"]), 1)
                response = client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "Updated initial password"},
                )
                self.assertEqual(response.status_code, 200, response.text)

    def test_interactive_initialization_without_password_arguments(self) -> None:
        password = "Custom initial password"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cli.sqlite3"
            with patch.dict(os.environ, {"AGENT_USERS_DATABASE_PATH": str(path)}):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = cli_main(
                        ["init-admin", "--username", "admin", "--prompt-password"],
                        password_reader=lambda _: password,
                    )
                self.assertEqual(code, 0)
                self.assertNotIn(password, output.getvalue())
                with closing(sqlite3.connect(path)) as connection:
                    row = connection.execute(
                        "SELECT roles,must_change_password FROM users"
                    ).fetchone()
                    self.assertEqual(json.loads(row[0]), ["admin"])
                    self.assertEqual(row[1], 0)
                with TestClient(create_app(Settings(database_path=path))) as client:
                    response = client.post(
                        "/api/v1/auth/login",
                        json={"username": "admin", "password": password},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["data"]["auth_scope"], "full")
                    token = response.json()["data"]["session_token"]
                    response = client.get(
                        "/api/v1/users", headers={"Authorization": f"Bearer {token}"}
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                with redirect_stdout(io.StringIO()):
                    code = cli_main(
                        ["init-admin", "--username", "other", "--prompt-password"],
                        password_reader=lambda _: password,
                    )
                self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
