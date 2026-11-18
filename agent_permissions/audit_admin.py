"""Local operator commands for audit storage maintenance and delivery monitoring."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from agent_permissions.audit_delivery import audit_store
from agent_permissions.config import Settings
from agent_permissions.database import create_database


def main(argv=None):
    parser = argparse.ArgumentParser(description="Agent 审计运维")
    parser.add_argument("command", choices=["status", "init-store", "prune-store"])
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args(argv)
    settings = Settings.from_env(args.env_file)
    if args.command == "status":
        db = create_database(settings)
        db.open()
        try:
            with db.transaction() as conn:
                counts = {
                    r[0]: r[1]
                    for r in conn.execute(
                        "SELECT state,count(*) FROM agent_audit_outbox GROUP BY state"
                    )
                }
                oldest = conn.execute(
                    "SELECT min(created_at) FROM agent_audit_outbox WHERE state='pending'"
                ).fetchone()[0]
                unknown = conn.execute(
                    "SELECT count(*) FROM agent_audit_call_refs WHERE state='needs_reconciliation' OR state='executing'"
                ).fetchone()[0]
            print(
                json.dumps(
                    {
                        "pending": counts.get("pending", 0),
                        "quarantined": counts.get("quarantined", 0),
                        "oldest_pending_seconds": max(0, int(time.time() - oldest))
                        if oldest is not None
                        else 0,
                        "without_business_result": unknown,
                    }
                )
            )
        finally:
            db.close()
    else:
        store = audit_store(settings)
        try:
            store.initialize(managed=args.command == "prune-store")
            if args.command == "prune-store":
                removed = store.cleanup(
                    datetime.now(UTC), settings.audit_retention_days
                )
                print(json.dumps({"removed_events": removed}))
        finally:
            store.database.close()


if __name__ == "__main__":
    main()
