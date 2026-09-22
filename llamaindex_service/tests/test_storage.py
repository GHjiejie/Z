"""Storage tests use local temporary folders and an in-memory S3 double."""

import hashlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from llamaindex_service.config import Settings
from llamaindex_service.contracts import AuthContext, ServiceError
from llamaindex_service.persistence import Repository
from llamaindex_service.storage import LocalStorage, S3Storage
from llamaindex_service.workers.maintenance import Maintenance


class LocalStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = LocalStorage(self.directory.name, max_upload_bytes=1024)

    def test_roundtrip_digest_and_idempotent_delete(self):
        content = "# 企业知识库\n产品编号 DEV-2026".encode()
        info = self.storage.write(
            io.BytesIO(content), "manual.md", "text/markdown; charset=utf-8"
        )
        self.assertEqual(info.size_bytes, len(content))
        self.assertEqual(info.sha256, hashlib.sha256(content).hexdigest())
        self.assertNotIn("manual", info.object_key)
        with self.storage.open(info.object_key) as source:
            self.assertEqual(source.read(), content)
        with self.storage.materialize(info.object_key) as path:
            self.assertEqual(path.read_bytes(), content)
        self.storage.delete(info.object_key)
        self.storage.delete(info.object_key)
        with self.assertRaises(ServiceError) as raised:
            self.storage.open(info.object_key)
        self.assertEqual(raised.exception.code, "OBJECT_NOT_FOUND")

    def test_stream_reads_are_bounded_and_failed_upload_leaves_no_object(self):
        class Stream(io.BytesIO):
            def read(self, size=-1):
                if size < 0 or size > 65536:
                    raise AssertionError("Unbounded stream read")
                return super().read(size)

        with self.assertRaises(ServiceError) as raised:
            self.storage.write(Stream(b"a" * 1025), "limit.txt")
        self.assertEqual(raised.exception.status_code, 413)
        self.assertEqual(list((Path(self.directory.name) / "objects").iterdir()), [])
        self.storage.write(Stream(b"a" * 1024), "limit.txt")

    def test_filename_mime_signature_and_key_validation(self):
        samples = [
            ("../secret.txt", "text/plain", b"text", "INVALID_FILENAME"),
            ("evil.exe", None, b"text", "UNSUPPORTED_FILE_TYPE"),
            ("fake.pdf", None, b"plain text", "FILE_TYPE_MISMATCH"),
            ("fake.docx", None, b"plain text", "FILE_TYPE_MISMATCH"),
            ("fake.txt", None, b"PK\x03\x04", "FILE_TYPE_MISMATCH"),
            ("manual.txt", "application/pdf", b"text", "FILE_TYPE_MISMATCH"),
            ("empty.txt", None, b"", "EMPTY_FILE"),
        ]
        for name, content_type, content, code in samples:
            with self.subTest(name=name), self.assertRaises(ServiceError) as raised:
                self.storage.write(io.BytesIO(content), name, content_type)
            self.assertEqual(raised.exception.code, code)
        for key in ("../../secret", "/etc/passwd", "objects/../../a.txt"):
            with self.assertRaises(ServiceError):
                self.storage.open(key)

    def test_immutable_keys_and_symlink_refusal(self):
        with patch("llamaindex_service.storage.objects.uuid.uuid4") as identifier:
            identifier.return_value.hex = "a" * 32
            info = self.storage.write(io.BytesIO(b"first"), "first.txt")
            with self.assertRaises(ServiceError) as raised:
                self.storage.write(io.BytesIO(b"second"), "second.txt")
            self.assertEqual(raised.exception.code, "OBJECT_COLLISION")
            with self.storage.open(info.object_key) as source:
                self.assertEqual(source.read(), b"first")
        self.storage.delete(info.object_key)
        (Path(self.directory.name) / info.object_key).symlink_to("/etc/passwd")
        with self.assertRaises(ServiceError):
            self.storage.open(info.object_key)
        with self.assertRaises(ServiceError):
            self.storage.delete(info.object_key)


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.last_put = None

    def put_object(self, **kwargs):
        self.last_put = kwargs
        self.objects[kwargs["Key"]] = kwargs["Body"].read()

    def get_object(self, **kwargs):
        value = self.objects[kwargs["Key"]]
        return {"Body": io.BytesIO(value), "ContentLength": len(value)}

    def delete_object(self, **kwargs):
        self.objects.pop(kwargs["Key"], None)


