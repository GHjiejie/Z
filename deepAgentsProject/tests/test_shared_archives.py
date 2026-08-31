from __future__ import annotations

import hashlib
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

from packages.coding.errors import CodingConflictError
from packages.coding.models import RepositoryCreate, RepositorySnapshotCreate
from packages.content_security import NoopContentScanner
from packages.domain.models import TenantContext
from packages.knowledge.ports import ObjectMetadata
from packages.persistence import Database
from packages.persistence.archive_store import SharedArchiveStore
from packages.persistence.migrate_archives import migrate_archives
from packages.repositories.service import RepositoryService


class VersionedStore:
    provider = "versioned-test"
    bucket = "test-archives"
    region = "test"

    def __init__(self):
        self.objects = {}
        self.last = None

    def put_content(self, key, content, content_type):
        version = str(len(self.objects) + 1)
        self.objects[(key, version)] = content
        self.last = (key, version)
        return ObjectMetadata(self.bucket, self.region, key, len(content), content_type, version_id=version)

    def get_content(self, key, version_id=None):
        assert version_id, "Never read the mutable latest version"
        return self.objects[(key, version_id)]


def test_archive_is_version_pinned_scope_checked_and_digest_verified():
    storage = VersionedStore()
    archives = SharedArchiveStore(storage)
    content = b"immutable source"
    uri = archives.put(content, tenant_id="tenant", project_id="project", kind="repository")
    row = {
        "archive_path": uri, "tenant_id": "tenant", "project_id": "project",
        "archive_sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content),
    }
    parts = urlsplit(uri)
    storage.put_content(parts.path.removeprefix("/"), b"new mutable version", "application/octet-stream")
    assert archives.read(row, kind="repository") == content
    with pytest.raises(CodingConflictError, match="scope"):
        archives.read({**row, "tenant_id": "other"}, kind="repository")
    with pytest.raises(CodingConflictError, match="scope"):
        archives.read(row, kind="workspace")
    version = parse_qs(parts.query)["version"][0]
    storage.objects[(parts.path.removeprefix("/"), version)] = b"corrupted"
    with pytest.raises(CodingConflictError, match="hash or size"):
        archives.read(row, kind="repository")


def test_unversioned_shared_store_is_rejected(monkeypatch):
    storage = VersionedStore()
    put = storage.put_content
    monkeypatch.setattr(storage, "put_content", lambda *args: replace(put(*args), version_id=None))
    with pytest.raises(CodingConflictError, match="versioning"):
        SharedArchiveStore(storage).put(b"data", tenant_id="tenant", project_id="project", kind="repository")


def test_other_worker_reads_shared_source_and_legacy_migration_is_resumable(tmp_path):
    db = Database(str(tmp_path / "archives.db"))
    db.initialize()
    context = TenantContext(tenant_id="tenant", project_id="project")
    storage = VersionedStore()
    shared = SharedArchiveStore(storage)
    source = tmp_path / "repository"
    source.mkdir()
    (source / "README.md").write_text("fixture repository")
    local_root = tmp_path / "node-a"
    first = RepositoryService(db, local_root, [source])
    repository = first.create_repository(
        RepositoryCreate(name="archive fixture", provider="local_snapshot", canonical_uri=str(source)), context,
    )
    try:
        snapshot = first.create_snapshot(repository["id"], RepositorySnapshotCreate(), context)
        row = db.fetch_one("SELECT * FROM repository_snapshots WHERE id=?", (snapshot["id"],))
        original_path = row["archive_path"]
        original = first.read_archive(row)
        preview = migrate_archives(db, shared, local_root, NoopContentScanner())
        assert preview == {"local_archives": 1, "migrated": 0, "shared_archives": 0}
        assert storage.objects == {}
        applied = migrate_archives(db, shared, local_root, NoopContentScanner(), apply=True)
        assert applied["migrated"] == 1
        repeated = migrate_archives(db, shared, local_root, NoopContentScanner(), apply=True)
        assert repeated == {"local_archives": 0, "migrated": 0, "shared_archives": 1}
        # A different Worker directory never contains node A's archives.
        second = RepositoryService(db, tmp_path / "node-b", [], archive_store=shared)
        migrated = db.fetch_one("SELECT * FROM repository_snapshots WHERE id=?", (snapshot["id"],))
        assert second.read_archive(migrated) == original
        assert second.get_snapshot(snapshot["id"], context)["archive_uri"] == f"repository-snapshot://{snapshot['id']}"
        from pathlib import Path

        assert Path(original_path).is_file(), "Migration preserves recoverable originals"
        (source / "README.md").write_text("new source snapshot")
        publisher = RepositoryService(db, local_root, [source], archive_store=shared)
        new_snapshot = publisher.create_snapshot(repository["id"], RepositorySnapshotCreate(), context)
        new_row = db.fetch_one("SELECT * FROM repository_snapshots WHERE id=?", (new_snapshot["id"],))
        assert new_row["archive_path"].startswith("snapshot-object://")
        assert second.read_archive(new_row)
    finally:
        db.close()
