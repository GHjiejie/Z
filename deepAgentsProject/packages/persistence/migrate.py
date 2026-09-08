from __future__ import annotations

import argparse
import os
from pathlib import Path

from packages.persistence import create_database
from packages.secrets import read_secret


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply ordered DeepAgent platform database migrations"
    )
    parser.add_argument(
        "--database-url",
        default=read_secret("DATABASE_URL") or os.getenv("DEEPAGENT_DB_PATH"),
        help="PostgreSQL URL or SQLite path (defaults to DATABASE_URL/DEEPAGENT_DB_PATH)",
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("a database URL/path is required")
    database = create_database(args.database_url)
    try:
        database.initialize(auto_migrate=True)
        from packages.runtime.checkpoint_saver import FencedCheckpointSaver

        saver = FencedCheckpointSaver(
            database,
            None if database.dialect == "postgresql" else str(Path(args.database_url).with_suffix(".checkpoints.db")),
        )
        try:
            saver.initialize(auto_migrate=True)
        finally:
            saver.close()
        versions = database.schema_versions()
        print(f"database schema ready at version {versions[-1]}")
    finally:
        database.close()
    return 0


if __name__ == "__main__":
    from packages.operations.logging import configure_logging
    import logging
    configure_logging()
    try:
        raise SystemExit(main())
    except Exception:
        logging.getLogger(__name__).exception('Migration failed')
        raise SystemExit(1) from None
