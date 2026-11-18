"""Collect trusted facts at the decision point and write them to the business outbox."""

import hashlib
import logging
import re
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
from starlette.concurrency import run_in_threadpool

from agent_permissions.audit_contract import Event, next_state, utc
from agent_permissions.errors import ApiError
from agent_permissions.permissions import roles_for
from agent_permissions.storage import DatabaseBackend

logger = logging.getLogger(__name__)
DB_ERRORS = (sqlite3.Error, psycopg.Error)
CHANGE_FIELDS = {
    "username",
    "display_name",
    "email",
    "roles",
    "status",
    "description",
    "permissions",
    "deleted_at",
    "version",
}


def new_id(prefix):
    return f"{prefix}_{uuid4().hex}"


def entrypoint(request):
    path = request.url.path
    if path == "/api/v1/ag-ui":
        return "ag_ui"
    if path.startswith("/v1/"):
        return "chat_completions"
    return "chat_runs"


@dataclass
class Call:
    database: DatabaseBackend
    clock: Callable[[], datetime]
    entrypoint: str
    fields: dict
    call_id: str = field(default_factory=lambda: new_id("call"))
    attempt_id: str = field(default_factory=lambda: new_id("attempt"))
    key: str | None = None
    stage: str = "runtime"
    grant: dict = field(default_factory=dict)
    checks: list = field(default_factory=list)
    dispatched: bool = False
    checks_recorded: bool = False

    def bind(self, tool, revision):
        if tool:
            binding = tool["binding"]
            op = binding["operationId"]
            target = (
                "permission"
                if op == "permission.list"
                else "session"
                if op.startswith("auth.")
                else op.split(".")[0]
            )
            self.fields.update(
                operation_id=op,
                tool_name=tool["definition"]["name"],
                http_method=binding["method"],
                route_template=binding["path"],
                effect=binding["policy"]["effect"],
                target_type=target
                if target in {"permission", "session", "user", "role"}
                else None,
                catalog_revision=revision,
            )

    def requested_target(self, arguments):
        if "{user_id}" in self.fields.get("route_template", "") and isinstance(
            arguments, dict
        ):
            path = arguments.get("path")
            user = path.get("user_id") if isinstance(path, dict) else None
            if isinstance(user, str) and re.fullmatch(r"usr_[0-9a-f]{32}", user):
                self.fields["requested_target_id"] = user

    def identify(self, user_id):
        if user_id:
            self.fields.update(actor_user_id=user_id, subject_user_id=user_id)

    def write(self, conn, kind, *, fact=None, **details):
        try:
            conn.execute(
                """INSERT INTO agent_audit_call_refs(call_id,run_id,call_key,entrypoint)
                VALUES(?,?,?,?) ON CONFLICT(call_id) DO NOTHING""",
                (self.call_id, self.fields.get("run_id"), self.key, self.entrypoint),
            )
            fact_key = self.call_id + ":" + fact if fact else None
            if (
                fact_key
                and conn.execute(
                    "SELECT 1 FROM agent_audit_outbox WHERE fact_key=?", (fact_key,)
                ).fetchone()
            ):
                return
            row = conn.execute(
                """UPDATE agent_audit_call_refs SET sequence=sequence+1
                WHERE call_id=? RETURNING sequence,entrypoint,state""",
                (self.call_id,),
            ).fetchone()
            state = next_state(row[2], kind)
            if state != row[2]:
                conn.execute(
                    "UPDATE agent_audit_call_refs SET state=? WHERE call_id=?",
                    (state, self.call_id),
                )
            now = self.clock()
            event = Event(
                event_type=kind,
                event_id=new_id("evt"),
                sequence=row[0],
                entrypoint=row[1],
                occurred_at=utc(now),
                call_id=self.call_id,
                attempt_id=self.attempt_id,
                **({"permission_checks": list(self.checks)} | self.fields | details),
            )
            conn.execute(
                """INSERT INTO agent_audit_outbox(event_id,call_id,fact_key,payload,created_at,available_at)
                VALUES(?,?,?,?,?,0)""",
                (
                    event.event_id,
                    self.call_id,
                    fact_key,
                    event.model_dump_json(exclude_none=True, exclude_defaults=True),
                    now.timestamp(),
                ),
            )
        except DB_ERRORS:
            raise ApiError(503, "AUDIT_UNAVAILABLE", "审计记录暂时不可用") from None

    def record(self, kind, **details):
        try:
            with self.database.transaction(write=True) as conn:
                self.write(conn, kind, **details)
            if "permission_checks" not in details:
                self.checks_recorded |= bool(self.checks)
                self.checks.clear()
        except DB_ERRORS:
            raise ApiError(503, "AUDIT_UNAVAILABLE", "审计记录暂时不可用") from None

    async def emit(self, kind, **details):
        await run_in_threadpool(self.record, kind, **details)

    async def reject(self, error, *, control=True):
        if not self.checks and not self.checks_recorded:
            authenticated = (
                "denied"
                if error.status == 401
                else "allowed"
                if self.fields.get("actor_user_id")
                else "not_evaluated"
            )
            self.checks.append(
                {
                    "stage": self.stage,
                    "check": "authentication",
                    "decision": authenticated,
                }
            )
            self.checks.extend(
                {"stage": self.stage, "check": kind, "decision": "not_evaluated"}
                for kind in ("user_grant", "rbac")
            )
        kind = (
            "result.unknown"
            if self.dispatched and error.status >= 500
            else "attempt.rejected"
        )
        try:
            await self.emit(
                kind,
                reason_code=error.code,
                **({"control_http_status": error.status} if control else {}),
            )
        except ApiError:
            if error.status not in (401, 403):
                raise
            logger.error(
                "Agent audit unavailable for denial request_id=%s",
                self.fields.get("control_request_id"),
            )


