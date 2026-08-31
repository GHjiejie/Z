from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from packages.coding.errors import CodingConflictError
from packages.knowledge.ports import ObjectStorage


class SharedArchiveStore:
    """Content-addressed, tenant-scoped snapshots pinned to an object version."""

    def __init__(self, storage: ObjectStorage):
        if storage.provider == "local":
            raise ValueError("Shared archives require a versioned object store")
        self.storage = storage

    @staticmethod
    def _prefix(tenant_id: str, project_id: str) -> str:
        tenant = hashlib.sha256(tenant_id.encode()).hexdigest()
        project = hashlib.sha256(project_id.encode()).hexdigest()
        return f"platform-snapshots/{tenant}/{project}/"

    def put(self, content: bytes, *, tenant_id: str, project_id: str, kind: str) -> str:
        if kind not in {"repository", "workspace"}:
            raise ValueError("Invalid snapshot kind")
        if len(content) > 100 * 1024 * 1024:
            raise CodingConflictError("Snapshot exceeds the 100 MiB transfer limit")
        digest = hashlib.sha256(content).hexdigest()
        key = self._prefix(tenant_id, project_id) + f"{kind}/{digest}"
        metadata = self.storage.put_content(key, content, "application/octet-stream")
        if not metadata.version_id or metadata.version_id == 'null':
            raise CodingConflictError("Shared snapshot storage requires object versioning")
        if metadata.size_bytes != len(content):
            raise CodingConflictError("Shared snapshot size attestation mismatch")
        if metadata.bucket != self.storage.bucket or metadata.object_key != key:
            raise CodingConflictError("Shared snapshot receipt scope mismatch")
        query = urlencode({"version": metadata.version_id})
        return f"snapshot-object://{quote(self.storage.bucket, safe='')}/{key}?{query}"

    def read(self, record: dict, *, kind: str) -> bytes:
        uri = urlsplit(record["archive_path"])
        expected_prefix = self._prefix(record["tenant_id"], record["project_id"]) + kind + "/"
        key = uri.path.removeprefix("/")
        query = parse_qs(uri.query)
        digest = record["archive_sha256"]
        if (
            uri.scheme != "snapshot-object" or uri.netloc != self.storage.bucket
            or uri.fragment or uri.username or uri.password
            or set(query) != {"version"} or len(query["version"]) != 1
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or key != expected_prefix + digest
        ):
            raise CodingConflictError(
                "Snapshot object scope is invalid or uses an unmigrated local archive"
            )
        content = self.storage.get_content(key, query["version"][0])
        if len(content) != record["size_bytes"] or hashlib.sha256(content).hexdigest() != digest:
            raise CodingConflictError("Shared snapshot archive hash or size mismatch")
        return content
