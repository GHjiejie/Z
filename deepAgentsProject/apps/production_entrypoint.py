"""Strict entrypoint for immutable production images, not the development CLI."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


ROLES = ("api", "worker", "migrate", "sandbox-service")


def verify_release_runtime(role: str) -> None:
    if role not in ROLES:
        raise RuntimeError("Unsupported production process role")
    if os.getenv("DEEPAGENT_ENVIRONMENT", "").strip().lower() not in {"prod", "production"}:
        raise RuntimeError("Release images cannot run in development mode")
    if not sys.platform.startswith("linux") or os.geteuid() == 0:
        raise RuntimeError("Release images require an unprivileged Linux user")
    if not os.statvfs("/").f_flag & os.ST_RDONLY:
        raise RuntimeError("Release containers require a read-only root filesystem")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    if int(status.get("CapEff", "1").strip(), 16) or status.get("NoNewPrivs", "").strip() != "1":
        raise RuntimeError("Release containers require cap-drop ALL and no-new-privileges")
    root = Path(__file__).resolve().parents[1]
    for location in (root, root / "apps", root / "packages", root / "builtin_plugins", Path(sys.prefix)):
        if os.access(location, os.W_OK):
            raise RuntimeError("Release code and Python runtime must be read-only")
    if role != "sandbox-service" and (
        os.getenv("DOCKER_HOST") or os.getenv("CONTAINER_HOST")
        or any(Path(path).exists() for path in ("/var/run/docker.sock", "/run/docker.sock"))
    ):
        raise RuntimeError("Only the dedicated sandbox service may access a Docker daemon")


def command(role: str) -> list[str]:
    if role == "api":
        return [sys.executable, "-m", "uvicorn", "apps.platform_api.main:app", "--host", "0.0.0.0", "--port", "8000",
                "--workers", "1", "--no-proxy-headers", "--no-server-header", "--limit-concurrency", "128",
                "--timeout-graceful-shutdown", "30", "--no-access-log", "--log-config",
                str(Path(__file__).resolve().parent / 'logging.json')]
    module = {"worker": "apps.platform_worker.main", "migrate": "packages.persistence.migrate",
              "sandbox-service": "apps.sandbox_service.main"}.get(role)
    if module is None:
        raise ValueError("Unsupported production process role")
    return [sys.executable, "-m", module]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=ROLES)
    role = parser.parse_args().role
    os.environ['DEEPAGENT_PROCESS_ROLE'] = role
    verify_release_runtime(role)
    if role in {"api", "worker"}:
        os.environ["DEEPAGENT_PROCESS_ROLE"] = role
    os.execv(sys.executable, command(role))


if __name__ == "__main__":
    from packages.operations.logging import configure_logging
    import logging
    configure_logging()
    try:
        main()
    except Exception:
        logging.getLogger(__name__).exception('Release startup failed')
        raise SystemExit(1) from None
