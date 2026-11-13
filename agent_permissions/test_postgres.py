"""Run shared API/RBAC contracts and migration checks against real PostgreSQL."""

import io
import os
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient
from psycopg import sql

from agent_permissions import test_api, test_rbac
from agent_permissions.__main__ import main as cli_main
from agent_permissions.api import create_app
from agent_permissions.config import Settings
from agent_permissions.migrate_postgres import TABLE_COLUMNS, fingerprint, migrate
from agent_permissions.models import UserCreate
from agent_permissions.postgres import PostgresDatabase

TEST_URL = os.getenv("AGENT_USERS_TEST_DATABASE_URL")


class PostgresFixture:
    def settings_for(self, path: Path) -> Settings:
        schema = f"test_agent_users_{uuid4().hex}"
        with psycopg.connect(TEST_URL, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
            )

        def cleanup() -> None:
            with psycopg.connect(TEST_URL, autocommit=True) as connection:
                connection.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )

        self.addCleanup(cleanup)
        parts = urlsplit(TEST_URL)
        url = urlunsplit(
            parts._replace(
                query=urlencode(
                    [
                        *parse_qsl(parts.query),
                        ("options", f"-csearch_path={schema},pg_catalog"),
                    ]
                )
            )
        )
        return Settings(database_path=path, database_url=url)


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
class PostgresMigrationTests(PostgresFixture, unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "source.sqlite3"
        self.target = PostgresDatabase(self.settings_for(self.path).database_url)

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

    def test_full_migration_preserves_hashes_sessions_relations_and_search(
        self,
    ) -> None:
        self.source()
        report = migrate(self.path, self.target)
        self.assertTrue(Path(report["backup"]).is_file())
        self.assertEqual(report["tables"]["users"]["rows"], 2)
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
                self.assertLogs("agent_permissions.service", level="ERROR"),
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


if __name__ == "__main__":
    unittest.main()
