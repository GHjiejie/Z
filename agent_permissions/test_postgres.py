"""Run shared API/RBAC contracts and migration checks against real PostgreSQL."""

import io
import os
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient
from psycopg import sql

from agent_permissions import (
    test_agent_grants,
    test_agui,
    test_api,
    test_audit,
    test_hitl,
    test_model_usage,
    test_rbac,
    test_tool_discovery,
)
from agent_permissions.__main__ import main as cli_main
from agent_permissions.api import create_app
from agent_permissions.config import Settings
from agent_permissions.migrate_postgres import TABLE_COLUMNS, fingerprint, migrate
from agent_permissions.migrate_postgres import main as migrate_main
from agent_permissions.models import UserCreate
from agent_permissions.postgres import PostgresDatabase
from agent_permissions.test_support import grant_tools

TEST_URL = os.getenv("AGENT_USERS_TEST_DATABASE_URL")


AUDIT_TEST_URL = os.getenv("AGENT_AUDIT_TEST_DATABASE_URL")


class PostgresFixture:
    def isolated_url(self, base_url):
        schema = f"test_agent_users_{uuid4().hex}"
        with psycopg.connect(base_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
            )

        def cleanup() -> None:
            with psycopg.connect(base_url, autocommit=True) as connection:
                connection.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )

        self.addCleanup(cleanup)
        parts = urlsplit(base_url)
        return urlunsplit(
            parts._replace(
                query=urlencode(
                    [
                        *parse_qsl(parts.query),
                        ("options", f"-csearch_path={schema},pg_catalog"),
                    ]
                )
            )
        )

    def settings_for(self, path: Path) -> Settings:
        return Settings(
            database_path=path,
            database_url=self.isolated_url(TEST_URL),
            db_pool_min_size=1,
            db_pool_max_size=2,
        )


@unittest.skipUnless(
    TEST_URL and AUDIT_TEST_URL,
    "Set both disposable business and audit PostgreSQL URLs",
)
class PostgresAuditTests(PostgresFixture, test_audit.AuditTests):
    def settings_for(self, path):
        return replace(
            super().settings_for(path),
            audit_database_url=self.isolated_url(AUDIT_TEST_URL),
        )


@unittest.skipUnless(
    TEST_URL, "Set AGENT_USERS_TEST_DATABASE_URL for PostgreSQL integration tests"
)
class PostgresApiTests(PostgresFixture, test_api.UserApiTests):
    pass


@unittest.skipUnless(
    TEST_URL, "Set AGENT_USERS_TEST_DATABASE_URL for PostgreSQL integration tests"
)
class PostgresRbacTests(PostgresFixture, test_rbac.RbacTests):
    pass


@unittest.skipUnless(
    TEST_URL, "Set AGENT_USERS_TEST_DATABASE_URL for PostgreSQL integration tests"
)
class PostgresAgentGrantTests(PostgresFixture, test_agent_grants.UserAgentGrantTests):
    pass


@unittest.skipUnless(
    TEST_URL, "Set AGENT_USERS_TEST_DATABASE_URL for PostgreSQL integration tests"
)
class PostgresHitlTests(PostgresFixture, test_hitl.HitlTests):
    pass


@unittest.skipUnless(
    TEST_URL, "Set AGENT_USERS_TEST_DATABASE_URL for PostgreSQL integration tests"
)
class PostgresAguiTests(PostgresFixture, test_agui.AguiTests):
    pass


@unittest.skipUnless(
    TEST_URL, "Set AGENT_USERS_TEST_DATABASE_URL for PostgreSQL integration tests"
)
class PostgresModelUsageTests(PostgresFixture, test_model_usage.ModelUsageTests):
    pass


@unittest.skipUnless(
    TEST_URL, "Set AGENT_USERS_TEST_DATABASE_URL for PostgreSQL integration tests"
)
class PostgresDiscoveryTests(PostgresFixture, test_tool_discovery.DiscoveryTests):
    pass


