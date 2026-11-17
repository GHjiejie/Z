"""Owner-scoped run state, leases, submissions and execution receipts."""

import hashlib
import hmac
import json
import os
import secrets
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from agent_permissions.config import Settings
from agent_permissions.errors import ApiError
from agent_permissions.storage import Connection, DatabaseBackend
from api_tools.compiler import canonical

GRAPH_VERSION = 1
LEASE_SECONDS = 150
TERMINAL = {"completed", "cancelled", "expired", "failed", "needs_reconciliation"}


def identity(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def secret_key(settings: Settings) -> bytes:
    if settings.hitl_digest_key:
        if len(settings.hitl_digest_key) < 32:
            raise ValueError("HITL digest key must contain at least 32 characters")
        return settings.hitl_digest_key.encode()
    directory = settings.database_path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "hitl-digest.key"
    if not path.exists():
        with tempfile.NamedTemporaryFile(dir=directory) as temporary:
            temporary.write(secrets.token_bytes(32))
            temporary.flush()
            try:
                os.link(temporary.name, path)
            except FileExistsError:
                pass
    key = Path(path).read_bytes()
    if len(key) < 32:
        raise ValueError("Invalid HITL digest key file")
    return key


class RunStore:
    def __init__(
        self,
        database: DatabaseBackend,
        settings: Settings,
        clock: Callable[[], datetime],
    ) -> None:
        self.database, self.settings, self.clock = database, settings, clock
        self._key: bytes | None = None

    def digest(self, value: object) -> str:
        if self._key is None:
            self._key = secret_key(self.settings)
        return hmac.new(
            self._key, canonical(value).encode(), hashlib.sha256
        ).hexdigest()

    def now(self) -> float:
        return self.clock().timestamp()

    def audit(self, connection: Connection, run: dict, action: str, **changes) -> None:
        connection.execute(
            """INSERT INTO audit_events(id,actor_user_id,target_user_id,action,outcome,reason_code,request_id,source,changes,created_at)
            VALUES(?,?,NULL,?,'success',NULL,?,'agent-hitl',?,?)""",
            (
                identity("evt"),
                run["user_id"],
                action,
                run["id"],
                canonical({"run_id": run["id"], **changes}),
                self.clock().isoformat(),
            ),
        )

    @staticmethod
    def _owned(connection: Connection, run_id: str, user_id: str) -> dict:
        row = connection.execute(
            "SELECT * FROM agent_chat_runs WHERE id=? AND user_id=?", (run_id, user_id)
        ).fetchone()
        if row is None:
            raise ApiError(404, "RUN_NOT_FOUND", "任务不存在或不可访问")
        return dict(row)

    def cleanup(self) -> None:
        now = self.now()
        with self.database.transaction(write=True) as connection:
            connection.execute(
                "UPDATE agent_chat_runs SET status='expired',lease_token=NULL,version=version+1 WHERE status IN ('running','waiting','interrupted') AND expires_at<=?",
                (now,),
            )
            connection.execute(
                "UPDATE agent_interactions SET status='expired' WHERE status='pending' AND expires_at<=?",
                (now,),
            )
            connection.execute(
                """UPDATE agent_chat_runs SET status='expired' WHERE status='waiting' AND EXISTS
                (SELECT 1 FROM agent_interactions i WHERE i.run_id=agent_chat_runs.id AND i.status='expired')
                AND NOT EXISTS (SELECT 1 FROM agent_interactions i WHERE i.run_id=agent_chat_runs.id AND i.status='pending')"""
            )
            connection.execute(
                "UPDATE agent_chat_runs SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'interrupted' END,lease_token=NULL,version=version+1 WHERE status='running' AND lease_until<?",
                (now,),
            )
            rows = connection.execute(
                "SELECT id FROM agent_chat_runs WHERE status NOT IN ('running','waiting') AND purged=0 AND updated_at<?",
                (now - self.settings.hitl_retention_seconds,),
            ).fetchall()
            for row in rows:
                run_id = row[0]
                connection.execute(
                    "DELETE FROM agent_workflow_entries WHERE run_id=?", (run_id,)
                )
                connection.execute(
                    "DELETE FROM agent_checkpoint_writes WHERE thread_id=?", (run_id,)
                )
                connection.execute(
                    "DELETE FROM agent_checkpoints WHERE thread_id=?", (run_id,)
                )
                connection.execute(
                    "UPDATE agent_interactions SET document='{}' WHERE run_id=?",
                    (run_id,),
                )
                connection.execute(
                    "UPDATE agent_tool_executions SET service_result=NULL,result=NULL WHERE run_id=?",
                    (run_id,),
                )
                connection.execute(
                    "UPDATE agent_chat_runs SET payload='{}',output='',usage='{}',error=NULL,purged=1 WHERE id=?",
                    (run_id,),
                )
            # Keep the small run/submission tombstones; an old key must never create a new write.
            connection.execute(
                "DELETE FROM agent_tool_executions WHERE updated_at<? AND run_id IN (SELECT id FROM agent_chat_runs WHERE purged=1)",
                (now - self.settings.hitl_receipt_seconds,),
            )

    def create(
        self, user_id: str, conversation: str, client_request: str, payload: dict
    ) -> tuple[dict, bool]:
        self.cleanup()
        fingerprint = self.digest({"conversation": conversation, "payload": payload})
        now = self.now()
        with self.database.transaction(write=True) as connection:
            saved = connection.execute(
                "SELECT * FROM agent_chat_runs WHERE user_id=? AND client_request_id=?",
                (user_id, client_request),
            ).fetchone()
            if saved:
                if saved["request_digest"] != fingerprint:
                    raise ApiError(
                        409, "IDEMPOTENCY_CONFLICT", "请求标识已用于另一份任务内容"
                    )
                return dict(saved), False
            if connection.execute(
                "SELECT 1 FROM agent_chat_runs WHERE user_id=? AND conversation_id=? AND status IN ('running','waiting')",
                (user_id, conversation),
            ).fetchone():
                raise ApiError(
                    409, "RUN_ALREADY_ACTIVE", "请先处理或取消当前会话中的任务"
                )
            run_id, lease = identity("run"), identity("lease")
            connection.execute(
                """INSERT INTO agent_chat_runs(id,user_id,conversation_id,client_request_id,request_digest,payload,status,
                lease_token,lease_until,created_at,updated_at,expires_at) VALUES(?,?,?,?,?,?,'running',?,?,?,?,?)""",
                (
                    run_id,
                    user_id,
                    conversation,
                    client_request,
                    fingerprint,
                    canonical(payload),
                    lease,
                    now + LEASE_SECONDS,
                    now,
                    now,
                    now + self.settings.hitl_run_seconds,
                ),
            )
            return self._owned(connection, run_id, user_id), True

    def raw(self, run_id: str, user_id: str) -> dict:
        self.cleanup()
        with self.database.transaction() as connection:
            return self._owned(connection, run_id, user_id)

    def snapshot(self, run_id: str, user_id: str) -> dict:
        run = self.raw(run_id, user_id)
        with self.database.transaction() as connection:
            interactions = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM agent_interactions WHERE run_id=? ORDER BY created_at,id",
                    (run_id,),
                )
            ]
            executions = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM agent_tool_executions WHERE run_id=? ORDER BY created_at,id",
                    (run_id,),
                )
            ]
        pending = next(
            (row for row in reversed(interactions) if row["status"] == "pending"), None
        )
        return {
            "id": run_id,
            "conversation_id": run["conversation_id"],
            "client_request_id": run["client_request_id"],
            "status": run["status"],
            "version": run["version"],
            "content": run["output"],
            "finish": run["finish_reason"],
            "usage": json.loads(run["usage"]) or None,
            "error": json.loads(run["error"]) if run["error"] else None,
            "expires_at": run["expires_at"],
            "purged": bool(run["purged"]),
            "workflow": self.workflow(run),
            "interaction": self.public_interaction(pending)
            if pending
            and not run["purged"]
            and run["status"] in {"running", "waiting", "interrupted", "failed"}
            else None,
            "executions": [
                {
                    "id": e["id"],
                    "operation_id": e["operation_id"],
                    "status": e["status"],
                    "result": json.loads(e["result"]) if e["result"] else None,
                }
                for e in executions
            ],
        }

    def workflow(self, run: dict) -> list[dict]:
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT document FROM agent_workflow_entries WHERE run_id=? ORDER BY position,entry_id",
                (run["id"],),
            ).fetchall()
        entries = [json.loads(row[0]) for row in rows]
        for entry in entries:
            if entry["status"] in {"running", "pending", "waiting"} and run[
                "status"
            ] in {
                "cancelled",
                "expired",
                "failed",
                "interrupted",
                "needs_reconciliation",
            }:
                entry["status"] = (
                    "interrupted"
                    if run["status"] in {"interrupted", "needs_reconciliation"}
                    else "cancelled"
                    if run["status"] in {"cancelled", "expired"}
                    or entry["status"] == "pending"
                    else "failed"
                )
        return entries

    def record_workflow(self, run: dict, entry: dict) -> dict:
        """Upsert a bounded, sanitized display entry; checkpoint replay keeps its identity."""
        with self.database.transaction(write=True) as connection:
            current = self._owned(connection, run["id"], run["user_id"])
            if current["purged"] or current["cancel_requested"]:
                raise ApiError(409, "RUN_CANCELLED", "任务已停止")
            if run.get("lease_token") and current["lease_token"] != run["lease_token"]:
                raise ApiError(409, "RUN_CANCELLED", "任务执行上下文已变化")
            previous = connection.execute(
                "SELECT position,document FROM agent_workflow_entries WHERE run_id=? AND entry_id=?",
                (run["id"], entry["id"]),
            ).fetchone()
            if previous:
                document = {**json.loads(previous["document"]), **entry}
                position = previous["position"]
            else:
                position = connection.execute(
                    "SELECT COALESCE(MAX(position),0)+1 FROM agent_workflow_entries WHERE run_id=?",
                    (run["id"],),
                ).fetchone()[0]
                if position > 256:
                    raise ApiError(409, "agent_step_limit", "工作流步骤达到上限")
                document = {"started_at": self.now(), **entry}
            document["updated_at"] = self.now()
            connection.execute(
                "INSERT INTO agent_workflow_entries(run_id,entry_id,position,document) VALUES(?,?,?,?) ON CONFLICT(run_id,entry_id) DO UPDATE SET document=excluded.document",
                (run["id"], entry["id"], position, canonical(document)),
            )
            return document

    def list_runs(self, user_id: str, conversation: str) -> list[str]:
        self.cleanup()
        with self.database.transaction() as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM agent_chat_runs WHERE user_id=? AND conversation_id=? AND purged=0 ORDER BY created_at DESC LIMIT 20",
                    (user_id, conversation),
                )
            ]

    @staticmethod
    def public_interaction(row: dict) -> dict:
        document = json.loads(row["document"])
        return {
            "id": row["id"],
            "version": row["version"],
            "status": row["status"],
            "expires_at": row["expires_at"],
            **{
                key: document[key]
                for key in (
                    "title",
                    "kind",
                    "confirmation_required",
                    "effect",
                    "fields",
                    "target",
                    "description",
                )
            },
        }

    def interaction(self, run_id: str, interaction_id: str, user_id: str) -> dict:
        self.raw(run_id, user_id)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM agent_interactions WHERE id=? AND run_id=?",
                (interaction_id, run_id),
            ).fetchone()
            if not row:
                raise ApiError(404, "INTERACTION_NOT_FOUND", "待处理操作不存在")
            return dict(row)

    def for_call(self, run_id: str, call_key: str) -> dict | None:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM agent_interactions WHERE run_id=? AND call_key=? AND status IN ('pending','submitted','consumed') ORDER BY created_at DESC,id DESC LIMIT 1",
                (run_id, call_key),
            ).fetchone()
            return dict(row) if row else None

    def prepare(
        self,
        run: dict,
        call_key: str,
        tool_call_id: str,
        operation: str,
        revision: str,
        document: dict,
        *,
        replace: dict | None = None,
        request_id: str | None = None,
        request_digest: str | None = None,
    ) -> dict:
        now = self.now()
        with self.database.transaction(write=True) as connection:
            current = self._owned(connection, run["id"], run["user_id"])
            if current["cancel_requested"] or current["status"] not in {
                "running",
                "waiting",
                "interrupted",
            }:
                raise ApiError(409, "RUN_CANCELLED", "任务已停止")
            if replace:
                previous = connection.execute(
                    "SELECT submission_id,submission_digest FROM agent_interactions WHERE id=?",
                    (replace["id"],),
                ).fetchone()
                if request_id and previous["submission_id"] == request_id:
                    if previous["submission_digest"] != request_digest:
                        raise ApiError(
                            409, "IDEMPOTENCY_CONFLICT", "请求标识已用于不同参数"
                        )
                    return replace
                changed = connection.execute(
                    "UPDATE agent_interactions SET status='invalidated',submission_id=?,submission_digest=? WHERE id=? AND version=? AND status='pending'",
                    (request_id, request_digest, replace["id"], replace["version"]),
                ).rowcount
                if not changed:
                    raise ApiError(
                        412,
                        "INTERACTION_VERSION_CONFLICT",
                        "操作已变化，请载入最新状态",
                    )
            interaction_id = identity("int")
            connection.execute(
                """INSERT INTO agent_interactions(id,run_id,call_key,tool_call_id,operation_id,catalog_revision,document,status,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?,'pending',?,?)""",
                (
                    interaction_id,
                    run["id"],
                    call_key,
                    tool_call_id,
                    operation,
                    revision,
                    canonical(document),
                    now,
                    min(
                        current["expires_at"],
                        now + self.settings.hitl_interaction_seconds,
                    ),
                ),
            )
            connection.execute(
                "UPDATE agent_chat_runs SET version=version+1,updated_at=? WHERE id=?",
                (now, run["id"]),
            )
            self.audit(
                connection,
                run,
                "agent.interaction.prepared",
                interaction_id=interaction_id,
                operation_id=operation,
                tool_call_id=tool_call_id,
                catalog_revision=revision,
            )
            return dict(
                connection.execute(
                    "SELECT * FROM agent_interactions WHERE id=?", (interaction_id,)
                ).fetchone()
            )

    def submit(
        self,
        run: dict,
        interaction: dict,
        submission_id: str,
        expected_version: int,
        arguments: dict,
        fingerprint: str,
    ) -> tuple[dict, bool]:
        now = self.now()
        with self.database.transaction(write=True) as connection:
            current = self._owned(connection, run["id"], run["user_id"])
            row = connection.execute(
                "SELECT * FROM agent_interactions WHERE id=? AND run_id=?",
                (interaction["id"], run["id"]),
            ).fetchone()
            if row["submission_id"] == submission_id:
                if row["submission_digest"] != fingerprint:
                    raise ApiError(
                        409, "IDEMPOTENCY_CONFLICT", "此提交标识已用于不同参数"
                    )
                return current, False
            if (
                current["purged"]
                or current["expires_at"] <= now
                or row["expires_at"] <= now
            ):
                raise ApiError(
                    410, "INTERACTION_EXPIRED", "待处理操作已过期，请重新发起"
                )
            if row["version"] != expected_version:
                raise ApiError(
                    412, "INTERACTION_VERSION_CONFLICT", "操作已变化，请载入最新状态"
                )
            if row["status"] != "pending" or current["status"] not in {
                "waiting",
                "interrupted",
            }:
                raise ApiError(
                    409,
                    "INTERACTION_ALREADY_RESOLVED",
                    "操作已提交或结束，请查看最新状态",
                )
            document = json.loads(row["document"])
            document["arguments"] = arguments
            from agent_permissions.hitl_forms import read_pointer

            for field in document["fields"]:
                present, value = read_pointer(arguments, field["pointer"])
                field["present"] = present and not field["sensitive"]
                if field["present"]:
                    field["value"] = value
                else:
                    field.pop("value", None)
            connection.execute(
                "UPDATE agent_interactions SET status='submitted',version=version+1,submission_id=?,submission_digest=?,document=? WHERE id=?",
                (submission_id, fingerprint, canonical(document), row["id"]),
            )
            lease = identity("lease")
            connection.execute(
                "UPDATE agent_chat_runs SET status='running',version=version+1,lease_token=?,lease_until=?,updated_at=?,error=NULL WHERE id=?",
                (lease, now + LEASE_SECONDS, now, run["id"]),
            )
            self.audit(
                connection,
                run,
                "agent.interaction.submitted",
                interaction_id=row["id"],
                operation_id=row["operation_id"],
                version=expected_version,
            )
            return self._owned(connection, run["id"], run["user_id"]), True

    def recover(self, run_id: str, user_id: str) -> tuple[dict, bool]:
        """Claim an interrupted graph without re-sending the user's original task."""
        self.cleanup()
        now = self.now()
        with self.database.transaction(write=True) as connection:
            run = self._owned(connection, run_id, user_id)
            if run["graph_version"] != GRAPH_VERSION:
                raise ApiError(
                    409, "RUN_VERSION_MISMATCH", "任务版本已变化，请重新发起"
                )
            if run["purged"] or run["expires_at"] <= now:
                raise ApiError(410, "INTERACTION_EXPIRED", "任务已过期")
            if run["status"] not in {"interrupted", "failed"}:
                return run, False
            if run["cancel_requested"]:
                raise ApiError(409, "RUN_CANCELLED", "任务已取消")
            if connection.execute(
                "SELECT 1 FROM agent_chat_runs WHERE user_id=? AND conversation_id=? AND id<>? AND status IN ('running','waiting')",
                (user_id, run["conversation_id"], run_id),
            ).fetchone():
                raise ApiError(
                    409, "RUN_ALREADY_ACTIVE", "请先处理当前会话中的另一个任务"
                )
            self.close_workflow(connection, run_id, "interrupted")
            pending = connection.execute(
                "SELECT * FROM agent_interactions WHERE run_id=? AND status IN ('pending','submitted') ORDER BY created_at DESC,id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if pending and pending["expires_at"] <= now:
                raise ApiError(410, "INTERACTION_EXPIRED", "待处理操作已过期")
            if pending:
                receipt = connection.execute(
                    "SELECT status FROM agent_tool_executions WHERE interaction_id=?",
                    (pending["id"],),
                ).fetchone()
                if receipt is None or receipt["status"] == "executing":
                    # All enabled writes have atomic receipts. An uncommitted execution is safe to re-confirm.
                    if pending["status"] == "submitted":
                        connection.execute(
                            "UPDATE agent_interactions SET status='invalidated' WHERE id=?",
                            (pending["id"],),
                        )
                        connection.execute(
                            """INSERT INTO agent_interactions(id,run_id,call_key,tool_call_id,operation_id,catalog_revision,document,status,created_at,expires_at)
                        VALUES(?,?,?,?,?,?,?,'pending',?,?)""",
                            (
                                identity("int"),
                                run_id,
                                pending["call_key"],
                                pending["tool_call_id"],
                                pending["operation_id"],
                                pending["catalog_revision"],
                                pending["document"],
                                now,
                                pending["expires_at"],
                            ),
                        )
                    connection.execute(
                        "DELETE FROM agent_tool_executions WHERE run_id=? AND call_key=? AND status='executing'",
                        (run_id, pending["call_key"]),
                    )
                    connection.execute(
                        "UPDATE agent_chat_runs SET status='waiting',lease_token=NULL,version=version+1,error=NULL,updated_at=? WHERE id=?",
                        (now, run_id),
                    )
                    entry_id = "tool:" + pending["call_key"]
                    workflow = connection.execute(
                        "SELECT document FROM agent_workflow_entries WHERE run_id=? AND entry_id=?",
                        (run_id, entry_id),
                    ).fetchone()
                    if workflow:
                        entry = json.loads(workflow["document"])
                        entry.update(
                            status="waiting",
                            phase="waiting",
                            detail="等待填写参数或确认执行",
                            updated_at=now,
                        )
                        connection.execute(
                            "UPDATE agent_workflow_entries SET document=? WHERE run_id=? AND entry_id=?",
                            (canonical(entry), run_id, entry_id),
                        )
                    self.audit(connection, run, "agent.run.recovered", status="waiting")
                    return self._owned(connection, run_id, user_id), False
            connection.execute(
                "UPDATE agent_chat_runs SET status='running',lease_token=?,lease_until=?,version=version+1,error=NULL,updated_at=? WHERE id=?",
                (identity("lease"), now + LEASE_SECONDS, now, run_id),
            )
            self.audit(connection, run, "agent.run.recovered", status="running")
            return self._owned(connection, run_id, user_id), True

    def update(
        self,
        run: dict,
        *,
        status: str | None = None,
        output: str | None = None,
        usage: dict | None = None,
        error: dict | None = None,
        finish: str | None = None,
    ) -> None:
        with self.database.transaction(write=True) as connection:
            values: dict = {"updated_at": self.now()}
            if status is not None:
                current = self._owned(connection, run["id"], run["user_id"])
                if current["cancel_requested"]:
                    status = "cancelled"
                values.update(status=status, lease_token=None)
            if output is not None:
                values["output"] = output
            if usage is not None:
                values["usage"] = canonical(usage)
            if finish is not None:
                values["finish_reason"] = finish
            if error is not None:
                values["error"] = canonical(error)
            changed = connection.execute(
                "UPDATE agent_chat_runs SET "
                + ",".join(f"{key}=?" for key in values)
                + ",version=version+1 WHERE id=? AND lease_token=?",
                (*values.values(), run["id"], run["lease_token"]),
            ).rowcount
            if changed and status in {
                "completed",
                "failed",
                "cancelled",
                "interrupted",
                "needs_reconciliation",
            }:
                self.close_workflow(connection, run["id"], status)

    def close_workflow(self, connection: Connection, run_id: str, status: str) -> None:
        rows = connection.execute(
            "SELECT entry_id,document FROM agent_workflow_entries WHERE run_id=?",
            (run_id,),
        ).fetchall()
        for row in rows:
            entry = json.loads(row["document"])
            if entry["status"] not in {"running", "waiting", "pending"}:
                continue
            entry["status"] = (
                "interrupted"
                if status in {"interrupted", "needs_reconciliation"}
                else "cancelled"
                if status in {"cancelled", "completed"} or entry["status"] == "pending"
                else "failed"
            )
            entry["updated_at"] = self.now()
            if entry["status"] == "interrupted":
                entry["detail"] = "执行中断，读取状态后可继续"
            elif entry["status"] == "failed":
                entry["detail"] = "本步骤未完成，请查看任务错误"
            elif status == "completed":
                entry["detail"] = "任务已结束，此步骤未执行"
            else:
                entry["detail"] = "本次步骤已取消"
            connection.execute(
                "UPDATE agent_workflow_entries SET document=? WHERE run_id=? AND entry_id=?",
                (canonical(entry), run_id, row["entry_id"]),
            )

    def assert_running(self, run: dict) -> None:
        with self.database.transaction() as connection:
            row = self._owned(connection, run["id"], run["user_id"])
            if (
                row["cancel_requested"]
                or row["status"] != "running"
                or row["lease_token"] != run["lease_token"]
                or row["expires_at"] <= self.now()
            ):
                raise ApiError(409, "RUN_CANCELLED", "任务已停止")

    def cancel(self, run_id: str, user_id: str) -> dict:
        self.cleanup()
        with self.database.transaction(write=True) as connection:
            run = self._owned(connection, run_id, user_id)
            if run["status"] in {"waiting", "interrupted", "running", "failed"}:
                connection.execute(
                    "UPDATE agent_interactions SET status='cancelled' WHERE run_id=? AND status='pending'",
                    (run_id,),
                )
                connection.execute(
                    "UPDATE agent_chat_runs SET status='cancelled',cancel_requested=1,version=version+1,updated_at=? WHERE id=?",
                    (self.now(), run_id),
                )
                self.close_workflow(connection, run_id, "cancelled")
            if (
                run["status"] in {"waiting", "interrupted", "running", "failed"}
                and not run["cancel_requested"]
            ):
                self.audit(connection, run, "agent.run.cancelled")
        return self.snapshot(run_id, user_id)

    def execution(self, run_id: str, call_key: str) -> dict | None:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM agent_tool_executions WHERE run_id=? AND call_key=?",
                (run_id, call_key),
            ).fetchone()
            return dict(row) if row else None

    def start_execution(
        self,
        run: dict,
        call_key: str,
        operation: str,
        arguments_digest: str,
        interaction_id: str | None,
    ) -> dict:
        with self.database.transaction(write=True) as connection:
            connection.execute(
                """INSERT INTO agent_tool_executions(id,run_id,interaction_id,call_key,operation_id,arguments_digest,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,'executing',?,?) ON CONFLICT(run_id,call_key) DO NOTHING""",
                (
                    identity("exe"),
                    run["id"],
                    interaction_id,
                    call_key,
                    operation,
                    arguments_digest,
                    self.now(),
                    self.now(),
                ),
            )
            row = dict(
                connection.execute(
                    "SELECT * FROM agent_tool_executions WHERE run_id=? AND call_key=?",
                    (run["id"], call_key),
                ).fetchone()
            )
            if row["arguments_digest"] != arguments_digest:
                raise ApiError(
                    409, "IDEMPOTENCY_CONFLICT", "执行参数与已接受操作不一致"
                )
            return row

    def finish_execution(self, execution_id: str, result: dict) -> None:
        with self.database.transaction(write=True) as connection:
            saved = connection.execute(
                "SELECT e.*,r.user_id FROM agent_tool_executions e JOIN agent_chat_runs r ON r.id=e.run_id WHERE e.id=?",
                (execution_id,),
            ).fetchone()
            if saved["status"] == "succeeded" and not result.get("ok"):
                return
            connection.execute(
                "UPDATE agent_tool_executions SET result=?,status=?,updated_at=? WHERE id=?",
                (
                    canonical(result),
                    "succeeded" if result.get("ok") else "failed",
                    self.now(),
                    execution_id,
                ),
            )
            connection.execute(
                "UPDATE agent_interactions SET status='consumed' WHERE id=(SELECT interaction_id FROM agent_tool_executions WHERE id=?)",
                (execution_id,),
            )
            self.audit(
                connection,
                {"id": saved["run_id"], "user_id": saved["user_id"]},
                "agent.tool.completed",
                execution_id=execution_id,
                interaction_id=saved["interaction_id"],
                operation_id=saved["operation_id"],
                status="succeeded" if result.get("ok") else "failed",
            )
