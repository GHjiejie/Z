"""Unforgeable in-process execution context and transactional business receipts."""

import json
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from agent_permissions.errors import ApiError
from agent_permissions.storage import Connection


@dataclass(frozen=True)
class ConfirmedExecution:
    execution_id: str
    user_id: str
    operation_id: str
    arguments_digest: str
    lease_token: str


confirmed_execution: ContextVar[ConfirmedExecution | None] = ContextVar(
    "confirmed_execution", default=None
)

# These business services record their result in the same transaction as the write.
# New write operations must implement that contract before Agent execution is enabled.
TRANSACTIONAL_OPERATIONS = {
    "auth.logout",
    "user.create",
    "user.self.update",
    "user.self.password.change",
    "user.update",
    "user.delete",
    "user.status.update",
    "user.password.reset",
    "user.roles.assign",
    "role.create",
    "role.update",
    "role.delete",
}


def guard_execution(
    connection: Connection, operation: str, user_id: str, now: float
) -> tuple[bool, Any]:
    """Check delegation and cancellation again inside the original write transaction."""
    context = confirmed_execution.get()
    if context is None:
        return False, None
    row = connection.execute(
        """SELECT e.*,r.user_id,r.cancel_requested,r.expires_at,r.lease_token,r.lease_until
        FROM agent_tool_executions e JOIN agent_chat_runs r ON r.id=e.run_id WHERE e.id=?""",
        (context.execution_id,),
    ).fetchone()
    if (
        not row
        or user_id != context.user_id
        or row["user_id"] != user_id
        or operation != context.operation_id
        or row["operation_id"] != operation
        or row["arguments_digest"] != context.arguments_digest
    ):
        raise ApiError(403, "INVALID_EXECUTION_CONTEXT", "执行确认不匹配")
    if row["status"] == "succeeded":
        return True, json.loads(row["service_result"])
    if (
        row["cancel_requested"]
        or row["status"] != "executing"
        or row["expires_at"] <= now
        or row["lease_until"] <= now
        or row["lease_token"] != context.lease_token
    ):
        raise ApiError(409, "RUN_CANCELLED", "任务已停止或执行上下文已失效")
    grant = connection.execute(
        "SELECT enabled FROM user_agent_tool_grants WHERE user_id=? AND operation_id=?",
        (user_id, operation),
    ).fetchone()
    if not grant or not grant[0]:
        raise ApiError(403, "TOOL_NOT_GRANTED", "当前用户未允许 Agent 调用此工具")
    return False, None


def record_execution(connection: Connection, value: Any, now: float) -> Any:
    """Persist the service return value atomically with its business mutation."""
    context = confirmed_execution.get()
    if context is not None:
        connection.execute(
            "UPDATE agent_tool_executions SET status='succeeded',service_result=?,updated_at=? WHERE id=?",
            (
                json.dumps(value, ensure_ascii=False, allow_nan=False),
                now,
                context.execution_id,
            ),
        )
    return value
