"""Persistent ownership/receipts for snapshot objects, separate from publication.

An uncertain PUT is never automatically repeated. This is an object-write ledger,
not a durable clone/scan worker or a claim that unknown provider versions are gone.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import tempfile
import time
from pathlib import Path

from packages.auth.transactions import authorized_write, current_authority
from packages.coding.errors import CodingConflictError
from packages.domain.models import utc_now


def require_external_io(db):
    if db.in_transaction:
        raise CodingConflictError("Snapshot preparation must run outside database transactions")


class SnapshotObjects:
    def __init__(self, repositories):
        self.repositories = repositories
        self.db = repositories.db

    def materialize(self, content, context, permission, *, validate, wait_seconds=10):
        require_external_io(self.db)
        digest = hashlib.sha256(content).hexdigest()
        if self.repositories.archive_store and len(content) > 100 * 1024 * 1024:
            raise CodingConflictError("Snapshot exceeds the 100 MiB transfer limit")
        key = hashlib.sha256(self.db.encode([context.tenant_id, context.project_id, digest]).encode()).hexdigest()
        object_id, token = "repoobj_" + key, secrets.token_hex(32)
        deadline = time.monotonic() + wait_seconds
        while True:
            with authorized_write(self.db, context, permission) as current:
                validate(current)
                now = utc_now()
                self.db.execute("""INSERT INTO repository_objects
                    (id,tenant_id,project_id,archive_sha256,size_bytes,status,owner_token,created_by,created_at,updated_at)
                    VALUES (?,?,?,?,?,'UPLOADING',?,?,?,?) ON CONFLICT DO NOTHING""",
                    (object_id, current.tenant_id, current.project_id, digest, len(content), token, current.user_id, now, now))
                row = self.db.fetch_one("SELECT * FROM repository_objects WHERE id=?", (object_id,))
            if row['status'] == 'READY':
                self.repositories.read_archive(row)
                current_authority(self.db, context, permission)
                return row
            if row['owner_token'] == token:
                break
            if row['status'] == 'UNCERTAIN':
                raise CodingConflictError("Snapshot object write is uncertain and requires reconciliation; do not repeat the upload")
            if time.monotonic() >= deadline:
                raise CodingConflictError("Snapshot object is being prepared; retry later without creating another upload")
            # No connection or write transaction is retained while waiting.
            time.sleep(0.05)

        uri = None
        dispatched = False
        try:
            current_authority(self.db, context, permission)
            validate(context)
            dispatched = True
            uri = self._put(content, context)
            # Record the fixed-version receipt even if the initiator was revoked
            # during I/O. These are internal recovery facts, not user publication.
            self.db.execute("""UPDATE repository_objects SET archive_path=?,updated_at=?
                WHERE id=? AND owner_token=? AND status='UPLOADING'""", (str(uri), utc_now(), object_id, token))
            row.update(archive_path=str(uri))
            self.repositories.read_archive(row)
            changed = self.db.execute_count("""UPDATE repository_objects SET status='READY',updated_at=?
                WHERE id=? AND owner_token=? AND status='UPLOADING'""", (utc_now(), object_id, token))
            if changed != 1:
                raise CodingConflictError("Snapshot object write ownership changed")
            return self.db.fetch_one("SELECT * FROM repository_objects WHERE id=?", (object_id,))
        except BaseException:
            if not dispatched:
                self.db.execute("DELETE FROM repository_objects WHERE id=? AND owner_token=? AND status='UPLOADING'",
                                (object_id, token))
                raise
            # If receipt persistence fails, do not conceal the failure or release
            # the owner slot. A crash can leave UPLOADING; neither state is replayed.
            self.db.execute("""UPDATE repository_objects SET status='UNCERTAIN',archive_path=COALESCE(?,archive_path),updated_at=?
                WHERE id=? AND owner_token=? AND status='UPLOADING'""", (str(uri) if uri is not None else None, utc_now(), object_id, token))
            raise

    def _put(self, content, context):
        if self.repositories.archive_store:
            return self.repositories.archive_store.put(content, tenant_id=context.tenant_id,
                project_id=context.project_id, kind='repository')
        digest = hashlib.sha256(content).hexdigest()
        destination = self.repositories.storage_root / digest[:2] / (digest + '.tar.gz')
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Hard-link publication is atomic and never overwrites an existing file.
        # Other tenants may independently prepare the same local content digest.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.snapshot-', delete=False) as output:
                temporary = Path(output.name)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass
            directory = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return destination
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
