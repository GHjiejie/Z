from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional
from contextlib import nullcontext

from packages.application.services import new_id
from packages.content_security import ContentRejectedError, ContentScanner, NoopContentScanner
from packages.coding.errors import (
    CodingConflictError,
    CodingNotFoundError,
    RepositoryAccessError,
)
from packages.coding.models import RepositoryCreate, RepositorySnapshotCreate
from packages.domain.models import TenantContext, utc_now
from packages.persistence import Database
from packages.persistence.archive_store import SharedArchiveStore
from packages.repositories.network import RepositoryNetworkPolicy, git_command, git_environment, run_clone
from packages.repositories.tunnel import RepositoryTunnel
from packages.repositories.materialization import SnapshotObjects, require_external_io
from packages.auth.permissions import Permission
from packages.auth.transactions import authorized_write, current_authority


_EXCLUDED_NAMES = {
    ".git",
    ".DS_Store",
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".azure",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
}
_SECRET_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_ed25519",
}
_MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
_MAX_SNAPSHOT_FILES = 100_000


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
        content_scanner: ContentScanner | None = None,
        archive_store: SharedArchiveStore | None = None,
        network_policy: RepositoryNetworkPolicy | None = None,
    ):
        self.db = db
        self.archive_store = archive_store
        self.storage_root = storage_root.resolve()
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.allowed_local_roots = [root.resolve() for root in allowed_local_roots]
        self.content_scanner = content_scanner or NoopContentScanner()
        self.network_policy = network_policy or RepositoryNetworkPolicy.from_environment()

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

    def browse_local_folders(self, value: Optional[str] = None) -> Dict[str, Any]:
        """Browse selectable Git folders without escaping configured local roots."""
        if not self.allowed_local_roots:
            raise RepositoryAccessError("No local repository roots are configured")
        current = self._resolve_local_path(value or str(self.allowed_local_roots[0]))
        current_root = next(
            root
            for root in self.allowed_local_roots
            if current == root or root in current.parents
        )
        parent = current.parent if current != current_root else None
        items: List[Dict[str, Any]] = []
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise RepositoryAccessError("Local folder cannot be read") from exc
        for child in children:
            if len(items) >= 500 or child.name in _EXCLUDED_NAMES or child.is_symlink():
                continue
            try:
                resolved = child.resolve()
                if not resolved.is_dir() or not (
                    resolved == current_root or current_root in resolved.parents
                ):
                    continue
                items.append(
                    {
                        "name": child.name,
                        "path": str(resolved),
                        "is_git_repository": self._has_git_root_within(
                            resolved, current_root
                        ),
                    }
                )
            except OSError:
                continue
        is_git_repository, default_branch = self._git_metadata(current)
        return {
            "roots": [str(root) for root in self.allowed_local_roots],
            "current_path": str(current),
            "parent_path": str(parent) if parent is not None else None,
            "current": {
                "name": current.name or str(current),
                "path": str(current),
                "is_git_repository": is_git_repository,
                "default_branch": default_branch,
            },
            "items": items,
            "truncated": len(items) >= 500,
        }

    def create_repository(
        self, payload: RepositoryCreate, context: TenantContext
    ) -> Dict[str, Any]:
        require_external_io(self.db)
        context = current_authority(self.db, context, Permission.REPOSITORY_MANAGE)
        if payload.credential_ref:
            raise RepositoryAccessError(
                "Repository credentials are not available in patch_only MVP; register a local or public read-only repository"
            )
        canonical_uri = payload.canonical_uri
        if payload.provider.value == "local_snapshot":
            canonical_uri = str(self._resolve_local_path(canonical_uri))
        else:
            self._validate_remote_uri(canonical_uri)
        repository_id = new_id("repo")
        now = utc_now()
        with authorized_write(self.db, context, Permission.REPOSITORY_MANAGE) as context:
            changed = self.db.execute_count(
            """INSERT INTO repositories
               (id, tenant_id, project_id, name, provider, canonical_uri, default_branch,
                credential_ref, access_policy_revision_id, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?) ON CONFLICT DO NOTHING""",
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
            if not changed:
                raise CodingConflictError(f"Repository name already exists: {payload.name}")
            self._audit(context, 'repository.created', repository_id, {})
            return self.get_repository(repository_id, context)

    def get_repository(self, repository_id: str, context: TenantContext) -> Dict[str, Any]:
        context = current_authority(self.db, context, Permission.REPOSITORY_READ)
        repository = self.db.fetch_one(
            """SELECT * FROM repositories WHERE id=? AND tenant_id=? AND project_id=?""",
            (repository_id, context.tenant_id, context.project_id),
        )
        if not repository:
            raise CodingNotFoundError("Repository not found")
        return repository

    def probe(self, repository_id: str, context: TenantContext) -> Dict[str, Any]:
        require_external_io(self.db)
        context = current_authority(self.db, context, Permission.REPOSITORY_MANAGE)
        repository = self.get_repository(repository_id, context)
        self._validate_repository(repository, context)
        result = self._probe(repository_id, context)
        current_authority(self.db, context, Permission.REPOSITORY_MANAGE)
        self._validate_repository(repository, context)
        return result

    def _probe(self, repository_id: str, context: TenantContext) -> Dict[str, Any]:
        repository = self.get_repository(repository_id, context)
        with self._repository_checkout(repository) as checkout:
            boundary = checkout if repository["provider"] != "local_snapshot" else None
            is_git_repository, _ = self._git_metadata(checkout, checkout_root=boundary)
            if not is_git_repository:
                return {
                    "repository_id": repository_id,
                    "status": "healthy",
                    "resolved_commit_sha": None,
                    "branch": None,
                    "dirty": None,
                    "version_controlled": False,
                    "checked_at": utc_now(),
                }
            commit = self._git(checkout, "rev-parse", "--verify", "HEAD^{commit}").strip()
            branch = self._git(checkout, "branch", "--show-current").strip() or None
            dirty = bool(self._git(checkout, "status", "--porcelain").strip()) if boundary is None else False
        return {
            "repository_id": repository_id,
            "status": "healthy",
            "resolved_commit_sha": commit,
            "branch": branch,
            "dirty": dirty,
            "version_controlled": True,
            "checked_at": utc_now(),
        }

    def create_snapshot(
        self,
        repository_id: str,
        payload: RepositorySnapshotCreate,
        context: TenantContext,
    ) -> Dict[str, Any]:
        return self._create_snapshot(repository_id, payload, context, Permission.REPOSITORY_MANAGE)

    def create_runtime_snapshot(self, repository_id, payload, context):
        # Runtime members may bind a registered repository; that does not grant
        # repository registration or the public management snapshot endpoint.
        return self._create_snapshot(repository_id, payload, context, Permission.RUNTIME_USE)

    def _create_snapshot(self, repository_id, payload, context, permission):
        require_external_io(self.db)
        context = current_authority(self.db, context, permission)
        repository = self.get_repository(repository_id, context)
        self._validate_repository(repository, context)
        requested_ref = payload.requested_ref or repository["default_branch"]
        source_mode = payload.source_mode
        with self._repository_checkout(repository) as checkout:
            boundary = checkout if repository["provider"] != "local_snapshot" else None
            if source_mode == "working_tree_snapshot" and repository["provider"] != "local_snapshot":
                raise CodingConflictError(
                    "working_tree_snapshot is available only for local repositories"
                )
            is_git_repository, _ = self._git_metadata(checkout, checkout_root=boundary)
            if not is_git_repository:
                if repository["provider"] != "local_snapshot":
                    raise CodingConflictError("Remote repository is not a Git repository")
                archive, manifest = self._archive_directory(checkout)
                resolved_commit = hashlib.sha256(archive).hexdigest()
                requested_ref = "working-directory"
                source_mode = "working_tree_snapshot"
            else:
                resolved_commit = self._git(
                    checkout, "rev-parse", "--verify", "--end-of-options", f"{requested_ref}^{{commit}}"
                ).strip()
                if source_mode == "committed_ref":
                    archive, manifest = self._archive_committed(checkout, resolved_commit, checkout_root=boundary)
                else:
                    archive, manifest = self._archive_working_tree(checkout)

        try:
            self.content_scanner.scan(
                archive, object_name=f"repository/{repository_id}/{requested_ref}"
            )
        except ContentRejectedError as exc:
            raise RepositoryAccessError(str(exc)) from exc
        content_hash = hashlib.sha256(archive).hexdigest()
        manifest_json = json.dumps({'files': manifest, 'archive_sha256': content_hash}, sort_keys=True, separators=(",", ":"))
        manifest_hash = hashlib.sha256(manifest_json.encode()).hexdigest()
        current_authority(self.db, context, permission)
        existing = self.db.fetch_one(
            """SELECT * FROM repository_snapshots
               WHERE repository_id=? AND resolved_commit_sha=? AND source_mode=?
                 AND archive_sha256=?""",
            (repository_id, resolved_commit, source_mode, content_hash),
        )
        if existing:
            self.read_archive(existing)
            with authorized_write(self.db, context, permission) as context:
                self._validate_repository(repository, context, lock=True)
                return self._public_snapshot(existing)
        materialized = SnapshotObjects(self).materialize(archive, context, permission,
            validate=lambda current: self._validate_repository(repository, current))
        snapshot_id = new_id("snap")
        now = utc_now()
        with authorized_write(self.db, context, permission) as context:
            self._validate_repository(repository, context, lock=True)
            # One repository row serializes concurrent publication; storage I/O
            # and fixed-version verification were completed before taking it.
            existing = self.db.fetch_one("""SELECT * FROM repository_snapshots
                WHERE repository_id=? AND resolved_commit_sha=? AND source_mode=? AND archive_sha256=?""",
                (repository_id, resolved_commit, source_mode, content_hash))
            if existing:
                return self._public_snapshot(existing)
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
                source_mode,
                manifest_hash,
                materialized['archive_path'],
                content_hash,
                len(archive),
                len(manifest),
                now,
            ),
            )
            self._audit(context, 'repository.snapshot.created', snapshot_id,
                        {'repository_id': repository_id, 'archive_sha256': content_hash})
            return self.snapshot_metadata(snapshot_id, context)

    def _validate_repository(self, original, context, *, lock=False):
        if lock and self.db.dialect == 'postgresql':
            self.db.fetch_one('SELECT id FROM repositories WHERE id=? FOR UPDATE', (original['id'],))
        current = self.get_repository(original['id'], context)
        fields = ('provider', 'canonical_uri', 'default_branch', 'credential_ref', 'access_policy_revision_id')
        if current['status'] != 'ACTIVE' or any(current[key] != original[key] for key in fields):
            raise CodingConflictError('Repository is disabled or changed during preparation')

    def _audit(self, context, action, resource_id, details):
        self.db.execute("""INSERT INTO governance_audit_events
            (id,tenant_id,project_id,actor_user_id,action,resource_id,details_json,created_at)
            VALUES (?,?,?,?,?,?,?,?)""", (new_id('audit'), context.tenant_id, context.project_id,
            context.user_id, action, resource_id, self.db.encode(details), utc_now()))

    @staticmethod
    def _public_snapshot(snapshot):
        public_snapshot = dict(snapshot)
        public_snapshot.pop('archive_path', None)
        public_snapshot['archive_uri'] = f"repository-snapshot://{snapshot['id']}"
        return public_snapshot

    def snapshot_metadata(self, snapshot_id, context):
        context = current_authority(self.db, context, Permission.REPOSITORY_READ)
        snapshot = self.db.fetch_one('SELECT * FROM repository_snapshots WHERE id=? AND tenant_id=? AND project_id=?',
                                    (snapshot_id, context.tenant_id, context.project_id))
        if not snapshot:
            raise CodingNotFoundError('Repository snapshot not found')
        return self._public_snapshot(snapshot)

    def get_snapshot(self, snapshot_id: str, context: TenantContext) -> Dict[str, Any]:
        current_authority(self.db, context, Permission.REPOSITORY_READ)
        snapshot = self.db.fetch_one(
            """SELECT * FROM repository_snapshots
               WHERE id=? AND tenant_id=? AND project_id=?""",
            (snapshot_id, context.tenant_id, context.project_id),
        )
        if not snapshot:
            raise CodingNotFoundError("Repository snapshot not found")
        self.read_archive(snapshot)
        current_authority(self.db, context, Permission.REPOSITORY_READ)
        return self._public_snapshot(snapshot)

    def read_archive(self, snapshot: Dict[str, Any]) -> bytes:
        if self.archive_store:
            return self.archive_store.read(snapshot, kind="repository")
        path = Path(snapshot["archive_path"]).resolve()
        if self.storage_root not in path.parents or not path.is_file():
            raise CodingConflictError("Repository snapshot archive is missing or outside storage")
        with path.open("rb") as source:
            data = source.read(_MAX_SNAPSHOT_BYTES + 1)
        if len(data) > _MAX_SNAPSHOT_BYTES:
            raise CodingConflictError("Repository snapshot archive exceeds the size limit")
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

    def _git_metadata(self, path: Path, *, checkout_root: Path | None = None) -> tuple[bool, Optional[str]]:
        env = git_environment(Path(tempfile.gettempdir()))
        try:
            root = subprocess.run(
                git_command("-C", str(path), "rev-parse", "--show-toplevel"),
                capture_output=True,
                text=True,
                timeout=3,
                env=env,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False, None
        if root.returncode:
            return False, None
        git_root = Path(root.stdout.strip()).resolve()
        if not (checkout_root is not None and git_root == checkout_root) and not any(
            git_root == allowed or allowed in git_root.parents
            for allowed in self.allowed_local_roots
        ):
            return False, None
        if not self._git_storage_allowed(path, checkout_root=checkout_root):
            raise RepositoryAccessError("Git metadata or shared object directory is outside configured roots")
        try:
            branch = subprocess.run(
                git_command("-C", str(path), "branch", "--show-current"),
                capture_output=True,
                text=True,
                timeout=3,
                env=env,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return True, "main"
        return True, branch.stdout.strip() or "main"

    @staticmethod
    def _has_git_root_within(path: Path, allowed_root: Path) -> bool:
        candidate = path
        while candidate == allowed_root or allowed_root in candidate.parents:
            if (candidate / ".git").exists():
                return True
            if candidate == allowed_root:
                break
            candidate = candidate.parent
        return False

    def _git_context(self, path: Path, *, checkout_root: Path | None = None) -> tuple[Path, Optional[PurePosixPath]]:
        git_root = Path(self._git(path, "rev-parse", "--show-toplevel").strip()).resolve()
        if not (checkout_root is not None and git_root == checkout_root) and not any(
            git_root == allowed or allowed in git_root.parents
            for allowed in self.allowed_local_roots
        ):
            raise RepositoryAccessError("Git repository root is outside configured roots")
        if not self._git_storage_allowed(path, checkout_root=checkout_root):
            raise RepositoryAccessError("Git metadata or shared object directory is outside configured roots")
        prefix = None
        if path != git_root:
            prefix = PurePosixPath(path.relative_to(git_root).as_posix())
        return git_root, prefix

    def _git_storage_allowed(self, path: Path, *, checkout_root: Path | None = None) -> bool:
        result = subprocess.run(git_command("-C", str(path), "rev-parse", "--path-format=absolute",
                                           "--git-dir", "--git-common-dir"),
                                capture_output=True, text=True, timeout=3, check=False,
                                env=git_environment(Path(tempfile.gettempdir())))
        directories = result.stdout.splitlines()
        roots = [checkout_root] if checkout_root is not None else self.allowed_local_roots
        if result.returncode or len(directories) != 2:
            return False
        for value in directories:
            directory = Path(value).resolve()
            if not any(directory == root or root in directory.parents for root in roots):
                return False
            objects = (directory / "objects").resolve()
            if not any(objects == root or root in objects.parents for root in roots):
                return False
            # Alternates can point Git outside its metadata directory, including
            # otherwise private repositories. No implicit shared-object trust.
            alternates = directory / "objects" / "info" / "alternates"
            if alternates.exists() and alternates.stat().st_size:
                return False
        return True

    def _validate_remote_uri(self, value: str) -> None:
        self.network_policy.target(value)

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
            self.path = (Path(self.temporary.name) / "repo").resolve()
            try:
                target = self.service.network_policy.target(self.repository["canonical_uri"])
                transport = RepositoryTunnel(target) if target.scheme == "https" else nullcontext()
                with transport as tunnel:
                    command, env = self.service.network_policy.clone_command(target, self.path,
                        Path(self.temporary.name), tunnel=tunnel)
                    run_clone(command, env)
            except BaseException:
                # __exit__ is not called when __enter__ fails.
                self.temporary.cleanup()
                raise
            return self.path

        def __exit__(self, *_: object) -> None:
            if self.temporary:
                self.temporary.cleanup()

    def _repository_checkout(self, repository: Dict[str, Any]) -> "RepositoryService._Checkout":
        return self._Checkout(self, repository)

    @staticmethod
    def _git(path: Path, *arguments: str) -> str:
        env = git_environment(Path(tempfile.gettempdir()))
        result = subprocess.run(
            git_command("-C", str(path), *arguments),
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

    def _archive_committed(self, path: Path, commit: str, *, checkout_root: Path | None = None) -> tuple[bytes, List[Dict[str, Any]]]:
        git_root, prefix = self._git_context(path, checkout_root=checkout_root)
        command = git_command("-C", str(git_root), "archive", "--format=tar", commit)
        if prefix is not None:
            command.extend(["--", str(prefix)])
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=180,
            check=False,
            env=git_environment(Path(tempfile.gettempdir())),
        )
        if result.returncode:
            raise CodingConflictError(
                f"Unable to archive repository: {result.stderr.decode(errors='replace')[:500]}"
            )
        if len(result.stdout) > _MAX_SNAPSHOT_BYTES:
            raise CodingConflictError("Committed repository snapshot exceeds 512 MiB")
        archive = self._normalize_tar(result.stdout, strip_prefix=prefix)
        return archive, self._manifest_from_archive(archive)

    def _archive_working_tree(self, path: Path) -> tuple[bytes, List[Dict[str, Any]]]:
        git_root, prefix = self._git_context(path)
        pathspec = str(prefix) if prefix is not None else "."
        result = subprocess.run(
            git_command(
                "-C",
                str(git_root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                pathspec,
            ),
            capture_output=True,
            timeout=120,
            check=False,
            env=git_environment(Path(tempfile.gettempdir())),
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
                    repository_relative = PurePosixPath(
                        raw_name.decode("utf-8", errors="strict")
                    )
                    try:
                        relative = (
                            repository_relative.relative_to(prefix)
                            if prefix is not None
                            else repository_relative
                        )
                    except ValueError:
                        continue
                    if self._excluded(relative):
                        continue
                    source = (git_root / Path(*repository_relative.parts)).resolve()
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

    def _archive_directory(self, path: Path) -> tuple[bytes, List[Dict[str, Any]]]:
        buffer = io.BytesIO()
        total = 0
        file_count = 0
        manifest: List[Dict[str, Any]] = []
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as tar:
                for directory, directory_names, file_names in os.walk(
                    path, topdown=True, followlinks=False
                ):
                    current = Path(directory)
                    directory_names[:] = sorted(
                        name
                        for name in directory_names
                        if not (current / name).is_symlink()
                        and not self._excluded(
                            PurePosixPath(
                                (current / name).relative_to(path).as_posix()
                            )
                        )
                    )
                    for name in sorted(file_names):
                        source = current / name
                        relative = PurePosixPath(source.relative_to(path).as_posix())
                        if source.is_symlink() or self._excluded(relative):
                            continue
                        try:
                            resolved = source.resolve(strict=True)
                            if path not in resolved.parents or not resolved.is_file():
                                continue
                            stat = resolved.stat()
                            if stat.st_size > _MAX_SNAPSHOT_BYTES - total:
                                raise CodingConflictError(
                                    "Working directory snapshot exceeds 512 MiB"
                                )
                            content = resolved.read_bytes()
                        except OSError as exc:
                            raise RepositoryAccessError(
                                f"Unable to read local file: {relative}"
                            ) from exc
                        total += len(content)
                        file_count += 1
                        if file_count > _MAX_SNAPSHOT_FILES:
                            raise CodingConflictError(
                                "Working directory snapshot exceeds 100000 files"
                            )
                        self._add_tar_bytes(tar, str(relative), content, stat.st_mode)
                        manifest.append(
                            {
                                "path": str(relative),
                                "sha256": hashlib.sha256(content).hexdigest(),
                                "size": len(content),
                            }
                        )
        return buffer.getvalue(), manifest

    @staticmethod
    def _normalize_tar(
        raw_tar: bytes, strip_prefix: Optional[PurePosixPath] = None
    ) -> bytes:
        source = io.BytesIO(raw_tar)
        target = io.BytesIO()
        with tarfile.open(fileobj=source, mode="r:") as incoming:
            with gzip.GzipFile(fileobj=target, mode="wb", mtime=0) as zipped:
                with tarfile.open(fileobj=zipped, mode="w") as outgoing:
                    total = 0
                    for member in incoming.getmembers():
                        path = PurePosixPath(member.name)
                        if strip_prefix is not None:
                            try:
                                path = path.relative_to(strip_prefix)
                            except ValueError:
                                continue
                        if (
                            member.isdir()
                            or str(path) in {"", "."}
                            or RepositoryService._excluded(path)
                        ):
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
