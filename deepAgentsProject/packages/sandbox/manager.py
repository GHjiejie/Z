from __future__ import annotations

import hashlib
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

from deepagents.backends.protocol import SandboxBackendProtocol

from packages.adapters.harness.deepagents.governed_backend import GovernedSandboxBackend
from packages.application.services import new_id
from packages.coding.errors import CodingConflictError, SandboxUnavailableError
from packages.content_security import ContentRejectedError, ContentScanner, NoopContentScanner
from packages.domain.models import utc_now
from packages.persistence import Database
from packages.persistence.archive_store import SharedArchiveStore
from packages.repositories import RepositoryService
from packages.runtime.event_emitter import EventEmitter
from packages.sandbox.policy import SandboxPolicy
from packages.sandbox.ports import SandboxProvider, SandboxProvisionRequest, SandboxSnapshot
from packages.sandbox.recovery_archive import normalize_recovery_archive


@dataclass(frozen=True)
class BoundCodingWorkspace:
    workspace: Dict[str, Any]
    sandbox_instance: Dict[str, Any]
    backend: GovernedSandboxBackend
    skill_paths: list[str]
    recovery: Any = None


class SandboxManager:
    def __init__(
        self,
        db: Database,
        events: EventEmitter,
        repositories: RepositoryService,
        providers: Iterable[SandboxProvider],
        snapshot_root: Path,
        content_scanner: ContentScanner | None = None,
        archive_store: SharedArchiveStore | None = None,
    ):
        self.db = db
        self.archive_store = archive_store
        self.events = events
        self.repositories = repositories
        self.providers = {provider.name: provider for provider in providers}
        self.snapshot_root = snapshot_root.resolve()
        self.snapshot_root.mkdir(parents=True, exist_ok=True)
        self.content_scanner = content_scanner or NoopContentScanner()
        self._cleanup_task: asyncio.Task | None = None
        self._stop_cleanup = asyncio.Event()

    async def start(self) -> None:
        if self._cleanup_task is None:
            self._stop_cleanup.clear()
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        if self._cleanup_task is None:
            return
        self._stop_cleanup.set()
        self._cleanup_task.cancel()
        await asyncio.gather(self._cleanup_task, return_exceptions=True)
        self._cleanup_task = None

    async def _cleanup_loop(self) -> None:
        while not self._stop_cleanup.is_set():
            await self.destroy_expired()
            try:
                await asyncio.wait_for(self._stop_cleanup.wait(), timeout=60)
            except TimeoutError:
                continue

    async def bind(
        self,
        run: Dict[str, Any],
        plan: Dict[str, Any],
        *,
        recovery: Dict[str, Any] | None = None,
    ) -> BoundCodingWorkspace:
        workspace_id = run.get("coding_workspace_id")
        if not workspace_id:
            raise CodingConflictError("Coding run has no workspace binding")
        workspace = self.db.fetch_one(
            """SELECT * FROM coding_workspaces WHERE id=? AND tenant_id=? AND project_id=?""",
            (workspace_id, run["tenant_id"], run["project_id"]),
        )
        if not workspace:
            raise CodingConflictError("Coding workspace is unavailable")
        coding_profile = plan.get("coding_profile") or {}
        sandbox_profile = coding_profile.get("sandbox") or {}
        provider_name = sandbox_profile.get("provider", "docker")
        provider = self.providers.get(provider_name)
        if not provider:
            raise SandboxUnavailableError(f"Sandbox provider is not configured: {provider_name}")

        instance = None
        result = None
        if workspace.get("sandbox_instance_id"):
            instance = self.db.fetch_one(
                "SELECT * FROM sandbox_instances WHERE id=?",
                (workspace["sandbox_instance_id"],),
            )
            if instance and instance["status"] == "ACTIVE" and instance.get("external_id"):
                try:
                    if run["status"] == "ORPHANED" or recovery is not None:
                        await provider.destroy(instance["external_id"])
                        raise SandboxUnavailableError("Replacing a sandbox owned by an expired attempt")
                    result = await provider.resume(instance["external_id"], sandbox_profile)
                    self.events.append(
                        run["id"],
                        "workspace.ready",
                        {"workspace_id": workspace["id"], "resumed": True},
                    )
                except SandboxUnavailableError:
                    self.db.execute(
                        "UPDATE sandbox_instances SET status='LOST', updated_at=? WHERE id=?",
                        (utc_now(), instance["id"]),
                    )
                    result = None

        if result is None:
            snapshot = self.db.fetch_one(
                "SELECT * FROM repository_snapshots WHERE id=?",
                (workspace["repository_snapshot_id"],),
            )
            if not snapshot:
                raise CodingConflictError("Workspace repository snapshot is missing")
            instance_id = new_id("sbx")
            now = datetime.now(timezone.utc)
            expires = now + timedelta(seconds=int(sandbox_profile.get("ttl_seconds", 86400)))
            self.db.execute(
                """INSERT INTO sandbox_instances
                   (id, tenant_id, project_id, provider, profile_json, status,
                    created_at, updated_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, 'PROVISIONING', ?, ?, ?)""",
                (
                    instance_id,
                    run["tenant_id"],
                    run["project_id"],
                    provider_name,
                    self.db.encode(sandbox_profile),
                    now.isoformat(),
                    now.isoformat(),
                    expires.isoformat(),
                ),
            )
            self.db.execute(
                """UPDATE coding_workspaces SET status='PROVISIONING',
                   sandbox_instance_id=?, updated_at=?, expires_at=? WHERE id=?""",
                (instance_id, now.isoformat(), expires.isoformat(), workspace["id"]),
            )
            self.events.append(
                run["id"],
                "workspace.provisioning",
                {
                    "workspace_id": workspace["id"],
                    "sandbox_instance_id": instance_id,
                    "provider": provider_name,
                },
            )
            archive = self.repositories.read_archive(snapshot)
            try:
                result = await provider.provision(
                    SandboxProvisionRequest(
                        sandbox_instance_id=instance_id,
                        tenant_id=run["tenant_id"],
                        project_id=run["project_id"],
                        thread_id=run["thread_id"],
                        workspace_id=workspace["id"],
                        profile=sandbox_profile,
                        source_archive=archive,
                        source_sha256=snapshot["archive_sha256"],
                        base_commit_sha=snapshot["resolved_commit_sha"],
                    )
                )
            except Exception:
                self.db.execute(
                    "UPDATE sandbox_instances SET status='FAILED', updated_at=? WHERE id=?",
                    (utc_now(), instance_id),
                )
                self.db.execute(
                    "UPDATE coding_workspaces SET status='FAILED', updated_at=? WHERE id=?",
                    (utc_now(), workspace["id"]),
                )
                raise
            try:
                with self.db.transaction():
                    self.db.execute(
                        """UPDATE sandbox_instances SET external_id=?, provider_metadata_json=?, status='ACTIVE',
                           updated_at=? WHERE id=?""",
                        (result.external_id, self.db.encode(result.metadata), utc_now(), instance_id),
                    )
                    self.db.execute(
                        """UPDATE coding_workspaces SET status='READY', updated_at=? WHERE id=?""",
                        (utc_now(), workspace["id"]),
                    )
            except BaseException:
                discard = getattr(provider, 'discard_unpublished', None)
                if discard:
                    await asyncio.shield(discard(result.external_id, instance_id))
                else:
                    await asyncio.shield(provider.destroy(result.external_id))
                raise
            instance = self.db.fetch_one(
                "SELECT * FROM sandbox_instances WHERE id=?", (instance_id,)
            )
            workspace = self.db.fetch_one(
                "SELECT * FROM coding_workspaces WHERE id=?", (workspace["id"],)
            )
            if recovery is not None:
                record = recovery["snapshot"]
                content = recovery["content"]
                try:
                    await provider.restore(result.external_id, SandboxSnapshot(
                        content, record["archive_sha256"], record["size_bytes"],
                    ))
                    self.db.execute(
                        """UPDATE coding_workspaces SET workspace_generation=
                           CASE WHEN workspace_generation>? THEN workspace_generation+1 ELSE ?+1 END,
                           status='READY',
                           updated_at=? WHERE id=?""",
                        (record["workspace_generation"], record["workspace_generation"], utc_now(), workspace["id"]),
                    )
                    workspace = self.db.fetch_one("SELECT * FROM coding_workspaces WHERE id=?", (workspace["id"],))
                    self.db.execute("UPDATE runs SET workspace_generation=?, updated_at=? WHERE id=?", (
                        workspace["workspace_generation"], utc_now(), run["id"],
                    ))
                    self.events.append(run["id"], "workspace.recovered", {
                        "workspace_id": workspace["id"], "recovery_point_id": recovery["point"]["id"],
                        "workspace_snapshot_id": record["id"], "workspace_generation": workspace["workspace_generation"],
                        "source_workspace_generation": record["workspace_generation"],
                        "content_hash": record["archive_sha256"],
                    })
                except Exception:
                    self.db.execute("UPDATE sandbox_instances SET status='FAILED', updated_at=? WHERE id=?", (utc_now(), instance_id))
                    self.db.execute("UPDATE coding_workspaces SET status='FAILED', updated_at=? WHERE id=?", (utc_now(), workspace["id"]))
                    await provider.destroy(result.external_id)
                    raise
            else:
                await asyncio.to_thread(self._restore_latest_patch, run, plan, workspace, result.backend)
            self.events.append(
                run["id"],
                "workspace.ready",
                {
                    "workspace_id": workspace["id"],
                    "sandbox_instance_id": instance_id,
                    "provider": provider_name,
                    "resumed": False,
                },
            )

        if instance is None:
            instance = self.db.fetch_one(
                "SELECT * FROM sandbox_instances WHERE id=?",
                (workspace["sandbox_instance_id"],),
            )
        raw_backend = result.backend
        skill_paths = await asyncio.to_thread(
            self._materialize_skills, raw_backend, plan.get("skill_versions", [])
        )
        governed = GovernedSandboxBackend(
            raw_backend,
            policy=SandboxPolicy.from_plan(
                coding_profile,
                approval_mode=plan.get("approval_mode", "high_risk"),
            ),
            db=self.db,
            events=self.events,
            run=run,
            workspace=workspace,
        )
        return BoundCodingWorkspace(workspace, instance, governed, skill_paths)

    async def raw_backend_for_workspace(
        self, workspace: Dict[str, Any]
    ) -> SandboxBackendProtocol:
        instance = self.db.fetch_one(
            "SELECT * FROM sandbox_instances WHERE id=?",
            (workspace.get("sandbox_instance_id"),),
        )
        if not instance or instance["status"] != "ACTIVE" or not instance.get("external_id"):
            raise SandboxUnavailableError("Workspace sandbox is not active")
        provider = self.providers.get(instance["provider"])
        if not provider:
            raise SandboxUnavailableError("Workspace sandbox provider is unavailable")
        result = await provider.resume(instance["external_id"], instance["profile"])
        return result.backend

    async def snapshot_workspace(
        self,
        workspace: Dict[str, Any],
        *,
        run: Dict[str, Any],
        plan: Dict[str, Any],
        reason: str,
        recovery: bool = False,
    ) -> Dict[str, Any]:
        instance = self.db.fetch_one(
            "SELECT * FROM sandbox_instances WHERE id=?",
            (workspace.get("sandbox_instance_id"),),
        )
        if not instance or not instance.get("external_id"):
            raise SandboxUnavailableError("Workspace sandbox is not active")
        provider = self.providers[instance["provider"]]
        snapshot = await (provider.recovery_snapshot(instance["external_id"]) if recovery
                          else provider.snapshot(instance["external_id"]))
        prepared = await self.prepare_snapshot(workspace, run=run, plan=plan,
            snapshot=snapshot, reason=reason, recovery=recovery)
        with self.db.transaction():
            return self.record_snapshot(prepared)

    async def prepare_snapshot(self, workspace, *, run, plan, snapshot, reason, recovery=False):
        """Store validated bytes before opening a short metadata transaction."""
        instance = self.db.fetch_one('SELECT * FROM sandbox_instances WHERE id=?',
            (workspace.get('sandbox_instance_id'),))
        if not instance:
            raise SandboxUnavailableError('Workspace sandbox metadata is missing')
        if snapshot.size_bytes != len(snapshot.content) or hashlib.sha256(snapshot.content).hexdigest() != snapshot.sha256:
            raise CodingConflictError("Workspace snapshot digest or size mismatch")
        if recovery:
            await asyncio.to_thread(normalize_recovery_archive, snapshot.content)
        if snapshot.size_bytes > int(instance["profile"].get("disk_mb", 10240)) * 1024 * 1024:
            raise CodingConflictError("Workspace snapshot exceeds the configured disk budget")
        try:
            await asyncio.to_thread(
                self.content_scanner.scan,
                snapshot.content,
                object_name=f"workspace/{workspace['id']}",
            )
        except ContentRejectedError as exc:
            raise CodingConflictError(str(exc)) from exc
        snapshot_id = new_id("wsnap")
        if self.archive_store:
            path = await asyncio.to_thread(
                self.archive_store.put, snapshot.content,
                tenant_id=run["tenant_id"], project_id=run["project_id"], kind="workspace",
            )
        else:
            directory = self.snapshot_root / snapshot.sha256[:2]
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{snapshot.sha256}.tar"
            if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != snapshot.sha256:
                temporary = directory / (new_id('snapshot') + '.tmp')
                try:
                    temporary.write_bytes(snapshot.content)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
        repository_snapshot = self.db.fetch_one(
            "SELECT resolved_commit_sha FROM repository_snapshots WHERE id=?",
            (workspace["repository_snapshot_id"],),
        )
        if not repository_snapshot:
            raise CodingConflictError("Repository snapshot is missing")
        return {'id': snapshot_id, 'tenant_id': run['tenant_id'], 'project_id': run['project_id'],
                'run_id': run['id'], 'workspace_id': workspace['id'],
                'base_commit_sha': repository_snapshot['resolved_commit_sha'],
                'workspace_generation': workspace['workspace_generation'], 'plan_hash': plan['plan_hash'],
                'reason': reason, 'archive_path': str(path), 'archive_sha256': snapshot.sha256,
                'size_bytes': snapshot.size_bytes, 'created_at': utc_now()}

    def record_snapshot(self, result):
        columns = list(result)
        self.db.execute(f"INSERT INTO workspace_snapshots ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            tuple(result[column] for column in columns))
        self.events.append(
            result["run_id"],
            "workspace.snapshot.created",
            {
                "workspace_snapshot_id": result['id'],
                "workspace_id": result['workspace_id'],
                "workspace_generation": result['workspace_generation'],
                "content_hash": result['archive_sha256'],
                "size_bytes": result['size_bytes'],
                "reason": result['reason'],
            },
        )
        return result

    async def destroy_expired(self) -> int:
        removed = 0
        rows = self.db.fetch_all(
            """SELECT * FROM sandbox_instances WHERE status='ACTIVE' AND expires_at < ?""",
            (utc_now(),),
        )
        for instance in rows:
            if self.db.fetch_one("""SELECT id FROM runs WHERE coding_workspace_id IN
                (SELECT id FROM coding_workspaces WHERE sandbox_instance_id=?) AND status='CANCELLING' LIMIT 1""",
                (instance['id'],)):
                # Preserve the only uncommitted cancellation evidence. A later
                # tick may collect it after finalization completes.
                continue
            workspace = self.db.fetch_one(
                "SELECT * FROM coding_workspaces WHERE sandbox_instance_id=?",
                (instance["id"],),
            )
            if workspace:
                run = self.db.fetch_one(
                    """SELECT * FROM runs WHERE coding_workspace_id=?
                       ORDER BY created_at DESC LIMIT 1""",
                    (workspace["id"],),
                )
                if run:
                    plan_row = self.db.fetch_one(
                        "SELECT plan_json FROM resolved_execution_plans WHERE id=?",
                        (run["resolved_plan_id"],),
                    )
                    if plan_row:
                        try:
                            await self.snapshot_workspace(
                                workspace,
                                run=run,
                                plan=plan_row["plan"],
                                reason="workspace_ttl_expired",
                            )
                        except Exception as exc:
                            self.events.append(
                                run["id"],
                                "workspace.snapshot.failed",
                                {"reason": "workspace_ttl_expired", "message": str(exc)[:500]},
                            )
            provider = self.providers.get(instance["provider"])
            if provider and instance.get("external_id"):
                await provider.destroy(instance["external_id"])
            self.db.execute(
                "UPDATE sandbox_instances SET status='EXPIRED', updated_at=? WHERE id=?",
                (utc_now(), instance["id"]),
            )
            self.db.execute(
                """UPDATE coding_workspaces SET status='EXPIRED', updated_at=?
                   WHERE sandbox_instance_id=?""",
                (utc_now(), instance["id"]),
            )
            removed += 1
        return removed

    async def interrupt_run(self, run_id: str) -> None:
        row = self.db.fetch_one(
            """SELECT s.*, r.current_attempt_id FROM runs r
               JOIN coding_workspaces w ON w.id=r.coding_workspace_id
               JOIN sandbox_instances s ON s.id=w.sandbox_instance_id
               WHERE r.id=? AND s.status='ACTIVE'""",
            (run_id,),
        )
        if not row or not row.get("external_id"):
            return
        provider = self.providers.get(row["provider"])
        scoped_interrupt = getattr(provider, "interrupt_attempt", None)
        if scoped_interrupt is not None:
            await scoped_interrupt(row["external_id"], row["current_attempt_id"])
            return
        interrupt = getattr(provider, "interrupt", None) if provider else None
        if interrupt is not None:
            await interrupt(row["external_id"])

    @staticmethod
    def _materialize_skills(
        backend: SandboxBackendProtocol, skills: list[Dict[str, Any]]
    ) -> list[str]:
        files = []
        paths = []
        for skill in skills:
            instructions = skill["instructions"]
            if hashlib.sha256(instructions.encode()).hexdigest() != skill.get(
                "artifact_hash"
            ):
                raise SandboxUnavailableError(
                    f"Skill artifact hash mismatch: {skill.get('slug', 'unknown')}"
                )
            path = f"/skills/{skill['slug']}/SKILL.md"
            files.append((path, instructions.encode()))
            paths.append(f"/skills/{skill['slug']}/")
        if files:
            results = backend.upload_files(files)
            errors = [
                getattr(result, "error", None)
                for result in results
                if getattr(result, "error", None)
            ]
            if errors:
                raise SandboxUnavailableError(f"Unable to materialize skills: {errors[0]}")
        return paths

    def _restore_latest_patch(
        self,
        run: Dict[str, Any],
        plan: Dict[str, Any],
        workspace: Dict[str, Any],
        backend: SandboxBackendProtocol,
    ) -> None:
        change_set = self.db.fetch_one(
            """SELECT * FROM change_sets WHERE workspace_id=?
               ORDER BY workspace_generation DESC, created_at DESC LIMIT 1""",
            (workspace["id"],),
        )
        if not change_set:
            return
        repository_snapshot = self.db.fetch_one(
            "SELECT resolved_commit_sha FROM repository_snapshots WHERE id=?",
            (workspace["repository_snapshot_id"],),
        )
        if not repository_snapshot:
            raise CodingConflictError("Repository snapshot is unavailable during recovery")
        if change_set.get("plan_hash") != plan.get("plan_hash"):
            raise CodingConflictError("Recovery ChangeSet plan hash does not match this run")
        if change_set["base_commit_sha"] != repository_snapshot["resolved_commit_sha"]:
            raise CodingConflictError("Recovery ChangeSet base commit does not match the workspace")
        if int(change_set["workspace_generation"]) != int(
            workspace["workspace_generation"]
        ):
            raise CodingConflictError(
                "Latest durable ChangeSet does not cover the current workspace generation"
            )
        artifact = self.db.fetch_one(
            "SELECT * FROM artifacts WHERE id=?", (change_set["patch_artifact_id"],)
        )
        if not artifact or not artifact.get("content"):
            raise CodingConflictError("Durable workspace patch is unavailable")
        if hashlib.sha256(artifact["content"].encode()).hexdigest() != artifact["content_hash"]:
            raise CodingConflictError("Durable workspace patch hash is invalid")
        if (
            artifact.get("plan_hash") != change_set.get("plan_hash")
            or artifact.get("base_commit_sha") != change_set.get("base_commit_sha")
            or int(artifact.get("workspace_generation", -1))
            != int(change_set["workspace_generation"])
        ):
            raise CodingConflictError("Durable workspace patch metadata is invalid")
        verification_hash = ""
        if change_set.get("verification_report_id"):
            verification = self.db.fetch_one(
                "SELECT content_hash FROM verification_reports WHERE id=?",
                (change_set["verification_report_id"],),
            )
            verification_hash = str((verification or {}).get("content_hash") or "")
        expected_change_set_hash = hashlib.sha256(
            (
                change_set["base_commit_sha"]
                + str(change_set["workspace_generation"])
                + artifact["content"]
                + verification_hash
            ).encode()
        ).hexdigest()
        if expected_change_set_hash != change_set["content_hash"]:
            raise CodingConflictError("Durable ChangeSet content hash is invalid")
        uploaded = backend.upload_files(
            [("/artifacts/recovery.patch", artifact["content"].encode())]
        )[0]
        if getattr(uploaded, "error", None):
            raise CodingConflictError("Unable to upload recovery patch")
        result = backend.execute("git apply --binary /artifacts/recovery.patch")
        if result.exit_code:
            raise CodingConflictError(
                f"Unable to restore workspace patch: {result.output[:500]}"
            )
        self.db.execute(
            """UPDATE coding_workspaces SET workspace_generation=?, status='DIRTY',
               updated_at=? WHERE id=?""",
            (change_set["workspace_generation"], utc_now(), workspace["id"]),
        )
        self.events.append(
            run["id"],
            "workspace.recovering",
            {
                "workspace_id": workspace["id"],
                "changeset_id": change_set["id"],
                "workspace_generation": change_set["workspace_generation"],
            },
        )
