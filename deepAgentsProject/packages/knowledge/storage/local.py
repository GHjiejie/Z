from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from packages.knowledge.errors import KnowledgeStorageError
from packages.knowledge.ports import ObjectMetadata, UploadAuthorization


class LocalObjectStorage:
    """Filesystem-backed reference adapter with the same contract as OSS."""

    provider = "local"
    bucket = "deepagent-local"
    region = "local"

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def canonical_uri(self, object_key: str) -> str:
        return f"local://{self.bucket}/{object_key}"

    def create_upload_authorization(
        self, object_key: str, content_type: str, expires_seconds: int = 900
    ) -> UploadAuthorization:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_seconds)
        return UploadAuthorization(
            method="PUT",
            url=f"local://{object_key}",
            expires_at=expires_at.isoformat(),
            headers={"Content-Type": content_type},
        )

    def put_content(self, object_key: str, content: bytes, content_type: str) -> ObjectMetadata:
        path = self._path(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.uploading")
        temporary.write_bytes(content)
        temporary.replace(path)
        etag = hashlib.md5(content, usedforsecurity=False).hexdigest()  # nosec: OSS-compatible ETag only
        self._metadata_path(path).write_text(
            json.dumps({"content_type": content_type, "etag": etag}, ensure_ascii=False),
            encoding="utf-8",
        )
        return ObjectMetadata(
            bucket=self.bucket,
            region=self.region,
            object_key=object_key,
            size_bytes=len(content),
            content_type=content_type,
            etag=etag,
            version_id=None,
            storage_class="STANDARD",
        )

    def head_object(self, object_key: str, version_id: Optional[str] = None) -> ObjectMetadata:
        path = self._path(object_key)
        if not path.is_file():
            raise KnowledgeStorageError(f"Object does not exist: {object_key}")
        metadata = {}
        metadata_path = self._metadata_path(path)
        if metadata_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        return ObjectMetadata(
            bucket=self.bucket,
            region=self.region,
            object_key=object_key,
            size_bytes=path.stat().st_size,
            content_type=metadata.get("content_type", "application/octet-stream"),
            etag=metadata.get("etag"),
            version_id=None,
            storage_class="STANDARD",
        )

    def get_content(self, object_key: str, version_id: Optional[str] = None) -> bytes:
        path = self._path(object_key)
        if not path.is_file():
            raise KnowledgeStorageError(f"Object does not exist: {object_key}")
        return path.read_bytes()

    def create_download_url(
        self, object_key: str, version_id: Optional[str] = None, expires_seconds: int = 300
    ) -> Optional[str]:
        return None

    def _path(self, object_key: str) -> Path:
        candidate = (self.root / object_key).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise KnowledgeStorageError("Object key escapes the configured storage root")
        return candidate

    @staticmethod
    def _metadata_path(path: Path) -> Path:
        return path.with_name(f".{path.name}.metadata.json")
