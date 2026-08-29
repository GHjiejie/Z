from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from packages.application.services import new_id
from packages.coding.errors import CodingConflictError, CodingNotFoundError
from packages.coding.models import RepositorySnapshotCreate, WorkspaceBinding
from packages.domain.models import TenantContext, utc_now
from packages.persistence import Database
from packages.repositories import RepositoryService
from packages.sandbox.manager import SandboxManager
from packages.sandbox.policy import SandboxPolicy


_ALLOWED_BINARY_SUFFIXES = {".gif", ".ico", ".jpeg", ".jpg", ".pdf", ".png", ".webp"}


class CodingService:
    def __init__(
        self,
        db: Database,
        repositories: RepositoryService,
        sandbox_manager: SandboxManager,
    ):
        self.db = db
        self.repositories = repositories
        self.sandbox_manager = sandbox_manager

    def bind_thread(
        self,
        thread_id: str,
        binding: WorkspaceBinding,
        context: TenantContext,
        *,
        lifecycle: str = "thread_scoped",
        ttl_seconds: int = 86400,
    ) -> Dict[str, Any]:
        thread = self.db.fetch_one(
            "SELECT * FROM threads WHERE id=? AND tenant_id=? AND project_id=?",
            (thread_id, context.tenant_id, context.project_id),
        )
        if not thread:
            raise CodingNotFoundError("Thread not found")
        if self.db.fetch_one("SELECT id FROM coding_workspaces WHERE thread_id=?", (thread_id,)):
            raise CodingConflictError("Thread already has a coding workspace")
        snapshot = self.repositories.create_snapshot(
            binding.repository_id,
            RepositorySnapshotCreate(
                requested_ref=binding.base_ref,
                source_mode=binding.source_mode,
            ),
            context,
        )
        workspace_id = new_id("ws")
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_seconds)
        self.db.execute(
            """INSERT INTO coding_workspaces
               (id, tenant_id, project_id, thread_id, repository_snapshot_id,
                lifecycle, workspace_generation, status, created_at, updated_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, 'UNPROVISIONED', ?, ?, ?)""",
            (
                workspace_id,
                context.tenant_id,
                context.project_id,
                thread_id,
                snapshot["id"],
                lifecycle,
                now.isoformat(),
                now.isoformat(),
                expires.isoformat(),
            ),
        )
        self.db.execute(
            """UPDATE threads SET repository_id=?, repository_snapshot_id=?, updated_at=?
               WHERE id=?""",
            (binding.repository_id, snapshot["id"], utc_now(), thread_id),
        )
        return self.get_thread_workspace(thread_id, context)

    def get_thread_workspace(self, thread_id: str, context: TenantContext) -> Dict[str, Any]:
        workspace = self.db.fetch_one(
            """SELECT w.*, s.resolved_commit_sha, s.requested_ref, s.source_mode,
                      s.manifest_hash, s.file_count, s.size_bytes,
                      r.id AS repository_id, r.name AS repository_name,
                      r.provider AS repository_provider
               FROM coding_workspaces w
               JOIN repository_snapshots s ON s.id=w.repository_snapshot_id
               JOIN repositories r ON r.id=s.repository_id
               WHERE w.thread_id=? AND w.tenant_id=? AND w.project_id=?""",
            (thread_id, context.tenant_id, context.project_id),
        )
        if not workspace:
            raise CodingNotFoundError("Coding workspace not found")
        if workspace.get("sandbox_instance_id"):
            sandbox = self.db.fetch_one(
                "SELECT * FROM sandbox_instances WHERE id=?",
                (workspace["sandbox_instance_id"],),
            )
            if sandbox:
                sandbox.pop("external_id", None)
                metadata = sandbox.get("provider_metadata") or {}
                sandbox["provider_metadata"] = {
                    key: metadata[key]
                    for key in ("image_id", "source_sha256", "source_commit_sha")
                    if key in metadata
                }
            workspace["sandbox"] = sandbox
        return workspace

    def get_run_workspace(self, run_id: str, context: TenantContext) -> Dict[str, Any]:
        run = self.db.fetch_one(
            "SELECT * FROM runs WHERE id=? AND tenant_id=? AND project_id=?",
            (run_id, context.tenant_id, context.project_id),
        )
        if not run or not run.get("coding_workspace_id"):
            raise CodingNotFoundError("Run has no coding workspace")
        return self.get_thread_workspace(run["thread_id"], context)

    async def tree(self, run_id: str, context: TenantContext) -> Dict[str, Any]:
        workspace = self.get_run_workspace(run_id, context)
        backend = await self.sandbox_manager.raw_backend_for_workspace(workspace)
        result = backend.glob("*", "/workspace/repo")
        matches = result.matches or []
        items = []
        for match in matches:
            if hasattr(match, "path"):
                path = match.path
            elif isinstance(match, dict):
                path = match.get("path", str(match))
            else:
                path = str(match)
            if "/.git/" in path or path.endswith("/.git"):
                continue
            items.append(
                {
                    "path": path,
                    "name": path.rsplit("/", 1)[-1],
                    "type": "file",
                }
            )
        return {
            "workspace_id": workspace["id"],
            "workspace_generation": workspace["workspace_generation"],
            "items": sorted(items, key=lambda item: item["path"]),
            "truncated": bool(getattr(result, "truncated", False)),
        }

    async def file(self, run_id: str, path: str, context: TenantContext) -> Dict[str, Any]:
        workspace = self.get_run_workspace(run_id, context)
        backend = await self.sandbox_manager.raw_backend_for_workspace(workspace)
        sandbox_profile = (workspace.get("sandbox") or {}).get("profile") or {}
        workspace_root = sandbox_profile.get("workspace_root", "/workspace/repo")
        policy = SandboxPolicy(workspace_root=workspace_root, protected_paths=())
        candidate = path if path.startswith("/") else f"{workspace_root}/{path}"
        normalized = policy.authorize_path(candidate, "read")
        if not normalized.startswith(workspace_root + "/"):
            raise CodingConflictError("File path is outside the coding workspace")
        resolver = getattr(backend, "resolve_path", None)
        if resolver is not None:
            resolved = str(resolver(normalized))
            if not resolved.startswith(workspace_root + "/"):
                raise CodingConflictError("File path resolves outside the coding workspace")
        result = backend.download_files([normalized])[0]
        error = getattr(result, "error", None)
        content = getattr(result, "content", None)
        if error or content is None:
            raise CodingNotFoundError("Workspace file not found")
        if len(content) > 2_000_000:
            raise CodingConflictError("Workspace file exceeds the 2 MiB API limit")
        try:
            text = content.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            suffix = normalized.rsplit("/", 1)[-1].lower()
            suffix = "." + suffix.rsplit(".", 1)[-1] if "." in suffix else ""
            if suffix not in _ALLOWED_BINARY_SUFFIXES:
                raise CodingConflictError("Workspace binary file type is not previewable")
            text = base64.b64encode(content).decode("ascii")
            encoding = "base64"
        return {
            "workspace_id": workspace["id"],
            "workspace_generation": workspace["workspace_generation"],
            "path": normalized,
            "encoding": encoding,
            "content": text,
            "size_bytes": len(content),
        }

    def diff(self, run_id: str, context: TenantContext) -> Dict[str, Any]:
        self.get_run_workspace(run_id, context)
        change_set = self.db.fetch_one(
            """SELECT * FROM change_sets WHERE run_id=? ORDER BY created_at DESC LIMIT 1""",
            (run_id,),
        )
        if not change_set:
            return {"run_id": run_id, "status": "PENDING", "patch": "", "changed_files": []}
        patch = self.db.fetch_one(
            "SELECT content FROM artifacts WHERE id=?", (change_set["patch_artifact_id"],)
        )
        return {**change_set, "patch": patch["content"] if patch else ""}

    def verification(self, run_id: str, context: TenantContext) -> Dict[str, Any]:
        self.get_run_workspace(run_id, context)
        report = self.db.fetch_one(
            "SELECT * FROM verification_reports WHERE run_id=?", (run_id,)
        )
        if not report:
            return {"run_id": run_id, "status": "PENDING", "checks": [], "summary": {}}
        return report

    def change_sets(self, run_id: str, context: TenantContext) -> List[Dict[str, Any]]:
        self.get_run_workspace(run_id, context)
        return self.db.fetch_all(
            "SELECT * FROM change_sets WHERE run_id=? ORDER BY created_at DESC", (run_id,)
        )

    def decide_change_set(
        self,
        run_id: str,
        change_set_id: str,
        approved: bool,
        context: TenantContext,
        message: str | None = None,
    ) -> Dict[str, Any]:
        self.get_run_workspace(run_id, context)
        change_set = self.db.fetch_one(
            "SELECT * FROM change_sets WHERE id=? AND run_id=?", (change_set_id, run_id)
        )
        if not change_set:
            raise CodingNotFoundError("ChangeSet not found")
        if change_set["status"] in {"DELIVERED", "REJECTED"}:
            raise CodingConflictError("ChangeSet has already been decided")
        if approved:
            patch = self.db.fetch_one(
                "SELECT * FROM artifacts WHERE id=?",
                (change_set["patch_artifact_id"],),
            )
            if not patch or hashlib.sha256(patch["content"].encode()).hexdigest() != patch[
                "content_hash"
            ]:
                raise CodingConflictError("ChangeSet patch artifact failed integrity validation")
            if (
                patch.get("plan_hash") != change_set.get("plan_hash")
                or patch.get("base_commit_sha") != change_set.get("base_commit_sha")
                or int(patch.get("workspace_generation", -1))
                != int(change_set["workspace_generation"])
            ):
                raise CodingConflictError("ChangeSet patch metadata does not match its review record")
        next_status = "DELIVERED" if approved else "REJECTED"
        self.db.execute(
            "UPDATE change_sets SET status=? WHERE id=?", (next_status, change_set_id)
        )
        self.sandbox_manager.events.append(
            run_id,
            "changeset.delivered" if approved else "changeset.rejected",
            {
                "changeset_id": change_set_id,
                "actor": context.user_id,
                "message": message,
            },
        )
        return self.db.fetch_one("SELECT * FROM change_sets WHERE id=?", (change_set_id,))
