from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles


SPA_ROOTS = frozenset({
    "", "login", "change-password", "security", "playground", "knowledge",
    "advanced", "agents", "coding", "runs", "approvals", "resources", "settings",
})


def mount_web_console(application: FastAPI, directory: Path) -> None:
    """Serve assets from the build and a fixed entrypoint for client routes.

    A client route is never interpreted as a filesystem path. StaticFiles also
    checks realpath containment, including symlinks, for assets and index.html.
    """
    root = directory.resolve()
    assets = root / "assets"
    if not (root / "index.html").is_file() or not assets.is_dir():
        return
    if not assets.resolve().is_relative_to(root):
        raise ValueError("Web assets must remain inside the frontend build")
    entrypoint = StaticFiles(directory=root, follow_symlink=False)
    application.mount("/assets", StaticFiles(directory=assets, follow_symlink=False), name="web-assets")

    @application.api_route("/{spa_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def web_console(request: Request, spa_path: str):
        parts = spa_path.split("/")
        if (
            any(part in {".", ".."} for part in parts)
            or any(character in spa_path for character in ("\\", "\x00", "%"))
            or (parts[0] not in SPA_ROOTS and spa_path != "index.html")
        ):
            raise HTTPException(status_code=404)
        response = await entrypoint.get_response("index.html", request.scope)
        response.headers["Cache-Control"] = "no-cache"
        return response
