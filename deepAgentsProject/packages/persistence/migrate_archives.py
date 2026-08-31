from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

from packages.content_security import ContentScanner, create_content_scanner
from packages.persistence import Database, create_database
from packages.persistence.archive_store import SharedArchiveStore
from packages.secrets import read_secret


def migrate_archives(
    db: Database, archive_store: SharedArchiveStore, legacy_root: Path,
    scanner: ContentScanner, *, apply: bool = False,
) -> dict[str, int]:
    """Offline, resumable migration; never deletes original snapshot archives."""
    active = db.fetch_one(
        """SELECT id FROM runs WHERE status NOT IN
           ('CANCELLED','TIMED_OUT','FAILED','FAILED_BUDGET','SUCCEEDED') LIMIT 1"""
    )
    if active:
        raise RuntimeError("Drain or cancel active runs and stop API/Workers before archive migration")
    root = legacy_root.resolve(strict=True)
    result = {"local_archives": 0, "migrated": 0, "shared_archives": 0}
    for table, kind in (("repository_snapshots", "repository"), ("workspace_snapshots", "workspace")):
        for record in db.fetch_all(f"SELECT * FROM {table} ORDER BY created_at"):
            if record["archive_path"].startswith("snapshot-object://"):
                result["shared_archives"] += 1
                continue
            source = Path(record["archive_path"]).resolve(strict=True)
            if root not in source.parents or not source.is_file():
                raise RuntimeError("Legacy archive is outside the explicitly supplied data directory")
            with source.open("rb") as stream:
                content = stream.read(100 * 1024 * 1024 + 1)
            if len(content) > 100 * 1024 * 1024:
                raise RuntimeError("Legacy archive exceeds the shared snapshot transfer limit")
            if len(content) != record["size_bytes"] or hashlib.sha256(content).hexdigest() != record["archive_sha256"]:
                raise RuntimeError("Legacy archive hash or size verification failed")
            result["local_archives"] += 1
            if not apply:
                continue
            scanner.scan(content, object_name=f"archive-migration/{record['id']}")
            uri = archive_store.put(content, tenant_id=record["tenant_id"], project_id=record["project_id"], kind=kind)
            archive_store.read({**record, "archive_path": uri}, kind=kind)
            updated = db.execute_count(
                f"UPDATE {table} SET archive_path=? WHERE id=? AND archive_path=?",
                (uri, record["id"], record["archive_path"]),
            )
            if updated != 1:
                raise RuntimeError("Archive metadata changed concurrently; stop services before retrying")
            result["migrated"] += 1
    return result


def main() -> int:
    from packages.knowledge.storage import create_object_storage

    parser = argparse.ArgumentParser(description="Migrate local snapshots into versioned shared object storage")
    parser.add_argument("--legacy-data-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Write objects and metadata; default is read-only validation")
    arguments = parser.parse_args()
    production = os.getenv("DEEPAGENT_ENVIRONMENT") == "production"
    location = read_secret("DATABASE_URL", production=production) or os.getenv("DEEPAGENT_DB_PATH")
    if not location:
        parser.error("DATABASE_URL_FILE or DEEPAGENT_DB_PATH is required")
    db = create_database(location)
    try:
        db.initialize(auto_migrate=False)
        storage = create_object_storage(arguments.legacy_data_root / "unused-local-store")
        if storage.provider == "local":
            parser.error("Configure a versioned shared object store, not local storage")
        report = migrate_archives(
            db, SharedArchiveStore(storage), arguments.legacy_data_root,
            create_content_scanner(production=production), apply=arguments.apply,
        )
        print(report)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