form_read = ContextVar("agent_audit_form_read", default=False)
active: ContextVar[Call | None] = ContextVar("agent_audit_call", default=None)


@contextmanager
def scope(call):
    token = active.set(call)
    try:
        yield call
    finally:
        active.reset(token)


async def detached_rejection(request, error):
    # The supplied task/card IDs have not passed ownership checks.
    store = request.app.state.run_store
    call = Call(
        store.database,
        store.clock,
        entrypoint(request),
        {"purpose": "operation", "control_request_id": request.state.info.request_id},
        stage="confirmation",
    )
    call.identify(request.state.info.actor_user_id)
    await call.reject(error)


def runtime_call(request, run, call_key, tool=None, revision=None):
    call = Call(
        request.app.state.database,
        request.app.state.service.clock,
        entrypoint(request),
        {
            "purpose": "operation",
            "run_id": run["id"],
            "agent_id": "agent_permissions.chat",
            "control_request_id": request.state.info.request_id,
        },
    )
    call.identify(run["user_id"])
    identify_run_call(call, run["id"], call_key)
    call.bind(tool, revision)
    return call


def identify_run_call(call, run_id, call_key):
    call.key = hashlib.sha256(call_key.encode()).hexdigest()
    call.call_id = (
        "call_"
        + uuid5(NAMESPACE_URL, "agent_permissions/" + run_id + "/" + call.key).hex
    )


def grant_snapshot(conn, user, operation):
    row = conn.execute(
        """SELECT enabled,updated_by,updated_at FROM user_agent_tool_grants
        WHERE user_id=? AND operation_id=?""",
        (user["id"], operation),
    ).fetchone()
    return {
        "grant_enabled": bool(row and row[0]),
        "grant_version": user["agent_tools_version"],
        "grant_changed_by_user_id": row[1] if row else None,
        "grant_changed_at": row[2] if row else None,
        "roles": roles_for(conn, user["id"]),
    }


def rbac_check(
    stage, all_codes, any_codes, granted, roles, *, evaluated=True, check="rbac"
):
    required = set(all_codes) | set(any_codes)
    allowed = set(all_codes) <= granted and (
        not any_codes or bool(set(any_codes) & granted)
    )
    return {
        "stage": stage,
        "check": check,
        "decision": ("allowed" if allowed else "denied")
        if evaluated
        else "not_evaluated",
        "roles": roles,
        "required_all": sorted(all_codes),
        "required_any": sorted(any_codes),
        "matched": sorted(required & granted) if evaluated else [],
        "missing": sorted(required - granted) if evaluated else [],
    }


def tool_checks(call, tool, context, operations):
    policy = tool["binding"]["policy"]
    stage = call.stage
    if policy["authentication"] == "none":
        call.checks.extend(
            {"stage": stage, "check": check, "decision": "not_applicable"}
            for check in ("authentication", "user_grant", "rbac")
        )
        return
    authenticated = context.authenticated
    allowed = tool["binding"]["operationId"] in operations
    call.checks.append(
        {
            "stage": stage,
            "check": "authentication",
            "decision": "allowed" if authenticated else "denied",
        }
    )
    call.checks.append(
        {
            "stage": stage,
            "check": "user_grant",
            "decision": ("allowed" if allowed else "denied")
            if authenticated
            else "not_evaluated",
            **call.grant,
        }
    )
    call.checks.append(
        rbac_check(
            stage,
            policy["allPermissions"],
            policy["anyPermissions"],
            set(context.permissions),
            call.grant.get("roles", []),
            evaluated=authenticated and allowed,
        )
    )


def business_audit(action, event_id, target, changes, actor):
    call = active.get()
    if (
        not call
        or call.stage != "business"
        or call.fields.get("operation_id") != action
    ):
        return
    call.identify(actor)
    call.fields.update(
        business_audit_event_id=event_id, target_id=target or changes.get("role_code")
    )
    fields = set(changes["fields"]) if "fields" in changes else set(changes)
    call.fields["change_summary"] = {"fields": sorted(fields & CHANGE_FIELDS)}
    if "fields" in changes:
        call.fields["change_summary"]["no_change"] = not bool(changes["fields"])
    if changes.get("password_changed") is True:
        call.fields["change_summary"]["password_changed"] = True


def committed(conn):
    call = active.get()
    if call and call.stage == "business":
        call.write(conn, "write.committed", fact="committed")


def grant_changes(service, conn, info, user_id, version, changes, business_event):
    for change in changes:
        fields = {
            "purpose": "grant_change",
            "actor_user_id": info.actor_user_id,
            "subject_user_id": user_id,
            "operation_id": change["operation_id"],
            "grant_version": version,
            "business_audit_event_id": business_event,
            "control_request_id": info.request_id,
            "change_summary": {"before": change["before"], "after": change["after"]},
        }
        call = Call(service.database, service.clock, "grant_admin", fields)
        call.write(conn, "grant.changed", fact="grant")
