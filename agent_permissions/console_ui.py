"""Serve the built Console from the API origin; frontend sources stay separate."""

from pathlib import Path

from fastapi import FastAPI
from starlette.responses import FileResponse, RedirectResponse
from starlette.staticfiles import StaticFiles

from agent_permissions.errors import ApiError


def mount_console(app: FastAPI, directory: Path | None = None) -> None:
    directory = directory or Path(__file__).resolve().parent / "console/dist"

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse("/console/", status_code=307)

    @app.get("/console", include_in_schema=False)
    def console_redirect() -> RedirectResponse:
        return RedirectResponse("/console/", status_code=307)

    @app.get("/console/", include_in_schema=False)
    def console_index() -> FileResponse:
        index = directory / "index.html"
        if not index.is_file():
            raise ApiError(503, "CONSOLE_NOT_BUILT", "管理界面暂不可用，请联系管理员")
        return FileResponse(index, media_type="text/html")

    # Source files and frontend configuration are never exposed by this mount.
    if (directory / "assets").is_dir():
        app.mount(
            "/console/assets",
            StaticFiles(directory=directory / "assets"),
            name="console-assets",
        )
