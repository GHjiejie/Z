"""Offline administrative operations; keep database credentials server-side."""

import argparse
import json

from llamaindex_service.config import Settings
from llamaindex_service.contracts import AuthContext


def main() -> None:
    parser = argparse.ArgumentParser(description="Knowledge service administration")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Apply the service schema using a migration account")
    member = sub.add_parser(
        "add-member", help="Provision a verified JWT subject membership"
    )
    member.add_argument("--tenant", required=True)
    member.add_argument("--project", required=True)
    member.add_argument("--user", required=True)
    member.add_argument("--role", action="append", default=[])
    sub.add_parser("health")
    args = parser.parse_args()
    from llamaindex_service.persistence import Repository

    repository = Repository(Settings())
    if args.command == "migrate":
        repository.create_schema()
        print(json.dumps({"status": "migrated"}))
    elif args.command == "add-member":
        repository.ensure_membership(
            AuthContext(args.tenant, args.project, args.user, tuple(args.role))
        )
        print(json.dumps({"status": "provisioned", "user_id": args.user}))
    else:
        print(json.dumps({"ready": repository.health()}))


if __name__ == "__main__":
    main()