@unittest.skipUnless(
    TEST_URL, "Set AGENT_USERS_TEST_DATABASE_URL for PostgreSQL integration tests"
)
class PostgresMigrationTests(PostgresFixture, unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "source.sqlite3"
        settings = self.settings_for(self.path)
        self.target = PostgresDatabase(settings.database_url, settings=settings)
        self.addCleanup(self.target.close)
        self.target.open()

    def source(self) -> None:
        with TestClient(create_app(Settings(database_path=self.path))) as client:
            client.app.state.service.bootstrap_admin(
                UserCreate(username="admin", display_name="管理员", password="12345")
            )
            auth = client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "12345"}
            )
            self.headers = {
                "Authorization": f"Bearer {auth.json()['data']['session_token']}"
            }
            role = client.post(
                "/api/v1/roles",
                headers=self.headers,
                json={
                    "code": "auditor",
                    "display_name": "审计员",
                    "permissions": ["user.self.read", "user.read"],
                },
            )
            self.assertEqual(role.status_code, 201)
            response = client.post(
                "/api/v1/users",
                headers=self.headers,
                json={
                    "username": "member",
                    "display_name": "Straße 100%",
                    "password": "12345",
                    "roles": ["member", "auditor"],
                },
            )
            self.assertEqual(response.status_code, 201)
            grant_tools(
                client,
                self.headers,
                response.json()["data"]["id"],
                ["user.list", "user.self.read"],
            )

    def test_full_migration_preserves_hashes_sessions_relations_and_search(
        self,
    ) -> None:
        self.source()
        report = migrate(self.path, self.target)
        self.assertTrue(Path(report["backup"]).is_file())
        self.assertEqual(report["tables"]["users"]["rows"], 2)
        self.assertEqual(report["tables"]["user_agent_tool_grants"]["rows"], 2)
        with self.target.transaction() as target:
            for table in TABLE_COLUMNS:
                self.assertEqual(
                    fingerprint(target.execute(f"SELECT * FROM {table}").fetchall()),
                    report["tables"][table]["sha256"],
                )
        with TestClient(
            create_app(Settings(database_url=self.target.database_url))
        ) as client:
            # The same opaque token remains valid after switching databases.
            response = client.get("/api/v1/users/me/permissions", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertIn("role.update", response.json()["data"]["permissions"])
            response = client.get(
                "/api/v1/users", headers=self.headers, params={"q": "STRASSE"}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["data"]["total"], 1)
            self.assertEqual(
                client.post(
                    "/api/v1/auth/login",
                    json={"username": "member", "password": "12345"},
                ).status_code,
                200,
            )
            self.assertEqual(
                client.get(
                    "/api/v1/users", headers=self.headers, params={"q": "\x00"}
                ).status_code,
                422,
            )
        with self.assertRaisesRegex(ValueError, "目标数据库已有数据"):
            migrate(self.path, self.target)

    def test_schema_two_source_import_uses_default_grants_and_new_management_permission(
        self,
    ):
        self.source()
        import sqlite3

        with closing(sqlite3.connect(self.path)) as source, source:
            source.execute("DROP TABLE user_agent_tool_grants")
            source.execute("ALTER TABLE users DROP COLUMN agent_tools_version")
            source.execute(
                "DELETE FROM role_permissions WHERE permission_code='user.agent_tools.manage'"
            )
            source.execute(
                "DELETE FROM permissions WHERE code='user.agent_tools.manage'"
            )
            source.execute("PRAGMA user_version=2")
        report = migrate(self.path, self.target)
        self.assertEqual(report["source_schema"], 2)
        with self.target.transaction() as target:
            self.assertEqual(
                target.execute(
                    "SELECT count(*) FROM user_agent_tool_grants"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                target.execute("SELECT min(agent_tools_version) FROM users").fetchone()[
                    0
                ],
                1,
            )
            self.assertIsNotNone(
                target.execute(
                    "SELECT 1 FROM role_permissions WHERE role_code='admin' AND permission_code='user.agent_tools.manage'"
                ).fetchone()
            )

    def test_postgres_schema_two_upgrade_is_atomic_and_idempotent(self):
        self.source()
        migrate(self.path, self.target)
        with self.target.transaction(write=True) as target:
            target.execute("DROP TABLE user_agent_tool_grants")
            target.execute("ALTER TABLE users DROP COLUMN agent_tools_version")
            target.execute("UPDATE schema_metadata SET version=2")
        with (
            patch(
                "agent_permissions.database.Database._seed_catalog",
                side_effect=RuntimeError("migration failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "migration failure"),
        ):
            self.target.initialize()
        with self.target.transaction() as target:
            self.assertEqual(
                target.execute(
                    "SELECT version FROM schema_metadata WHERE key='schema_version'"
                ).fetchone()[0],
                2,
            )
            self.assertIsNone(
                target.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='users' AND column_name='agent_tools_version'"
                ).fetchone()
            )
        self.target.initialize()
        self.target.initialize()
        with self.target.transaction() as target:
            self.assertEqual(
                target.execute(
                    "SELECT version FROM schema_metadata WHERE key='schema_version'"
                ).fetchone()[0],
                7,
            )
            self.assertEqual(
                target.execute("SELECT count(*) FROM users").fetchone()[0], 2
            )

    def test_migrate_waiting_graph_and_resume_on_postgres(self):
        from agent_permissions.test_hitl import InteractionModel, events
        from agent_permissions.tracing import ChatTracing, TracingSettings

        model = InteractionModel()
        with TestClient(
            create_app(
                Settings(database_path=self.path),
                chat_model_factory=lambda: model,
                chat_tracing=ChatTracing(TracingSettings()),
            )
        ) as source:
            source.app.state.service.bootstrap_admin(
                UserCreate(username="admin", display_name="管理员", password="12345")
            )
            login = source.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "12345"}
            ).json()["data"]
            headers = {"Authorization": "Bearer " + login["session_token"]}
            grant_tools(source, headers, login["user"]["id"])
            response = source.post(
                "/api/v1/chat/runs",
                headers=headers,
                json={
                    "model": model.model_name,
                    "messages": [{"role": "user", "content": "创建角色"}],
                    "client_request_id": "migration",
                    "conversation_id": "migration",
                },
            )
            run = [event["run"] for event in events(response) if "run" in event][-1]
            self.assertEqual(run["status"], "waiting")
        report = migrate(self.path, self.target)
        self.assertGreater(report["tables"]["agent_audit_outbox"]["rows"], 0)
        self.assertGreater(report["tables"]["agent_audit_call_refs"]["rows"], 0)
        self.assertGreater(report["tables"]["agent_checkpoints"]["rows"], 0)
        self.assertGreater(report["tables"]["agent_model_usage"]["rows"], 0)
        settings = Settings(
            database_path=self.path, database_url=self.target.database_url
        )
        with TestClient(
            create_app(
                settings,
                chat_model_factory=lambda: model,
                chat_tracing=ChatTracing(TracingSettings()),
            )
        ) as destination:
            current = destination.get(
                f"/api/v1/chat/runs/{run['id']}", headers=headers
            ).json()["data"]
            self.assertEqual(current["interaction"]["id"], run["interaction"]["id"])
            result = destination.post(
                f"/api/v1/chat/runs/{run['id']}/interactions/{run['interaction']['id']}/resume",
                headers=headers,
                json={"submission_id": "after-migration", "expected_version": 1},
            )
            done = [event["run"] for event in events(result) if "run" in event][-1]
            self.assertEqual(done["status"], "completed", done)
            self.assertEqual(
                destination.get("/api/v1/roles/new_role", headers=headers).status_code,
                200,
            )

    def test_copy_error_rolls_back_all_imported_tables(self) -> None:
        self.source()
        import sqlite3

        with closing(sqlite3.connect(self.path)) as source, source:
            source.execute(
                "UPDATE users SET display_name=? WHERE username='member'",
                ("unsupported\x00text",),
            )
        with self.assertRaises(psycopg.Error):
            migrate(self.path, self.target)
        with self.target.transaction() as target:
            for table in TABLE_COLUMNS:
                self.assertEqual(
                    target.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0
                )

    def test_unknown_postgres_schema_is_rejected(self) -> None:
        self.target.initialize()
        with self.target.transaction(write=True) as connection:
            connection.execute("UPDATE schema_metadata SET version=99")
        with self.assertRaises(RuntimeError):
            self.target.initialize()

    def test_connection_errors_are_redacted_without_sqlite_fallback(self) -> None:
        fallback = self.path.parent / "must-not-be-created.sqlite3"
        app = create_app(
            Settings(database_path=fallback, database_url=self.target.database_url)
        )
        with TestClient(app) as client:
            with (
                patch.object(
                    app.state.database,
                    "transaction",
                    side_effect=psycopg.OperationalError(
                        "database-password-must-stay-private"
                    ),
                ),
                self.assertLogs("agent_permissions.api", level="ERROR"),
            ):
                response = client.get(
                    "/api/v1/users/me", headers={"Authorization": "Bearer sample-token"}
                )
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["error"]["code"], "SERVICE_UNAVAILABLE")
            self.assertNotIn("database-password-must-stay-private", response.text)
        self.assertFalse(fallback.exists())

    def test_cli_reads_private_postgres_configuration(self) -> None:
        config = self.path.parent / ".env"
        config.write_text(f"AGENT_USERS_DATABASE_URL={self.target.database_url}\n")
        config.chmod(0o600)
        output = io.StringIO()
        with redirect_stdout(output):
            result = cli_main(
                ["init-admin", "--username", "admin", "--env-file", str(config)]
            )
        self.assertEqual(result, 0)
        self.assertNotIn(self.target.database_url, output.getvalue())
        settings = Settings.from_env(config)
        self.assertNotIn(self.target.database_url, repr(settings))
        with TestClient(create_app(settings)) as client:
            response = client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "12345"}
            )
            self.assertEqual(response.status_code, 200)

    def test_migration_cli_closes_target_on_success_and_repeated_import(self) -> None:
        self.source()
        config = self.path.parent / "migration.env"
        config.write_text(
            f"AGENT_USERS_DATABASE_URL={self.target.database_url}\n"
            "AGENT_USERS_DB_POOL_MIN_SIZE=1\nAGENT_USERS_DB_POOL_MAX_SIZE=1\n"
        )
        config.chmod(0o600)
        created = []

        def target_factory(*args, **kwargs):
            target = PostgresDatabase(*args, **kwargs)
            created.append(target)
            return target

        for expected in (0, 1):
            with (
                patch(
                    "agent_permissions.migrate_postgres.PostgresDatabase",
                    side_effect=target_factory,
                ),
                redirect_stdout(io.StringIO()) as output,
            ):
                result = migrate_main(
                    ["--source", str(self.path), "--env-file", str(config)]
                )
            self.assertEqual(result, expected)
            self.assertEqual(created[-1].pool_stats(), {})
            self.assertNotIn(self.target.database_url, output.getvalue())
        with self.target.transaction() as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM users").fetchone()[0], 2
            )


if __name__ == "__main__":
    unittest.main()