class S3StorageTests(unittest.TestCase):
    def test_private_conditional_upload_and_materialize(self):
        client = FakeS3()
        storage = S3Storage("private-docs", max_upload_bytes=100, client=client)
        info = storage.write(io.BytesIO(b"document"), "doc.txt")
        self.assertEqual(client.last_put["IfNoneMatch"], "*")
        self.assertEqual(client.last_put["Metadata"]["sha256"], info.sha256)
        self.assertNotIn("ACL", client.last_put)
        with storage.materialize(info.object_key) as path:
            self.assertEqual(path.read_bytes(), b"document")
            temporary_path = path
        self.assertFalse(temporary_path.exists())
        storage.delete(info.object_key)
        storage.delete(info.object_key)
        self.assertEqual(client.objects, {})


class MaintenanceRepository:
    def __init__(self, references):
        self.references = references
        self.expiry_passes = 0

    def purge_expired_history(self):
        self.expiry_passes += 1
        return 2

    def delete_orphan_object(self, key, delete_callback):
        if key in self.references:
            return False
        delete_callback(key)
        return True


class MaintenanceTests(unittest.TestCase):
    def test_actual_repository_preserves_references_and_refuses_expired_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                _env_file=None,
                environment="test",
                model_mode="mock",
                database_url="sqlite:///:memory:",
                storage_path=directory,
            )
            repository = Repository(settings)
            repository.create_schema()
            self.addCleanup(repository.engine.dispose)
            context = AuthContext("tenant", "project", "user")
            repository.ensure_membership(context)
            knowledge_base = repository.create_kb(context, "Documents")
            storage = LocalStorage(directory)
            kept = storage.write(io.BytesIO(b"queued original"), "kept.txt")
            upload = lambda item, **kwargs: repository.upload_version(
                context,
                knowledge_base["id"],
                item.filename,
                item.object_key,
                item.sha256,
                item.size_bytes,
                item.content_type,
                **kwargs,
            )
            upload(kept, object_created_at=kept.created_at)
            orphan = storage.write(io.BytesIO(b"failed upload"), "orphan.txt")
            old = time.time() - 2 * 86400
            for item in (kept, orphan):
                os.utime(Path(directory) / item.object_key, (old, old))
            maintenance = Maintenance(repository, storage)
            try:
                self.assertEqual(maintenance.run_once()["deleted_objects"], 1)
                self.assertTrue((Path(directory) / kept.object_key).exists())
                self.assertFalse((Path(directory) / orphan.object_key).exists())
                with self.assertRaises(ServiceError) as raised:
                    upload(orphan, object_created_at=old)
                self.assertEqual(raised.exception.code, "UPLOAD_EXPIRED")
                self.assertEqual(repository.live_object_keys(), {kept.object_key})
            finally:
                maintenance.close()

    def test_grace_reference_checks_and_bounded_inventory_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalStorage(directory)
            recent = storage.write(io.BytesIO(b"upload still finalizing"), "recent.txt")
            referenced = storage.write(io.BytesIO(b"live document"), "live.txt")
            orphaned = [
                storage.write(io.BytesIO(b"abandoned upload"), f"old-{index}.txt")
                for index in range(3)
            ]
            old = time.time() - 2 * 86400
            for item in [referenced, *orphaned]:
                os.utime(Path(directory) / item.object_key, (old, old))
            repo = MaintenanceRepository({referenced.object_key})
            maintenance = Maintenance(repo, storage, scan_limit=2, delete_limit=1)
            try:
                deleted = 0
                for _ in range(7):
                    result = maintenance.run_once()
                    self.assertLessEqual(result["scanned_objects"], 2)
                    self.assertLessEqual(result["deleted_objects"], 1)
                    self.assertEqual(result["expired_messages"], 2)
                    deleted += result["deleted_objects"]
                self.assertEqual(deleted, 3)
                self.assertTrue((Path(directory) / recent.object_key).exists())
                self.assertTrue((Path(directory) / referenced.object_key).exists())
                self.assertGreater(repo.expiry_passes, 0)
            finally:
                maintenance.close()

    def test_reference_created_after_inventory_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalStorage(directory)
            item = storage.write(io.BytesIO(b"document"), "doc.txt")
            old = time.time() - 2 * 86400
            os.utime(Path(directory) / item.object_key, (old, old))
            repo = MaintenanceRepository(set())
            callback = repo.delete_orphan_object

            def commit_before_lock(key, delete_callback):
                repo.references.add(key)
                return callback(key, delete_callback)

            repo.delete_orphan_object = commit_before_lock
            maintenance = Maintenance(repo, storage)
            try:
                self.assertEqual(maintenance.run_once()["deleted_objects"], 0)
                self.assertTrue((Path(directory) / item.object_key).exists())
            finally:
                maintenance.close()

    def test_grace_cannot_be_reduced_below_safe_upload_window(self):
        with self.assertRaises(ValueError):
            Maintenance(None, None, grace_seconds=3600)


if __name__ == "__main__":
    unittest.main()
