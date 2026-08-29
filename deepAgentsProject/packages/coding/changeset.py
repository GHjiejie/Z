from __future__ import annotations

import hashlib
import json
import shlex
from typing import Any, Dict, Optional

from deepagents.backends.protocol import SandboxBackendProtocol

from packages.application.services import new_id
from packages.domain.models import utc_now
from packages.persistence import Database
from packages.runtime.event_emitter import EventEmitter


class VerificationService:
    def __init__(self, db: Database, events: EventEmitter):
        self.db = db
        self.events = events

    def run(
        self,
        run: Dict[str, Any],
        workspace: Dict[str, Any],
        backend: SandboxBackendProtocol,
        policy: Dict[str, Any],
    ) -> Dict[str, Any]:
        commands = list(policy.get("required_commands") or [])
        if policy.get("auto_discover", True):
            commands.extend(self._discover(backend))
        commands = list(dict.fromkeys(command for command in commands if command.strip()))
        self.events.append(
            run["id"],
            "verification.started",
            {"workspace_id": workspace["id"], "commands": commands},
            span_id="span_verification",
            parent_span_id="span_main",
            execution_path=["main", "verification"],
        )
        checks = []
        timeout = int(policy.get("command_timeout_seconds", 300))
        for index, command in enumerate(commands, start=1):
            result = backend.execute(command, timeout=timeout)
            check = {
                "id": f"check_{index}",
                "command": command,
                "exit_code": result.exit_code,
                "status": "passed" if result.exit_code == 0 else "failed",
                "truncated": result.truncated,
                "output_preview": result.output[-2000:],
            }
            checks.append(check)
            self.events.append(
                run["id"],
                "verification.check.completed",
                {key: value for key, value in check.items() if key != "output_preview"},
                span_id="span_verification",
                parent_span_id="span_main",
                execution_path=["main", "verification", check["id"]],
            )
        if not checks:
            status = "NOT_CONFIGURED"
        elif all(check["status"] == "passed" for check in checks):
            status = "PASSED"
        else:
            status = "FAILED"
        summary = {
            "total": len(checks),
            "passed": sum(check["status"] == "passed" for check in checks),
            "failed": sum(check["status"] == "failed" for check in checks),
            "require_success": bool(policy.get("require_success", True)),
        }
        canonical = json.dumps(
            {"status": status, "checks": checks, "summary": summary},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        existing = self.db.fetch_one(
            "SELECT id FROM verification_reports WHERE run_id=?", (run["id"],)
        )
        report_id = existing["id"] if existing else new_id("verify")
        content_hash = hashlib.sha256(canonical.encode()).hexdigest()
        if existing:
            self.db.execute(
                """UPDATE verification_reports SET status=?, checks_json=?, summary_json=?,
                   content_hash=?, created_at=? WHERE id=?""",
                (
                    status,
                    self.db.encode(checks),
                    self.db.encode(summary),
                    content_hash,
                    utc_now(),
                    report_id,
                ),
            )
        else:
            self.db.execute(
                """INSERT INTO verification_reports
                   (id, tenant_id, project_id, run_id, workspace_id, status, checks_json,
                    summary_json, content_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    report_id,
                    run["tenant_id"],
                    run["project_id"],
                    run["id"],
                    workspace["id"],
                    status,
                    self.db.encode(checks),
                    self.db.encode(summary),
                    content_hash,
                    utc_now(),
                ),
            )
        self.events.append(
            run["id"],
            "verification.completed",
            {"verification_report_id": report_id, "status": status, **summary},
            span_id="span_verification",
            parent_span_id="span_main",
            execution_path=["main", "verification"],
        )
        return self.db.fetch_one("SELECT * FROM verification_reports WHERE id=?", (report_id,))

    @staticmethod
    def _discover(backend: SandboxBackendProtocol) -> list[str]:
        commands = []
        if backend.execute("test -f pyproject.toml -a -d tests").exit_code == 0:
            commands.append("python -m pytest -q")
        python_source = backend.execute(
            "find . -type f -name '*.py' -print -quit"
        )
        if python_source.exit_code == 0 and python_source.output.strip():
            commands.append(
                "PYTHONPYCACHEPREFIX=/tmp/deepagent-pycache python -m compileall -q ."
            )
        package = backend.download_files(["/workspace/repo/package.json"])[0]
        package_content = getattr(package, "content", None)
        if not getattr(package, "error", None) and package_content:
            try:
                scripts = json.loads(package_content.decode()).get("scripts", {})
                if "test" in scripts:
                    commands.append("npm test -- --runInBand")
                elif "build" in scripts:
                    commands.append("npm run build")
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        return commands


class ChangeSetBuilder:
    def __init__(self, db: Database, events: EventEmitter):
        self.db = db
        self.events = events

    def build(
        self,
        run: Dict[str, Any],
        workspace: Dict[str, Any],
        snapshot: Dict[str, Any],
        backend: SandboxBackendProtocol,
        verification: Optional[Dict[str, Any]],
        coding_profile: Dict[str, Any],
        *,
        plan_hash: str,
    ) -> Dict[str, Any]:
        # The real repository index is read-only in patch_only sandboxes. A
        # platform-owned temporary index makes untracked files visible without
        # allowing the agent to rewrite Git metadata.
        temporary_index = f"/tmp/deepagent-changeset-{run['id']}.index"
        temporary_objects = f"/tmp/deepagent-objects-{run['id']}"
        base_objects = f"/tmp/deepagent-base-objects-{run['id']}"
        index_env = (
            f"GIT_INDEX_FILE={shlex.quote(temporary_index)} "
            f"GIT_OBJECT_DIRECTORY={shlex.quote(temporary_objects)} "
            f"GIT_ALTERNATE_OBJECT_DIRECTORIES={shlex.quote(base_objects)}"
        )
        platform_execute = getattr(backend, "execute_platform", backend.execute)
        prepared = platform_execute(
            f"rm -f -- {shlex.quote(temporary_index)} {shlex.quote(base_objects)} && "
            f"rm -rf -- {shlex.quote(temporary_objects)} && "
            f"mkdir -p -- {shlex.quote(temporary_objects)} && "
            f"ln -s /workspace/repo/.git/objects {shlex.quote(base_objects)} && "
            f"{index_env} git read-tree HEAD && {index_env} git add -N -- ."
        )
        if prepared.exit_code:
            raise RuntimeError(f"Unable to prepare workspace diff: {prepared.output[:500]}")
        patch_result = backend.execute(
            f"{index_env} git diff --binary --no-ext-diff --no-color HEAD"
        )
        if patch_result.exit_code:
            raise RuntimeError(f"Unable to compute workspace patch: {patch_result.output[:500]}")
        numstat_result = backend.execute(f"{index_env} git diff --numstat -z HEAD")
        status_result = backend.execute("git status --porcelain=v1 -z")
        patch = patch_result.output
        changed_files = self._parse_status(status_result.output)
        self._attach_file_hashes(changed_files, backend)
        diff_stat = self._parse_numstat(numstat_result.output)
        diff_lines = diff_stat["added"] + diff_stat["deleted"]
        over_limit = (
            len(changed_files) > int(coding_profile.get("max_changed_files", 50))
            or diff_lines > int(coding_profile.get("max_diff_lines", 5000))
        )
        patch_artifact_id = self._artifact(
            run, "changes.patch", "text/x-diff", patch
        )
        diff_document = json.dumps(
            {
                "base_commit_sha": snapshot["resolved_commit_sha"],
                "workspace_generation": workspace["workspace_generation"],
                "diff_stat": diff_stat,
                "changed_files": changed_files,
                "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
            },
            ensure_ascii=False,
            indent=2,
        )
        diff_artifact_id = self._artifact(
            run, "diff.json", "application/json", diff_document
        )
        self.db.execute(
            """UPDATE artifacts SET plan_hash=?, base_commit_sha=?, workspace_generation=?,
               artifact_metadata_json=? WHERE id IN (?, ?)""",
            (
                plan_hash,
                snapshot["resolved_commit_sha"],
                workspace["workspace_generation"],
                self.db.encode(
                    {
                        "workspace_id": workspace["id"],
                        "kind": "coding_changeset",
                    }
                ),
                patch_artifact_id,
                diff_artifact_id,
            ),
        )
        content_hash = hashlib.sha256(
            (
                snapshot["resolved_commit_sha"]
                + str(workspace["workspace_generation"])
                + patch
                + (verification["content_hash"] if verification else "")
            ).encode()
        ).hexdigest()
        change_set_id = new_id("chg")
        status = "REVIEW_REQUIRED" if over_limit else "READY"
        self.db.execute(
            """INSERT INTO change_sets
               (id, tenant_id, project_id, run_id, workspace_id, base_commit_sha,
                workspace_generation, patch_artifact_id, diff_artifact_id,
               verification_report_id, diff_stat_json, changed_files_json,
                status, content_hash, plan_hash, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                change_set_id,
                run["tenant_id"],
                run["project_id"],
                run["id"],
                workspace["id"],
                snapshot["resolved_commit_sha"],
                workspace["workspace_generation"],
                patch_artifact_id,
                diff_artifact_id,
                verification["id"] if verification else None,
                self.db.encode(diff_stat),
                self.db.encode(changed_files),
                status,
                content_hash,
                plan_hash,
                utc_now(),
            ),
        )
        self.events.append(
            run["id"],
            "changeset.created",
            {
                "changeset_id": change_set_id,
                "status": status,
                "changed_files": len(changed_files),
                "diff_stat": diff_stat,
                "content_hash": content_hash,
            },
        )
        if over_limit:
            self.events.append(
                run["id"],
                "changeset.review_required",
                {"changeset_id": change_set_id, "reason": "change_limit_exceeded"},
            )
        return self.db.fetch_one("SELECT * FROM change_sets WHERE id=?", (change_set_id,))

    @staticmethod
    def _attach_file_hashes(
        changed_files: list[Dict[str, Any]], backend: SandboxBackendProtocol
    ) -> None:
        for item in changed_files:
            if "D" in item.get("status", ""):
                item["sha256"] = None
                continue
            path = str(item.get("path") or "")
            if not path:
                item["sha256"] = None
                continue
            result = backend.download_files([f"/workspace/repo/{path}"])[0]
            error = result.get("error") if isinstance(result, dict) else result.error
            content = result.get("content") if isinstance(result, dict) else result.content
            item["sha256"] = (
                None if error or content is None else hashlib.sha256(content).hexdigest()
            )

    def _artifact(
        self, run: Dict[str, Any], name: str, media_type: str, content: str
    ) -> str:
        artifact_id = new_id("art")
        encoded = content.encode()
        digest = hashlib.sha256(encoded).hexdigest()
        self.db.execute(
            """INSERT INTO artifacts
               (id, tenant_id, project_id, run_id, name, media_type, size_bytes,
                content_hash, content, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                artifact_id,
                run["tenant_id"],
                run["project_id"],
                run["id"],
                name,
                media_type,
                len(encoded),
                digest,
                content,
                utc_now(),
            ),
        )
        self.events.append(
            run["id"],
            "artifact.created",
            {"artifact_id": artifact_id, "name": name, "media_type": media_type, "content_hash": digest},
        )
        return artifact_id

    @staticmethod
    def _parse_status(output: str) -> list[Dict[str, str]]:
        entries = []
        chunks = [chunk for chunk in output.split("\0") if chunk]
        index = 0
        while index < len(chunks):
            chunk = chunks[index]
            status = chunk[:2]
            path = chunk[3:] if len(chunk) > 3 else ""
            entry = {"path": path, "status": status.strip() or status}
            if status.startswith("R") or status.startswith("C"):
                index += 1
                if index < len(chunks):
                    entry["original_path"] = path
                    entry["path"] = chunks[index]
            entries.append(entry)
            index += 1
        return entries

    @staticmethod
    def _parse_numstat(output: str) -> Dict[str, int]:
        added = deleted = files = 0
        chunks = [chunk for chunk in output.split("\0") if chunk]
        for chunk in chunks:
            fields = chunk.split("\t", 2)
            if len(fields) < 3:
                continue
            files += 1
            if fields[0].isdigit():
                added += int(fields[0])
            if fields[1].isdigit():
                deleted += int(fields[1])
        return {"files": files, "added": added, "deleted": deleted}
