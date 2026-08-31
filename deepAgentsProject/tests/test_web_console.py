from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apps.platform_api.web_console import mount_web_console


@pytest.fixture
def console(tmp_path):
    build = tmp_path / "build"
    assets = build / "assets"
    assets.mkdir(parents=True)
    (build / "index.html").write_text("<html>console entrypoint</html>")
    (assets / "app.js").write_text("window.consoleReady = true;")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-build-canary")
    (assets / "linked.txt").symlink_to(outside)
    (assets / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    app = FastAPI()

    @app.get("/api/v1/private")
    def private():
        raise HTTPException(401)

    mount_web_console(app, build)
    with TestClient(app) as client:
        yield client, build


@pytest.mark.parametrize("path", [
    "/%2e%2e/outside.txt", "/..%2foutside.txt", "/%252e%252e%252foutside.txt",
    "/advanced/%2e%2e/outside.txt", "/advanced/..%5coutside.txt",
    "/assets/..%2f..%2foutside.txt", "/assets/%252e%252e/outside.txt",
    "/assets/linked.txt", "/assets/linked-dir/outside.txt",
    "/api/v1/missing", "/.env", "/outside.txt",
])
def test_console_never_serves_files_outside_build(console, path):
    client, _ = console
    response = client.get(path)
    assert response.status_code == 404
    assert "outside-build-canary" not in response.text


@pytest.mark.parametrize("path", ["/", "/index.html", "/login", "/playground", "/knowledge", "/advanced/runs/run_123"])
def test_console_deep_links_assets_and_api_keep_their_contract(console, path):
    client, _ = console
    response = client.get(path)
    assert response.status_code == 200
    assert response.text == "<html>console entrypoint</html>"
    assert response.headers["cache-control"] == "no-cache"
    assert client.head(path).status_code == 200
    assert client.get("/assets/app.js").text == "window.consoleReady = true;"
    assert client.get("/api/v1/private").status_code == 401


def test_entrypoint_symlink_cannot_escape_build(console):
    client, build = console
    (build / "index.html").unlink()
    (build / "index.html").symlink_to(build.parent / "outside.txt")
    assert client.get("/playground").status_code == 404


def test_asset_root_cannot_be_a_symlink_outside_build(tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    (build / "index.html").write_text("console")
    (build / "assets").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="inside the frontend build"):
        mount_web_console(FastAPI(), build)
