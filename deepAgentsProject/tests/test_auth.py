from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from apps.platform_api.main import create_app
from packages.persistence import Database
from packages.runtime.model_gateway import DeterministicModelGateway


BOOTSTRAP_PASSWORD = "Console1@"
ADMIN_PASSWORD = "AdminReady2@"


def secured_client(tmp_path):
    database_path = str(tmp_path / "auth.db")
    return database_path, TestClient(
        create_app(
            database_path,
            seed=False,
            model_gateway=DeterministicModelGateway(),
            load_env=False,
            trust_identity_headers=False,
            allow_demo_identity=False,
        )
    )


def login(client: TestClient, username: str = "admin", password: str = BOOTSTRAP_PASSWORD):
    return client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )


def bearer(session: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['access_token']}"}


def ready_admin(client: TestClient) -> tuple[dict, dict[str, str]]:
    session = login(client).json()
    headers = bearer(session)
    changed = client.put(
        "/api/v1/auth/password",
        headers=headers,
        json={
            "current_password": BOOTSTRAP_PASSWORD,
            "new_password": ADMIN_PASSWORD,
            "version": session["user"]["version"],
        },
    )
    assert changed.status_code == 200, changed.text
    session["user"] = changed.json()
    return session, headers


def create_user(
    client: TestClient,
    headers: dict[str, str],
    username: str,
    *,
    password: str = "Member1@",
    tenant_id: str = "tenant_demo",
    project_id: str = "project_atlas",
    roles: list[str] | None = None,
    is_super_admin: bool = False,
) -> dict:
    response = client.post(
        "/api/v1/users",
        headers=headers,
        json={
            "username": username,
            "display_name": username.replace(".", " ").title(),
            "password": password,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "environment_id": "env_development",
            "roles": roles or ["member"],
            "is_super_admin": is_super_admin,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def ready_user(
    client: TestClient,
    user: dict,
    username: str,
    old_password: str = "Member1@",
    new_password: str = "MemberReady2@",
) -> tuple[dict, dict[str, str]]:
    session = login(client, username, old_password).json()
    headers = bearer(session)
    assert session["user"]["must_change_password"] is True
    changed = client.put(
        "/api/v1/auth/password",
        headers=headers,
        json={
            "current_password": old_password,
            "new_password": new_password,
            "version": user["version"],
        },
    )
    assert changed.status_code == 200, changed.text
    session["user"] = changed.json()
    return session, headers


def test_bootstrap_forces_password_change_and_contracts_are_explicit(tmp_path):
    database_path, test_client = secured_client(tmp_path)
    with test_client as client:
        assert client.get("/api/v1/context").status_code == 401
        rejected = login(client, password="wrong-password")
        assert rejected.status_code == 401
        assert rejected.json()["error"]["message"] == "Invalid username or password"

        authenticated = login(client)
        session = authenticated.json()
        headers = bearer(session)
        assert authenticated.status_code == 200
        assert session["user"]["is_super_admin"] is True
        assert session["user"]["must_change_password"] is True
        assert session["access_token"] in authenticated.headers["set-cookie"]
        assert "HttpOnly" in authenticated.headers["set-cookie"]
        assert client.get("/api/v1/context", headers=headers).status_code == 403

        changed = client.put(
            "/api/v1/auth/password",
            headers=headers,
            json={
                "current_password": BOOTSTRAP_PASSWORD,
                "new_password": ADMIN_PASSWORD,
                "version": session["user"]["version"],
            },
        )
        assert changed.status_code == 200
        assert changed.json()["must_change_password"] is False
        context = client.get("/api/v1/context", headers=headers)
        assert context.status_code == 200
        assert context.json()["user"]["role"] == "super_admin"

        openapi = client.get("/openapi.json").json()
        schemas = openapi["components"]["schemas"]
        for schema in (
            "LoginResponse",
            "UserResponse",
            "UserListResponse",
            "AuthSessionListResponse",
            "AuthAuditListResponse",
        ):
            assert schema in schemas
        assert (
            openapi["paths"]["/api/v1/users"]["get"]["responses"]["200"]["content"]
            ["application/json"]["schema"]["$ref"]
            == "#/components/schemas/UserListResponse"
        )

    connection = sqlite3.connect(database_path)
    password_hash = connection.execute(
        "SELECT password_hash FROM users WHERE username='admin'"
    ).fetchone()[0]
    token_hash = connection.execute("SELECT token_hash FROM auth_sessions").fetchone()[0]
    migrations = connection.execute(
        "SELECT version, name FROM schema_migrations ORDER BY version"
    ).fetchall()
    connection.close()
    assert password_hash.startswith("pbkdf2_sha256$")
    assert ADMIN_PASSWORD not in password_hash
    assert session["access_token"] != token_hash
    assert len(token_hash) == 64
    assert migrations == [
        (1, "record-existing-platform-schema"),
        (2, "auth-user-governance"),
        (3, "durable-task-queue"),
        (4, "worker-heartbeats"),
        (5, "sandbox-execution-lease-authority"),
        (6, "durable-model-budget"),
        (7, "evaluation-release-gates"),
        (8, "coding-consistent-recovery"),
        (9, "unified-metering-and-quotas"),
        (10, "thread-access-and-source-provenance"),
        (11, "routing-ownership-and-atomic-review"),
        (12, "complete-metering-attribution"),
        (13, "immutable-model-bindings"),
        (14, "knowledge-metadata-access"),
        (15, "governed-production-releases"),
        (16, "outstanding-work-admission-indexes"),
        (17, "governed-production-routing"),
        (18, "durable-cancellation-finalization"),
        (19, "durable-trace-origins"),
        (20, "bounded-knowledge-upload-pipeline"),
    ]


def test_user_crud_pagination_soft_delete_audit_and_optimistic_lock(tmp_path):
    _, test_client = secured_client(tmp_path)
    with test_client as client:
        _, admin_headers = ready_admin(client)
        operator = create_user(
            client,
            admin_headers,
            "operator.one",
            roles=["member", "reviewer"],
        )
        create_user(client, admin_headers, "operator.two", roles=["member"])
        duplicate = client.post(
            "/api/v1/users",
            headers=admin_headers,
            json={
                "username": "OPERATOR.ONE",
                "display_name": "Duplicate",
                "password": "Member1@",
            },
        )
        assert duplicate.status_code == 409

        first_page = client.get(
            "/api/v1/users?page=1&page_size=1&q=operator&status=ACTIVE&role=member"
            "&sort_by=username&sort_order=desc",
            headers=admin_headers,
        )
        assert first_page.status_code == 200
        assert first_page.json()["total"] == 2
        assert first_page.json()["pages"] == 2
        assert first_page.json()["items"][0]["username"] == "operator.two"

        updated = client.patch(
            f"/api/v1/users/{operator['id']}",
            headers=admin_headers,
            json={
                "version": operator["version"],
                "display_name": "Operations Reviewer",
                "roles": ["reviewer"],
            },
        )
        assert updated.status_code == 200
        operator = updated.json()
        assert operator["version"] == 2
        stale = client.patch(
            f"/api/v1/users/{operator['id']}",
            headers=admin_headers,
            json={"version": 1, "display_name": "Stale overwrite"},
        )
        assert stale.status_code == 409

        missing_reason = client.request(
            "DELETE",
            f"/api/v1/users/{operator['id']}",
            headers=admin_headers,
            json={"version": operator["version"], "reason": ""},
        )
        assert missing_reason.status_code == 422
        disabled = client.request(
            "DELETE",
            f"/api/v1/users/{operator['id']}",
            headers=admin_headers,
            json={"version": operator["version"], "reason": "Employment ended"},
        )
        assert disabled.status_code == 200
        operator = disabled.json()
        assert operator["status"] == "INACTIVE"
        assert operator["deleted_by"]
        assert operator["deleted_at"]
        assert operator["deletion_reason"] == "Employment ended"

        reactivated = client.patch(
            f"/api/v1/users/{operator['id']}",
            headers=admin_headers,
            json={"version": operator["version"], "status": "ACTIVE"},
        )
        assert reactivated.status_code == 200
        assert reactivated.json()["deleted_at"] is None
        assert reactivated.json()["deletion_reason"] is None

        audit = client.get(
            f"/api/v1/users/audit-events?target_user_id={operator['id']}",
            headers=admin_headers,
        )
        assert audit.status_code == 200
        actions = {event["action"] for event in audit.json()["items"]}
        assert {"USER_CREATED", "USER_UPDATED", "USER_DEACTIVATED", "USER_REACTIVATED"} <= actions
        serialized = str(audit.json()).lower()
        assert "member1@" not in serialized
        assert "password_hash" not in serialized


def test_password_reset_self_change_expiry_and_session_revocation(tmp_path):
    database_path, test_client = secured_client(tmp_path)
    with test_client as client:
        _, admin_headers = ready_admin(client)
        operator = create_user(client, admin_headers, "operator.one")
        first_session, first_headers = ready_user(client, operator, "operator.one")
        operator = first_session["user"]
        assert client.get("/api/v1/context", headers=first_headers).status_code == 200
        second_session = login(client, "operator.one", "MemberReady2@").json()
        second_headers = bearer(second_session)

        sessions = client.get("/api/v1/auth/sessions", headers=second_headers)
        assert sessions.status_code == 200
        assert len(sessions.json()["items"]) == 2
        assert sum(item["current"] for item in sessions.json()["items"]) == 1

        reset = client.put(
            f"/api/v1/users/{operator['id']}/password",
            headers=admin_headers,
            json={"password": "ResetReady3@", "version": operator["version"]},
        )
        assert reset.status_code == 200
        reset_user = reset.json()
        assert reset_user["must_change_password"] is True
        assert client.get("/api/v1/auth/me", headers=first_headers).status_code == 401
        assert client.get("/api/v1/auth/me", headers=second_headers).status_code == 401
        assert login(client, "operator.one", "MemberReady2@").status_code == 401

        reset_session = login(client, "operator.one", "ResetReady3@").json()
        reset_headers = bearer(reset_session)
        assert client.get("/api/v1/context", headers=reset_headers).status_code == 403
        wrong_current = client.put(
            "/api/v1/auth/password",
            headers=reset_headers,
            json={
                "current_password": "Wrong4@x",
                "new_password": "FinalReady4@",
                "version": reset_user["version"],
            },
        )
        assert wrong_current.status_code == 422
        changed = client.put(
            "/api/v1/auth/password",
            headers=reset_headers,
            json={
                "current_password": "ResetReady3@",
                "new_password": "FinalReady4@",
                "version": reset_user["version"],
            },
        )
        assert changed.status_code == 200
        assert changed.json()["must_change_password"] is False

        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        connection = sqlite3.connect(database_path)
        connection.execute(
            "UPDATE users SET password_expires_at=? WHERE id=?",
            (expired, operator["id"]),
        )
        connection.commit()
        connection.close()
        assert client.get("/api/v1/auth/me", headers=reset_headers).json()[
            "must_change_password"
        ] is True
        assert client.get("/api/v1/context", headers=reset_headers).status_code == 403


def test_tenant_admin_scope_and_normal_user_boundaries(tmp_path):
    _, test_client = secured_client(tmp_path)
    with test_client as client:
        _, admin_headers = ready_admin(client)
        tenant_admin = create_user(
            client,
            admin_headers,
            "tenant.manager",
            tenant_id="tenant_a",
            project_id="project_a",
            roles=["tenant_admin"],
        )
        _, tenant_headers = ready_user(client, tenant_admin, "tenant.manager")
        own_member = create_user(
            client,
            tenant_headers,
            "tenant.member",
            tenant_id="tenant_a",
            project_id="project_b",
        )
        denied_cross_tenant = client.post(
            "/api/v1/users",
            headers=tenant_headers,
            json={
                "username": "outside.member",
                "display_name": "Outside Member",
                "password": "Member1@",
                "tenant_id": "tenant_b",
                "project_id": "project_b",
                "environment_id": "env_development",
                "roles": ["member"],
                "is_super_admin": False,
            },
        )
        assert denied_cross_tenant.status_code == 403
        assert client.get(
            "/api/v1/users?tenant_id=tenant_b", headers=tenant_headers
        ).status_code == 403
        scoped = client.get("/api/v1/users", headers=tenant_headers).json()
        assert scoped["total"] == 2
        assert {item["tenant_id"] for item in scoped["items"]} == {"tenant_a"}
        assert not any(item["is_super_admin"] for item in scoped["items"])

        promote = client.patch(
            f"/api/v1/users/{own_member['id']}",
            headers=tenant_headers,
            json={"version": own_member["version"], "is_super_admin": True},
        )
        assert promote.status_code == 403
        move = client.patch(
            f"/api/v1/users/{own_member['id']}",
            headers=tenant_headers,
            json={"version": own_member["version"], "tenant_id": "tenant_b"},
        )
        assert move.status_code == 403

        _, normal_headers = ready_user(client, own_member, "tenant.member")
        assert client.get("/api/v1/users", headers=normal_headers).status_code == 403
        assert client.get("/api/v1/auth/sessions", headers=normal_headers).status_code == 200


def test_failed_login_lockout_rate_limit_and_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPAGENT_MAX_FAILED_LOGINS", "3")
    monkeypatch.setenv("DEEPAGENT_LOGIN_LOCKOUT_MINUTES", "1")
    _, test_client = secured_client(tmp_path)
    with test_client as client:
        _, admin_headers = ready_admin(client)
        user = create_user(client, admin_headers, "locked.user")
        assert login(client, "locked.user", "wrong-one").status_code == 401
        assert login(client, "locked.user", "wrong-two").status_code == 401
        limited = login(client, "locked.user", "wrong-three")
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) > 0
        still_locked = login(client, "locked.user", "Member1@")
        assert still_locked.status_code == 429
        loaded = client.get(f"/api/v1/users/{user['id']}", headers=admin_headers).json()
        assert loaded["failed_login_count"] == 3
        assert loaded["locked_until"]
        audit = client.get(
            f"/api/v1/users/audit-events?action=LOGIN_FAILED&target_user_id={user['id']}",
            headers=admin_headers,
        ).json()
        assert audit["total"] == 3


def test_session_kick_revoke_all_last_seen_and_expired_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPAGENT_SESSION_LAST_SEEN_SECONDS", "0")
    database_path, test_client = secured_client(tmp_path)
    with test_client as client:
        _, admin_headers = ready_admin(client)
        user = create_user(client, admin_headers, "session.user")
        first, first_headers = ready_user(client, user, "session.user")
        second = login(client, "session.user", "MemberReady2@").json()
        second_headers = bearer(second)
        items = client.get("/api/v1/auth/sessions", headers=second_headers).json()["items"]
        first_id = next(item["id"] for item in items if not item["current"])
        kicked = client.delete(
            f"/api/v1/auth/sessions/{first_id}", headers=second_headers
        )
        assert kicked.status_code == 200
        assert kicked.json()["revoked_count"] == 1
        assert client.get("/api/v1/auth/me", headers=first_headers).status_code == 401

        third = login(client, "session.user", "MemberReady2@").json()
        third_headers = bearer(third)
        admin_view = client.get(
            f"/api/v1/users/{user['id']}/sessions", headers=admin_headers
        )
        assert admin_view.status_code == 200
        assert any(item["ip_address"] for item in admin_view.json()["items"])
        revoke_all = client.delete(
            f"/api/v1/users/{user['id']}/sessions", headers=admin_headers
        )
        assert revoke_all.status_code == 200
        assert revoke_all.json()["revoked_count"] >= 2
        assert client.get("/api/v1/auth/me", headers=second_headers).status_code == 401
        assert client.get("/api/v1/auth/me", headers=third_headers).status_code == 401

        connection = sqlite3.connect(database_path)
        connection.execute(
            """INSERT INTO auth_sessions(
                   id, token_hash, user_id, expires_at, revoked_at, created_at,
                   last_seen_at, ip_address, user_agent
               ) VALUES(?,?,?,?,NULL,?,?,NULL,NULL)""",
            (
                "session_expired_test",
                "0" * 64,
                user["id"],
                (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        connection.commit()
        connection.close()
        assert client.app.state.services.auth.purge_expired_sessions() >= 1


def test_legacy_database_is_upgraded_by_versioned_migration(tmp_path):
    path = str(tmp_path / "legacy.db")
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE users (
          id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
          password_hash TEXT NOT NULL, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
          environment_id TEXT NOT NULL, roles_json TEXT NOT NULL DEFAULT '[]',
          is_super_admin INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'ACTIVE',
          last_login_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE auth_sessions (
          id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, user_id TEXT NOT NULL,
          expires_at TEXT NOT NULL, revoked_at TEXT, created_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL
        );
        """
    )
    connection.close()
    db = Database(path)
    db.initialize()
    user_columns = {
        row["name"] for row in db.connection.execute("PRAGMA table_info(users)")
    }
    session_columns = {
        row["name"] for row in db.connection.execute("PRAGMA table_info(auth_sessions)")
    }
    versions = db.connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert {"version", "deleted_at", "must_change_password", "locked_until"} <= user_columns
    assert {"ip_address", "user_agent"} <= session_columns
    assert [row[0] for row in versions] == list(range(1, 21))
    db.close()
