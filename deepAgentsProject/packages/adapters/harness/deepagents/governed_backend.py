from __future__ import annotations

from packages.auth.resource_access import ResourceAccess

import hashlib
import time
from typing import Any, Optional

from deepagents.backends.protocol import BackendProtocol, ExecuteResponse, SandboxBackendProtocol

from packages.application.services import new_id
from packages.coding.errors import SandboxPolicyError
from packages.coding.redaction import redact_text
from packages.domain.models import utc_now
from packages.persistence import Database
from packages.runtime.event_emitter import EventEmitter
from packages.sandbox.policy import SandboxPolicy


class GovernedSandboxBackend(SandboxBackendProtocol):
    """Policy, audit and event facade around a sandbox backend.

    The raw backend is never passed to Deep Agents. File mutation generation and
    command evidence are therefore recorded independently of model narration.
    """

    def __init__(
        self,
        raw: SandboxBackendProtocol,
        *,
        policy: SandboxPolicy,
        db: Database,
        events: EventEmitter,
        run: dict[str, Any],
        workspace: dict[str, Any],
    ):
        self.raw = raw
        self.policy = policy
        self.db = db
        self.events = events
        self.run = run
        self.workspace = workspace

    @property
    def id(self) -> str:
        return self.raw.id

    def ls(self, path: str):
        normalized = self._authorize_path(path, "read")
        result = self.raw.ls(normalized)
        self._emit("file.listed", {"path": normalized, "error": _error(result)})
        return result

    def read(self, file_path: str, offset: int = 0, limit: int = 2000):
        normalized = self._authorize_path(file_path, "read")
        result = self.raw.read(normalized, offset, limit)
        self._emit(
            "file.read",
            {"path": normalized, "offset": offset, "limit": limit, "error": _error(result)},
        )
        return result

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ):
        normalized = self._authorize_path(path or self.policy.workspace_root, "read")
        result = self.raw.grep(pattern, normalized, glob, max_count=max_count)
        self._emit(
            "file.searched",
            {
                "path": normalized,
                "pattern_hash": hashlib.sha256(pattern.encode()).hexdigest(),
                "glob": glob,
                "error": _error(result),
            },
        )
        return result

    def glob(self, pattern: str, path: str | None = None):
        normalized = self._authorize_path(path or self.policy.workspace_root, "read")
        result = self.raw.glob(pattern, normalized)
        self._emit(
            "file.globbed",
            {"path": normalized, "pattern": pattern, "error": _error(result)},
        )
        return result

    def write(self, file_path: str, content: str):
        normalized = self._authorize_path(file_path, "write")
        before = self._content_hash(normalized)
        result = self.raw.write(normalized, content)
        if not _error(result):
            self._record_change(normalized, "write", before, hashlib.sha256(content.encode()).hexdigest())
        return result

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ):
        normalized = self._authorize_path(file_path, "edit")
        before = self._content_hash(normalized)
        result = self.raw.edit(normalized, old_string, new_string, replace_all)
        if not _error(result):
            self._record_change(normalized, "edit", before, self._content_hash(normalized))
        return result

    def delete(self, file_path: str):
        normalized = self._authorize_path(file_path, "delete")
        before = self._content_hash(normalized)
        result = self.raw.delete(normalized)
        if not _error(result):
            self._record_change(normalized, "delete", before, None)
        return result

    def upload_files(self, files: list[tuple[str, bytes]]):
        for path, _ in files:
            # This method is used by the platform for immutable skills and recovery
            # artifacts. Agent-facing writes go through write/edit instead.
            self._validate_resolved(self.policy.normalize_path(path))
        return self.raw.upload_files(files)

    def download_files(self, paths: list[str]):
        for path in paths:
            self._authorize_path(path, "read")
        return self.raw.download_files(paths)

    def _authorize_path(self, path: str, operation: str) -> str:
        self.db.assert_execution_fence()
        normalized = self.policy.authorize_path(path, operation)
        ResourceAccess(self.db).require_execution(self.run["id"])
        self._validate_resolved(normalized)
        return normalized

    def _validate_resolved(self, normalized: str) -> None:
        resolver = getattr(self.raw, "resolve_path", None)
        if resolver is None:
            return
        resolved = str(resolver(normalized))
        allowed_roots = (self.policy.workspace_root, "/artifacts", "/skills")
        if not any(
            resolved == root or resolved.startswith(root + "/")
            for root in allowed_roots
        ):
            from packages.coding.errors import SandboxPolicyError

            raise SandboxPolicyError(
                f"Path resolves outside the governed workspace: {normalized}"
            )

    def execute(self, command: str, *, timeout: int | None = None):
        from packages.operations.telemetry import operation
        with operation('sandbox.command') as span:
            result = self._execute_governed(command, timeout=timeout)
            if span is not None and result.exit_code:
                from opentelemetry.trace import StatusCode
                span.set_status(StatusCode.ERROR)
                span.set_attribute('deepagent.command.exit_code', result.exit_code)
            return result

    def _execute_governed(self, command: str, *, timeout: int | None = None):
        self.db.assert_execution_fence()
        try:
            self.policy.authorize_command(command)
        except SandboxPolicyError as exc:
            return self._deny_command(command, exc)
        ResourceAccess(self.db).require_execution(self.run["id"])
        return self._execute_audited(command, timeout=timeout, actor="agent")

    def _deny_command(self, command: str, error: SandboxPolicyError) -> ExecuteResponse:
        """Audit a policy rejection and return it to the agent as a recoverable tool result."""
        command_id = new_id("cmd")
        command_hash = hashlib.sha256(command.encode()).hexdigest()
        preview = redact_text(command)[:300]
        message = redact_text(str(error))[:500]
        now = utc_now()
        self.db.execute(
            """INSERT INTO sandbox_commands
               (id, tenant_id, project_id, run_id, workspace_id, command_hash,
                command_preview, working_directory, status, exit_code, duration_ms,
                resource_usage_json, created_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'DENIED', 126, 0, '{}', ?, ?)""",
            (
                command_id,
                self.run["tenant_id"],
                self.run["project_id"],
                self.run["id"],
                self.workspace["id"],
                command_hash,
                preview,
                self.policy.workspace_root,
                now,
                now,
            ),
        )
        self._emit(
            "sandbox.command.denied",
            {
                "command_id": command_id,
                "command_hash": command_hash,
                "preview": preview,
                "actor": "agent",
                "message": message,
                "exit_code": 126,
            },
        )
        return ExecuteResponse(
            output=f"Command was not executed: {message}",
            exit_code=126,
        )

    def execute_platform(self, command: str, *, timeout: int | None = None):
        """Execute a platform-authored maintenance command with full auditing.

        Callers must not pass model-generated strings. This narrow escape hatch
        is used for the temporary Git index needed to calculate an immutable
        ChangeSet while the real .git directory remains read-only.
        """
        return self._execute_audited(command, timeout=timeout, actor="platform")

    def _execute_audited(
        self, command: str, *, timeout: int | None, actor: str
    ):
        ResourceAccess(self.db).require_execution(self.run["id"])
        before_workspace = self._workspace_fingerprint()
        command_id = new_id("cmd")
        command_hash = hashlib.sha256(command.encode()).hexdigest()
        preview = redact_text(command)[:300]
        now = utc_now()
        self.db.execute(
            """INSERT INTO sandbox_commands
               (id, tenant_id, project_id, run_id, workspace_id, command_hash,
                command_preview, working_directory, status, resource_usage_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING', '{}', ?)""",
            (
                command_id,
                self.run["tenant_id"],
                self.run["project_id"],
                self.run["id"],
                self.workspace["id"],
                command_hash,
                preview,
                self.policy.workspace_root,
                now,
            ),
        )
        self._emit(
            "sandbox.command.requested",
            {
                "command_id": command_id,
                "command_hash": command_hash,
                "preview": preview,
                "actor": actor,
            },
        )
        self._emit("sandbox.command.started", {"command_id": command_id})
        started = time.monotonic()
        try:
            result = self.raw.execute(command, timeout=timeout)
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            self.db.execute(
                """UPDATE sandbox_commands SET status='FAILED', duration_ms=?, completed_at=?
                   WHERE id=?""",
                (duration_ms, utc_now(), command_id),
            )
            self._emit(
                "sandbox.command.failed",
                {
                    "command_id": command_id,
                    "message": redact_text(str(exc))[:500],
                    "duration_ms": duration_ms,
                },
            )
            raise
        duration_ms = int((time.monotonic() - started) * 1000)
        after_workspace = self._workspace_fingerprint()
        if before_workspace != after_workspace:
            self._record_change(
                "*", "execute", before_workspace, after_workspace
            )
        output = redact_text(result.output)
        resource_usage = dict(getattr(self.raw, "last_resource_usage", {}) or {})
        artifact_id: Optional[str] = None
        if output:
            streamed = output[:32_768]
            for offset in range(0, len(streamed), 4096):
                self._emit(
                    "sandbox.command.delta",
                    {
                        "command_id": command_id,
                        "delta": streamed[offset : offset + 4096],
                        "offset": offset,
                        "truncated": len(output) > len(streamed),
                    },
                )
            artifact_id = self._artifact(
                # Git's machine-readable -z output contains NUL separators,
                # which PostgreSQL TEXT cannot store. Escape only the human
                # log; the raw tool response must retain its parser semantics.
                f"command-{command_id}.log", "text/plain", output.replace("\x00", "\\0")
            )
        status = "SUCCEEDED" if result.exit_code == 0 else "FAILED"
        self.db.execute(
            """UPDATE sandbox_commands SET status=?, exit_code=?, duration_ms=?,
               output_artifact_id=?, resource_usage_json=?, completed_at=? WHERE id=?""",
            (
                status,
                result.exit_code,
                duration_ms,
                artifact_id,
                self.db.encode(resource_usage),
                utc_now(),
                command_id,
            ),
        )
        event_type = "sandbox.command.completed" if result.exit_code == 0 else "sandbox.command.failed"
        self._emit(
            event_type,
            {
                "command_id": command_id,
                "exit_code": result.exit_code,
                "duration_ms": duration_ms,
                "truncated": result.truncated,
                "output_artifact_id": artifact_id,
                "resource_usage": resource_usage,
            },
        )
        result.output = output
        return result

    def _record_change(
        self,
        path: str,
        operation: str,
        before_hash: Optional[str],
        after_hash: Optional[str],
    ) -> None:
        self.db.execute(
            """UPDATE coding_workspaces SET workspace_generation=workspace_generation+1,
               status='DIRTY', updated_at=? WHERE id=?""",
            (utc_now(), self.workspace["id"]),
        )
        generation = self.db.fetch_one(
            "SELECT workspace_generation FROM coding_workspaces WHERE id=?",
            (self.workspace["id"],),
        )["workspace_generation"]
        self.db.execute(
            """UPDATE runs SET workspace_generation=?, updated_at=? WHERE id=?""",
            (generation, utc_now(), self.run["id"]),
        )
        self.db.execute(
            """INSERT INTO workspace_file_changes
               (id, tenant_id, project_id, run_id, workspace_id, path, operation,
                before_hash, after_hash, workspace_generation, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("fchg"),
                self.run["tenant_id"],
                self.run["project_id"],
                self.run["id"],
                self.workspace["id"],
                path,
                operation,
                before_hash,
                after_hash,
                generation,
                utc_now(),
            ),
        )
        self.workspace["workspace_generation"] = generation
        self.workspace["status"] = "DIRTY"
        self._emit(
            "file.changed" if operation != "delete" else "file.deleted",
            {
                "path": path,
                "operation": operation,
                "before_hash": before_hash,
                "after_hash": after_hash,
                "workspace_generation": generation,
            },
        )

    def _content_hash(self, path: str) -> Optional[str]:
        result = self.raw.download_files([path])[0]
        content = result.get("content") if isinstance(result, dict) else result.content
        error = result.get("error") if isinstance(result, dict) else result.error
        return None if error or content is None else hashlib.sha256(content).hexdigest()

    def _workspace_fingerprint(self) -> str:
        result = self.raw.execute(
            "(git diff --binary --no-ext-diff --no-color HEAD; "
            "git ls-files --others --exclude-standard -z | sort -z | "
            "xargs -0 sha256sum) | sha256sum",
            timeout=30,
        )
        canonical = f"{result.exit_code}\0{result.output}".encode()
        return hashlib.sha256(canonical).hexdigest()

    def _artifact(self, name: str, media_type: str, content: str) -> str:
        artifact_id = new_id("art")
        encoded = content.encode()
        digest = hashlib.sha256(encoded).hexdigest()
        self.db.execute(
            """INSERT INTO artifacts
               (id, tenant_id, project_id, run_id, name, media_type, size_bytes,
                content_hash, content, plan_hash, base_commit_sha, workspace_generation,
                artifact_metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                artifact_id,
                self.run["tenant_id"],
                self.run["project_id"],
                self.run["id"],
                name,
                media_type,
                len(encoded),
                digest,
                content,
                self._plan_hash(),
                self._base_commit(),
                self.workspace.get("workspace_generation"),
                self.db.encode({"kind": "sandbox_command", "workspace_id": self.workspace["id"]}),
                utc_now(),
            ),
        )
        self._emit(
            "artifact.created",
            {"artifact_id": artifact_id, "name": name, "media_type": media_type, "content_hash": digest},
        )
        return artifact_id

    def _plan_hash(self) -> Optional[str]:
        row = self.db.fetch_one(
            "SELECT plan_hash FROM resolved_execution_plans WHERE id=?",
            (self.run["resolved_plan_id"],),
        )
        return str(row["plan_hash"]) if row else None

    def _base_commit(self) -> Optional[str]:
        row = self.db.fetch_one(
            "SELECT resolved_commit_sha FROM repository_snapshots WHERE id=?",
            (self.workspace["repository_snapshot_id"],),
        )
        return str(row["resolved_commit_sha"]) if row else None

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append(
            self.run["id"],
            event_type,
            payload,
            span_id="span_coding_workspace",
            parent_span_id="span_main",
            execution_path=["main", "coding_workspace"],
        )


def _error(result: Any) -> Any:
    return result.get("error") if isinstance(result, dict) else getattr(result, "error", None)
