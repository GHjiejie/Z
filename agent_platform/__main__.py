"""Run from the repository root: uv run python -m agent_platform serve."""

import argparse
import asyncio
import logging

from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.db import Database
from agent_platform.modules.platform import Platform


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Platform")
    parser.add_argument(
        "command", choices=["serve", "worker", "init"], nargs="?", default="serve"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    settings = Settings.from_env()
    if args.command == "serve":
        import uvicorn

        from agent_platform.apps.api.main import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port)
    else:
        db = Database(settings.database_url)
        db.create_schema()
        platform = Platform(db, settings)
        platform.bootstrap()
        if args.command == "worker":
            from agent_platform.apps.worker.main import Worker

            try:
                asyncio.run(Worker(platform).serve(asyncio.Event()))
            except KeyboardInterrupt:
                pass
        else:
            print("平台已初始化；新组织余额为零。")
        db.close()


if __name__ == "__main__":
    main()
