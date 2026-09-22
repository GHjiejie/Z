"""Run with `uv run --group rag python -m llamaindex_service.workers`."""

import argparse
import logging
import signal
import threading
from typing import Any

from llamaindex_service.config import Settings
from llamaindex_service.persistence import Repository

from .runner import Worker


def require_worker_readiness(settings: Settings, repository: Any) -> None:
    """Production workers must use the dedicated restricted worker database role."""
    if settings.environment != "production":
        return
    try:
        ready = repository.health(worker=True)
    except Exception:  # noqa: BLE001 -- fail closed without exposing connection details.
        raise SystemExit(
            "Production worker database schema or role is not ready"
        ) from None
    if not ready:
        raise SystemExit("Production worker database schema or role is not ready")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Process durable knowledge ingestion jobs"
    )
    parser.add_argument(
        "--once", action="store_true", help="Process one queued job and exit"
    )
    parser.add_argument(
        "--maintenance-only",
        action="store_true",
        help="Run one bounded retention and orphan cleanup pass",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    settings = Settings()
    repository = Repository(settings)
    require_worker_readiness(settings, repository)
    worker = Worker(settings, repository)
    if args.maintenance_only:
        try:
            worker.maintenance.run_once()
        finally:
            worker.maintenance.close()
        return
    if args.once:
        worker.run_once()
        return
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    worker.run(stop)


if __name__ == "__main__":
    main()
