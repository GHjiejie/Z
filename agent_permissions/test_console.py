"""Console hosting checks independent of Node and generated frontend assets."""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agent_permissions.api import create_app
from agent_permissions.config import Settings


class ConsoleHostingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.dist = self.directory / "dist"
        (self.dist / "assets").mkdir(parents=True)
        (self.dist / "index.html").write_text(
            '<!doctype html><html lang="zh-CN"><body>Console fixture</body></html>'
        )
        (self.dist / "assets/app.js").write_text("console.info('Console fixture');")
        self.client = self.enterContext(
            TestClient(
                create_app(
                    Settings(database_path=self.directory / "users.sqlite3"),
                    console_directory=self.dist,
                )
            )
        )

    def test_console_entry_and_redirects(self) -> None:
        for path in ("/", "/console"):
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 307)
            self.assertEqual(response.headers["location"], "/console/")
        response = self.client.get("/console/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Console fixture", response.text)

    def test_assets_public_but_source_and_paths_not_exposed(self) -> None:
        response = self.client.get("/console/assets/app.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        for path in (
            "/console/src/api.ts",
            "/console/package.json",
            "/console/assets/..%2f..%2fusers.sqlite3",
        ):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_missing_build_has_readable_error_and_api_stays_available(self) -> None:
        app = create_app(
            Settings(database_path=self.directory / "missing.sqlite3"),
            console_directory=self.directory / "not-built",
        )
        with TestClient(app) as client:
            response = client.get("/console/")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["error"]["code"], "CONSOLE_NOT_BUILT")
            self.assertEqual(client.get("/openapi.json").status_code, 200)

    def test_console_routes_do_not_change_api_contract(self) -> None:
        schema = self.client.get("/openapi.json").json()
        self.assertTrue(all(path.startswith("/api/v1/") for path in schema["paths"]))
