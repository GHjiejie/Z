"""Independent audit reader, idempotent inbox and rebuildable operation index."""

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import Field, field_validator

from agent_permissions.audit_contract import (
    TERMINAL_STATES,
    Batch,
    Identifier,
    Outcome,
    Strict,
    utc,
)
from agent_permissions.audit_schema import STORE_SCHEMA
from agent_permissions.database import Database
from agent_permissions.errors import ApiError
from agent_permissions.storage import DatabaseBackend


class Page(Strict):
    actor_user_id: Identifier | None = None
    subject_user_id: Identifier | None = None
    operation_id: Identifier | None = None
    started_from: datetime | None = None
    started_to: datetime | None = None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=512)

    @field_validator("started_from", "started_to")
    @classmethod
    def timezone_required(cls, value):
        if value is not None:
            utc(value)
        return value


class OperationQuery(Page):
    agent_id: Identifier | None = None
    target_id: Identifier | None = None
    run_id: Identifier | None = None
    business_outcome: Outcome | None = None
    include_children: bool = False


class GrantQuery(Page):
    grant_version: int | None = Field(default=None, ge=1)


def encode(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def projection(events: list[dict]) -> dict:
    summary = {
        "call_id": events[0]["call_id"],
        "started_at": events[0]["occurred_at"],
        "dispatch_state": "not_sent",
        "business_outcome": "not_executed",
        "status": "requested",
    }
    proof_attempt = None
    for event in events:
        kind, at = event["event_type"], event["occurred_at"]
        if proof_attempt and event["attempt_id"] != proof_attempt:
            continue
        for field in (
            "actor_user_id",
            "subject_user_id",
            "agent_id",
            "entrypoint",
            "purpose",
            "parent_call_id",
            "run_id",
            "interaction_id",
            "interaction_version",
            "interaction_action",
            "execution_id",
            "operation_id",
            "tool_name",
            "effect",
            "http_method",
            "route_template",
            "target_type",
            "requested_target_id",
            "target_id",
            "catalog_revision",
            "control_request_id",
            "business_request_id",
            "business_http_status",
            "control_http_status",
            "result_summary",
            "change_summary",
            "duration_ms",
            "business_audit_event_id",
            "confirmed_by_user_id",
        ):
            if field in event:
                summary[field] = event[field]
        for check in event.get("permission_checks", []):
            if check["check"] == "user_grant" and check.get("grant_version"):
                for field in (
                    "grant_version",
                    "grant_enabled",
                    "grant_changed_at",
                    "grant_changed_by_user_id",
                ):
                    if field in check:
                        summary[field] = check[field]
        if kind == "dispatch.attempted" and summary["dispatch_state"] == "not_sent":
            summary["dispatch_state"] = "attempted"
            summary["business_outcome"] = "unknown"
        if kind == "business.received":
            summary["dispatch_state"] = "received"
            if not proof_attempt:
                summary["business_outcome"] = "unknown"
        if proof_attempt:
            continue
        if kind in {"dispatch.attempted", "business.received"}:
            summary["status"] = "executing"
        if kind in {"waiting", "confirmed"}:
            summary.update(status=kind)
            if kind == "confirmed":
                summary["confirmed_at"] = at
                summary.pop("reason_code", None)
        elif kind in {"cancelled", "expired"}:
            summary.update(status=kind, finished_at=at)
        elif kind == "attempt.rejected":
            summary["reason_code"] = event.get("reason_code")
            if (
                summary["status"] not in {"waiting", "cancelled", "expired"}
                and summary["dispatch_state"] == "not_sent"
            ):
                summary.update(status="rejected", finished_at=at)
        elif kind in {"read.succeeded", "write.committed", "receipt.reconciled"}:
            summary.update(
                status="completed",
                business_outcome="succeeded",
                dispatch_state="received",
                finished_at=at,
            )
            summary.pop("reason_code", None)
            proof_attempt = event["attempt_id"]
        elif kind == "business.failed" or (
            kind == "result.unknown" and summary["business_outcome"] != "failed"
        ):
            summary.update(
                status="failed"
                if kind == "business.failed"
                else "needs_reconciliation",
                business_outcome="failed" if kind == "business.failed" else "unknown",
                reason_code=event.get("reason_code"),
            )
            if kind == "business.failed":
                summary["finished_at"] = at
    if summary["status"] not in TERMINAL_STATES:
        summary.pop("finished_at", None)
    return summary


class AuditStore:
    def __init__(self, database: DatabaseBackend):
        self.database = database

    def initialize(self, *, managed=False):
        if isinstance(self.database, Database):
            self.database.path.parent.mkdir(parents=True, exist_ok=True)
        self.database.open()
        if managed:
            with self.database.transaction() as conn:
                conn.execute("SELECT 1 FROM agent_audit_events LIMIT 0")
            return
        with self.database.transaction(write=True) as conn:
            Database._execute_schema(conn, STORE_SCHEMA)

    def receive(self, batch: Batch) -> list[dict]:
        acknowledgements = []
        changed_calls = set()
        with self.database.transaction(write=True) as conn:
            for event in batch.events:
                doc = event.model_dump(exclude_none=True, exclude_defaults=True)
                payload = encode(doc)
                retired = conn.execute(
                    """SELECT event_id,producer_id,payload_hash FROM agent_audit_retired_events
                    WHERE (producer_id=? AND event_id=?) OR call_id=? ORDER BY CASE WHEN event_id=? THEN 0 ELSE 1 END LIMIT 1""",
                    (batch.producer_id, event.event_id, event.call_id, event.event_id),
                ).fetchone()
                if retired:
                    duplicate = (
                        retired[0] == event.event_id
                        and retired[1] == batch.producer_id
                        and retired[2] == hashlib.sha256(payload.encode()).hexdigest()
                    )
                    acknowledgements.append(
                        {
                            "event_id": event.event_id,
                            "status": "duplicate" if duplicate else "conflict",
                        }
                    )
                    continue
                if event.event_type == "grant.changed":
                    prior = conn.execute(
                        """SELECT g.call_id,e.event_id FROM agent_audit_grant_changes g JOIN agent_audit_events e ON e.call_id=g.call_id
                        WHERE (g.subject_user_id=? AND g.operation_id=? AND g.grant_version=?) OR g.call_id=?""",
                        (
                            event.subject_user_id,
                            event.operation_id,
                            event.grant_version,
                            event.call_id,
                        ),
                    ).fetchone()
                    if prior and (
                        prior[0] != event.call_id or prior[1] != event.event_id
                    ):
                        acknowledgements.append(
                            {"event_id": event.event_id, "status": "conflict"}
                        )
                        continue
                accepted = conn.execute(
                    """INSERT INTO agent_audit_events(producer_id,event_id,call_id,sequence,schema_version,received_at,payload)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                    (
                        batch.producer_id,
                        event.event_id,
                        event.call_id,
                        event.sequence,
                        batch.schema_version,
                        utc(datetime.now(UTC)),
                        payload,
                    ),
                ).rowcount
                if accepted:
                    if event.event_type == "grant.changed":
                        conn.execute(
                            """INSERT INTO agent_audit_grant_changes(call_id,started_at,actor_user_id,subject_user_id,operation_id,grant_version,document)
                            VALUES(?,?,?,?,?,?,?)""",
                            (
                                event.call_id,
                                event.occurred_at,
                                event.actor_user_id,
                                event.subject_user_id,
                                event.operation_id,
                                event.grant_version,
                                payload,
                            ),
                        )
                    else:
                        changed_calls.add(event.call_id)
                    status = "saved"
                else:
                    prior = conn.execute(
                        "SELECT payload FROM agent_audit_events WHERE producer_id=? AND event_id=?",
                        (batch.producer_id, event.event_id),
                    ).fetchone()
                    status = (
                        "duplicate" if prior and prior[0] == payload else "conflict"
                    )
                acknowledgements.append({"event_id": event.event_id, "status": status})
            for call_id in changed_calls:
                self._index(conn, call_id)
        return acknowledgements

    def _index(self, conn, call_id):
        rows = conn.execute(
            "SELECT payload FROM agent_audit_events WHERE call_id=? ORDER BY sequence",
            (call_id,),
        )
        summary = projection([json.loads(row[0]) for row in rows])
        columns = (
            "call_id",
            "parent_call_id",
            "started_at",
            "finished_at",
            "actor_user_id",
            "subject_user_id",
            "agent_id",
            "operation_id",
            "target_id",
            "run_id",
            "business_outcome",
            "document",
        )
        summary_values = [summary.get(k) for k in columns[:-1]] + [encode(summary)]
        conn.execute(
            f"""INSERT INTO agent_audit_operations({",".join(columns)}) VALUES({",".join("?" for _ in columns)})
            ON CONFLICT(call_id) DO UPDATE SET {",".join(f"{k}=excluded.{k}" for k in columns[1:])}""",
            summary_values,
        )

    def detail(self, call_id: str) -> dict:
        with self.database.transaction() as conn:
            row = conn.execute(
                "SELECT document FROM agent_audit_operations WHERE call_id=?",
                (call_id,),
            ).fetchone()
            if not row:
                raise ApiError(404, "AUDIT_OPERATION_NOT_FOUND", "未找到该操作")
            events = [
                {
                    **json.loads(r[0]),
                    "received_at": r[1],
                    "producer_id": r[2],
                    "schema_version": r[3],
                }
                for r in conn.execute(
                    "SELECT payload,received_at,producer_id,schema_version FROM agent_audit_events WHERE call_id=? ORDER BY sequence",
                    (call_id,),
                )
            ]
            children = [
                json.loads(r[0])
                for r in conn.execute(
                    "SELECT document FROM agent_audit_operations WHERE parent_call_id=? ORDER BY started_at,call_id",
                    (call_id,),
                )
            ]
        summary = json.loads(row[0])
        attempts = {}
        checks = []
        for event in events:
            attempts.setdefault(event["attempt_id"], []).append(event)
            for check in event.get("permission_checks", []):
                checks.append(
                    {
                        **check,
                        "event_id": event["event_id"],
                        "attempt_id": event["attempt_id"],
                    }
                )
        if summary["dispatch_state"] == "not_sent":
            checks.append(
                {"stage": "business", "check": "rbac", "decision": "not_evaluated"}
            )
        return {
            **summary,
            "attempts": [
                {
                    **projection(group),
                    "attempt_id": attempt_id,
                    "event_ids": [e["event_id"] for e in group],
                }
                for attempt_id, group in attempts.items()
            ],
            "permission_checks": checks,
            "events": events,
            "children": children,
        }

    def page(self, query: OperationQuery | GrantQuery) -> dict:
        operations = isinstance(query, OperationQuery)
        table = "agent_audit_operations" if operations else "agent_audit_grant_changes"
        criteria, params = [], []
        for field in (
            "actor_user_id",
            "subject_user_id",
            "operation_id",
            "agent_id",
            "target_id",
            "run_id",
            "business_outcome",
            "grant_version",
        ):
            value = getattr(query, field, None)
            if value is not None:
                criteria.append(field + "=?")
                params.append(value)
        if operations and not query.include_children:
            criteria.append("parent_call_id IS NULL")
        for value, op in ((query.started_from, ">="), (query.started_to, "<")):
            if value:
                criteria.append("started_at" + op + "?")
                params.append(utc(value))
        if query.cursor:
            try:
                stamp, call = json.loads(base64.urlsafe_b64decode(query.cursor))
                stamp = utc(datetime.fromisoformat(stamp))
                if not isinstance(call, str) or len(call) > 128:
                    raise ValueError
            except (TypeError, ValueError, UnicodeError):
                raise ApiError(422, "INVALID_CURSOR", "无效的分页游标") from None
            criteria.append("(started_at,call_id)<(?,?)")
            params.extend([stamp, call])
        where = " WHERE " + " AND ".join(criteria) if criteria else ""
        with self.database.transaction() as conn:
            rows = conn.execute(
                f"SELECT document,started_at,call_id FROM {table}{where} ORDER BY started_at DESC,call_id DESC LIMIT ?",
                [*params, query.limit + 1],
            ).fetchall()
        items = rows[: query.limit]
        cursor = None
        if len(rows) > query.limit:
            cursor = base64.urlsafe_b64encode(
                encode([items[-1][1], items[-1][2]]).encode()
            ).decode()
        return {"items": [json.loads(row[0]) for row in items], "next_cursor": cursor}

    def cleanup(self, now: datetime, days: int):
        cutoff = utc(now - timedelta(days=days))
        with self.database.transaction(write=True) as conn:
            removed = 0
            for table, time_column in (
                ("agent_audit_operations", "finished_at"),
                ("agent_audit_grant_changes", "started_at"),
            ):
                # Wait for missing source sequence numbers before retiring a call.
                calls = conn.execute(
                    f"""SELECT call_id FROM {table} WHERE {time_column}<?
                    AND call_id IN (SELECT call_id FROM agent_audit_events GROUP BY call_id HAVING count(*)=max(sequence))
                    ORDER BY {time_column} LIMIT 500""",
                    (cutoff,),
                ).fetchall()
                for row in calls:
                    record = row[0]
                    events = conn.execute(
                        "SELECT producer_id,event_id,payload FROM agent_audit_events WHERE call_id=?",
                        (record,),
                    ).fetchall()
                    conn.executemany(
                        """INSERT INTO agent_audit_retired_events(producer_id,event_id,call_id,payload_hash)
                        VALUES(?,?,?,?)""",
                        [
                            (
                                event[0],
                                event[1],
                                record,
                                hashlib.sha256(event[2].encode()).hexdigest(),
                            )
                            for event in events
                        ],
                    )
                    removed += len(events)
                    conn.execute(
                        "DELETE FROM agent_audit_events WHERE call_id=?", (record,)
                    )
                    conn.execute(f"DELETE FROM {table} WHERE call_id=?", (record,))
            if removed:
                conn.execute(
                    "INSERT INTO agent_audit_maintenance(id,occurred_at,deleted_events) VALUES(?,?,?)",
                    (uuid4().hex, utc(now), removed),
                )
            return removed
