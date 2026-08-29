from __future__ import annotations

import gzip
import hashlib
import ipaddress
import io
import json
import os
import subprocess
import socket
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from packages.application.services import new_id
from packages.coding.errors import (
    CodingConflictError,
    CodingNotFoundError,
    RepositoryAccessError,
)
from packages.coding.models import RepositoryCreate, RepositorySnapshotCreate
from packages.domain.models import TenantContext, utc_now
from packages.persistence import Database


_EXCLUDED_NAMES = {".git", ".DS_Store", "node_modules", "__pycache__", ".pytest_cache"}
_SECRET_NAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}
_MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024


class RepositoryService:
    """Registers repositories and creates immutable, content-addressed source snapshots.

    Local paths are accepted only under explicitly configured roots. Git commands are
    platform-authored argument arrays; repository content and model output are never
    interpolated into a shell command.
    """

    def __init__(
        self,
        db: Database,
        storage_root: Path,
        allowed_local_roots: Iterable[Path],
    ):
        self.db = db
        self.storage_root = storage_root.resolve()
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.allowed_local_roots = [root.resolve() for root in allowed_local_roots]

    def list_repositories(self, context: TenantContext) -> List[Dict[str, Any]]:
        items = self.db.fetch_all(
            """SELECT * FROM repositories WHERE tenant_id=? AND project_id=?
               ORDER BY updated_at DESC""",
            (context.tenant_id, context.project_id),
        )
        for item in items:
            item["snapshot_count"] = self.db.fetch_one(
                "SELECT COUNT(*) AS count FROM repository_snapshots WHERE repository_id=?",
                (item["id"],),
            )["count"]
        return items

    def create_repository(
        self, payload: RepositoryCreate, context: TenantContext
    ) -> Dict[str, Any]:
        if payload.credential_ref:
            raise RepositoryAccessError(
                "Repository credentials are not available in patch_only MVP; register a local or public read-only repository"
            )
        canonical_uri = payload.canonical_uri
        if payload.provider.value == "local_snapshot":
            canonical_uri = str(self._resolve_local_path(canonical_uri))
        else:
            self._validate_remote_uri(canonical_uri)
        existing = self.db.fetch_one(
            """SELECT id FROM repositories WHERE tenant_id=? AND project_id=? AND name=?""",
            (context.tenant_id, context.project_id, payload.name),
        )
        if existing:
            raise CodingConflictError(f"Repository name already exists: {payload.name}")
        repository_id = new_id("repo")
        now = utc_now()
        self.db.execute(
            """INSERT INTO repositories
               (id, tenant_id, project_id, name, provider, canonical_uri, default_branch,
                credential_ref, access_policy_revision_id, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)""",
            (
                repository_id,
                context.tenant_id,
                context.project_id,
                payload.name,
                payload.provider.value,
                canonical_uri,
                payload.default_branch,
                payload.credential_ref,
                payload.access_policy_revision_id,
                now,
                now,
            ),
        )
        return self.get_repository(repository_id, context)

    def get_repository(self, repository_id: str, context: TenantContext) -> Dict[str, Any]:
        repository = self.db.fetch_one(
            """SELECT * FROM repositories WHERE id=? AND tenant_id=? AND project_id=?""",
            (repository_id, context.tenant_id, context.project_id),
        )
        if not repository:
            raise CodingNotFoundError("Repository not found")
        return repository

    def probe(self, repository_id: str, context: TenantContext) -> Dict[str, Any]:
        repository = self.get_repository(repository_id, context)
        with self._repository_checkout(repository) as checkout:
            commit = self._git(checkout, "rev-parse", "--verify", "HEAD^{commit}").strip()
            branch = self._git(checkout, "branch", "--show-current").strip() or None
            dirty = bool(self._git(checkout, "status", "--porcelain").strip())
        return {
            "repository_id": repository_id,
            "status": "healthy",
            "resolved_commit_sha": commit,
            "branch": branch,
            "dirty": dirty,
            "checked_at": utc_now(),
        }

    def create_snapshot(
        self,
        repository_id: str,
        payload: RepositorySnapshotCreate,
        context: TenantContext,
    ) -> Dict[str, Any]:
        repository = self.get_repository(repository_id, context)
        requested_ref = payload.requested_ref or repository["default_branch"]
        with self._repository_checkout(repository) as checkout:
            if payload.source_mode == "working_tree_snapshot" and repository["provider"] != "local_snapshot":
                raise CodingConflictError(
                    "working_tree_snapshot is available only for local repositories"
                )
            resolved_commit = self._git(
                checkout, "rev-parse", "--verify", f"{requested_ref}^{{commit}}"
            ).strip()
            if payload.source_mode == "committed_ref":
                archive, manifest = self._archive_committed(checkout, resolved_commit)
            else:
                archive, manifest = self._archive_working_tree(checkout)

        content_hash = hashlib.sha256(archive).hexdigest()
        manifest_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        manifest_hash = hashlib.sha256(manifest_json.encode()).hexdigest()
        archive_path = self.storage_root / content_hash[:2] / f"{content_hash}.tar.gz"
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        if not archive_path.exists():
            archive_path.write_bytes(archive)

        existing = self.db.fetch_one(
            """SELECT * FROM repository_snapshots
               WHERE repository_id=? AND resolved_commit_sha=? AND source_mode=?
                 AND manifest_hash=?""",
            (repository_id, resolved_commit, payload.source_mode, manifest_hash),
        )
        if existing:
            return self.get_snapshot(existing["id"], context)
        snapshot_id = new_id("snap")
        now = utc_now()
        self.db.execute(
            """INSERT INTO repository_snapshots
               (id, repository_id, tenant_id, project_id, requested_ref,
                resolved_commit_sha, source_mode, manifest_hash, archive_path,
                archive_sha256, size_bytes, file_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_id,
                repository_id,
                context.tenant_id,
                context.project_id,
                requested_ref,
                resolved_commit,
                payload.source_mode,
                manifest_hash,
                str(archive_path),
                content_hash,
                len(archive),
                len(manifest),
                now,
            ),
        )
        return self.get_snapshot(snapshot_id, context)

    def get_snapshot(self, snapshot_id: str, context: TenantContext) -> Dict[str, Any]:
        snapshot = self.db.fetch_one(
            """SELECT * FROM repository_snapshots
               WHERE id=? AND tenant_id=? AND project_id=?""",
            (snapshot_id, context.tenant_id, context.project_id),
        )
        if not snapshot:
            raise CodingNotFoundError("Repository snapshot not found")
        path = Path(snapshot["archive_path"])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != snapshot["archive_sha256"]:
            raise CodingConflictError("Repository snapshot archive is missing or corrupted")
        public_snapshot = dict(snapshot)
        public_snapshot.pop("archive_path", None)
        public_snapshot["archive_uri"] = f"repository-snapshot://{snapshot['id']}"
        return public_snapshot

    def read_archive(self, snapshot: Dict[str, Any]) -> bytes:
        path = Path(snapshot["archive_path"])
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != snapshot["archive_sha256"]:
            raise CodingConflictError("Repository snapshot archive hash mismatch")
        return data

    def _resolve_local_path(self, value: str) -> Path:
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise RepositoryAccessError("Local repository path does not exist or is not a directory")
        if not any(path == root or root in path.parents for root in self.allowed_local_roots):
            raise RepositoryAccessError("Local repository path is outside configured roots")
        return path

    @staticmethod
    def _validate_remote_uri(value: str) -> None:
        parsed = urlparse(value)
        if parsed.scheme not in {"https", "ssh"}:
            raise RepositoryAccessError("Remote repository URI must use HTTPS or SSH")
        if parsed.username or parsed.password:
            raise RepositoryAccessError("Credentials must not be embedded in repository URI")
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if not hostname or hostname == "localhost" or hostname.endswith(".local"):
            raise RepositoryAccessError("Remote repository host is not allowed")
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(
                    hostname,
                    parsed.port or (443 if parsed.scheme == "https" else 22),
                    type=socket.SOCK_STREAM,
                )
            }
        except OSError as exc:
            raise RepositoryAccessError("Remote repository host cannot be resolved") from exc
        if not addresses or any(
            not ipaddress.ip_address(address).is_global for address in addresses
        ):
            raise RepositoryAccessError(
                "Remote repository host resolves to a non-public network"
            )

    class _Checkout:
        def __init__(self, service: "RepositoryService", repository: Dict[str, Any]):
            self.service = service
            self.repository = repository
            self.temporary: Optional[tempfile.TemporaryDirectory[str]] = None
            self.path: Optional[Path] = None

        def __enter__(self) -> Path:
            if self.repository["provider"] == "local_snapshot":
                self.path = self.service._resolve_local_path(self.repository["canonical_uri"])
                return self.path
            self.temporary = tempfile.TemporaryDirectory(prefix="deepagent-repo-")
            self.path = Path(self.temporary.name) / "repo"
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "GIT_TERMINAL_PROMPT": "0",
                "HOME": self.temporary.name,
            }
            result = subprocess.run(
                [
                    "git",
                    "clone",
                    "--filter=blob:none",
                    "--no-tags",
                    "--",
                    self.repository["canonical_uri"],
                    str(self.path),
                ],
                capture_output=True,
                text=True,
                timeout=180,
                env=env,
                check=False,
            )
            if result.returncode:
                raise RepositoryAccessError(
                    f"Unable to clone repository: {result.stderr.strip()[:500]}"
                )
            return self.path

        def __exit__(self, *_: object) -> None:
            if self.temporary:
                self.temporary.cleanup()

    def _repository_checkout(self, repository: Dict[str, Any]) -> "RepositoryService._Checkout":
        return self._Checkout(self, repository)

    @staticmethod
    def _git(path: Path, *arguments: str) -> str:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": tempfile.gettempdir(),
            "LANG": "C.UTF-8",
        }
        result = subprocess.run(
            ["git", "-C", str(path), *arguments],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            check=False,
        )
        if result.returncode:
            raise CodingConflictError(
                f"Git operation failed ({arguments[0]}): {result.stderr.strip()[:500]}"
            )
        return result.stdout

    def _archive_committed(self, path: Path, commit: str) -> tuple[bytes, List[Dict[str, Any]]]:
        result = subprocess.run(
            ["git", "-C", str(path), "archive", "--format=tar", commit],
            capture_output=True,
            timeout=180,
            check=False,
        )
        if result.returncode:
            raise CodingConflictError(
                f"Unable to archive repository: {result.stderr.decode(errors='replace')[:500]}"
            )
        if len(result.stdout) > _MAX_SNAPSHOT_BYTES:
            raise CodingConflictError("Committed repository snapshot exceeds 512 MiB")
        archive = self._normalize_tar(result.stdout)
        return archive, self._manifest_from_archive(archive)

    def _archive_working_tree(self, path: Path) -> tuple[bytes, List[Dict[str, Any]]]:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            timeout=120,
            check=False,
        )
        if result.returncode:
            raise CodingConflictError("Unable to enumerate working tree")
        names = [item for item in result.stdout.split(b"\0") if item]
        buffer = io.BytesIO()
        total = 0
        manifest: List[Dict[str, Any]] = []
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as tar:
                for raw_name in sorted(names):
                    relative = PurePosixPath(raw_name.decode("utf-8", errors="strict"))
                    if self._excluded(relative):
                        continue
                    source = (path / Path(*relative.parts)).resolve()
                    if not source.is_file() or not (source == path or path in source.parents):
                        continue
                    content = source.read_bytes()
                    total += len(content)
                    if total > _MAX_SNAPSHOT_BYTES:
                        raise CodingConflictError("Working tree snapshot exceeds 512 MiB")
                    self._add_tar_bytes(tar, str(relative), content, source.stat().st_mode)
                    manifest.append(
                        {
                            "path": str(relative),
                            "sha256": hashlib.sha256(content).hexdigest(),
                            "size": len(content),
                        }
                    )
        return buffer.getvalue(), manifest

    @staticmethod
    def _normalize_tar(raw_tar: bytes) -> bytes:
        source = io.BytesIO(raw_tar)
        target = io.BytesIO()
        with tarfile.open(fileobj=source, mode="r:") as incoming:
            with gzip.GzipFile(fileobj=target, mode="wb", mtime=0) as zipped:
                with tarfile.open(fileobj=zipped, mode="w") as outgoing:
                    total = 0
                    for member in incoming.getmembers():
                        path = PurePosixPath(member.name)
                        if member.isdir() or RepositoryService._excluded(path):
                            continue
                        if not member.isfile():
                            continue
                        extracted = incoming.extractfile(member)
                        if extracted is None:
                            continue
                        content = extracted.read()
                        total += len(content)
                        if total > _MAX_SNAPSHOT_BYTES:
                            raise CodingConflictError(
                                "Committed repository snapshot exceeds 512 MiB"
                            )
                        RepositoryService._add_tar_bytes(
                            outgoing, str(path), content, member.mode
                        )
        return target.getvalue()

    @staticmethod
    def _manifest_from_archive(archive: bytes) -> List[Dict[str, Any]]:
        manifest: List[Dict[str, Any]] = []
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in sorted(tar.getmembers(), key=lambda item: item.name):
                if not member.isfile():
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                content = extracted.read()
                manifest.append(
                    {
                        "path": member.name,
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size": len(content),
                    }
                )
        return manifest

    @staticmethod
    def _add_tar_bytes(tar: tarfile.TarFile, name: str, content: bytes, mode: int) -> None:
        info = tarfile.TarInfo(name=name)
        info.size = len(content)
        info.mode = mode & 0o777
        info.uid = 10001
        info.gid = 10001
        info.uname = "coder"
        info.gname = "coder"
        info.mtime = 0
        tar.addfile(info, io.BytesIO(content))

    @staticmethod
    def _excluded(path: PurePosixPath) -> bool:
        environment_template = path.name in {
            ".env.example",
            ".env.sample",
            ".env.template",
        }
        return (
            any(part in _EXCLUDED_NAMES for part in path.parts)
            or path.name in _SECRET_NAMES
            or (path.name.startswith(".env.") and not environment_template)
        )
