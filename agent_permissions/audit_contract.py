"""Version one of the allowlisted audit wire format. No free-form payload fields."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]
Stamp = Annotated[str, Field(max_length=40)]
Entry = Literal[
    "ag_ui", "chat_completions", "chat_runs", "direct_tool_api", "grant_admin"
]
Kind = Literal[
    "requested",
    "attempt.started",
    "permission.checked",
    "waiting",
    "confirmed",
    "cancelled",
    "expired",
    "attempt.rejected",
    "dispatch.attempted",
    "business.received",
    "read.succeeded",
    "write.committed",
    "business.failed",
    "result.unknown",
    "business.returned",
    "control.returned",
    "receipt.reconciled",
    "grant.changed",
]
Outcome = Literal["not_executed", "succeeded", "failed", "unknown"]


def utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timezone required")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


TERMINAL_STATES = ("completed", "failed", "rejected", "cancelled", "expired")


def next_state(previous, kind):
    if previous == "completed" or kind in {
        "grant.changed",
        "read.succeeded",
        "write.committed",
        "receipt.reconciled",
    }:
        return "completed"
    if kind in {"waiting", "confirmed", "cancelled", "expired"}:
        return kind
    if kind in {"dispatch.attempted", "business.received"}:
        return "executing"
    if kind == "result.unknown" and previous != "failed":
        return "needs_reconciliation"
    if kind == "business.failed":
        return "failed"
    if kind == "attempt.rejected" and previous in {"requested", "confirmed"}:
        return "rejected"
    return previous


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Check(Strict):
    stage: Literal["runtime", "confirmation", "tool_entry", "business"]
    check: Literal["authentication", "user_grant", "rbac", "grant_scope"]
    decision: Literal["allowed", "denied", "not_evaluated", "not_applicable"]
    roles: list[Identifier] = Field(default_factory=list, max_length=256)
    required_all: list[Identifier] = Field(default_factory=list, max_length=256)
    required_any: list[Identifier] = Field(default_factory=list, max_length=256)
    matched: list[Identifier] = Field(default_factory=list, max_length=256)
    missing: list[Identifier] = Field(default_factory=list, max_length=256)
    grant_enabled: bool | None = None
    grant_changed_by_user_id: Identifier | None = None
    grant_changed_at: Stamp | None = None
    grant_version: int | None = Field(default=None, ge=1)


class Change(Strict):
    fields: list[
        Literal[
            "username",
            "display_name",
            "email",
            "roles",
            "status",
            "password",
            "deleted_at",
            "description",
            "permissions",
            "role_code",
            "sessions",
            "version",
        ]
    ] = Field(default_factory=list)
    no_change: bool | None = None
    password_changed: bool | None = None
    before: bool | None = None
    after: bool | None = None


class Result(Strict):
    page: int | None = Field(default=None, ge=1)
    page_size: int | None = Field(default=None, ge=1)
    returned_count: int | None = Field(default=None, ge=0)


class Event(Strict):
    event_id: Identifier
    call_id: Identifier
    attempt_id: Identifier
    event_type: Kind
    sequence: int = Field(ge=1)
    occurred_at: Stamp
    entrypoint: Entry
    purpose: Literal["operation", "form_lookup", "grant_change"]
    actor_user_id: Identifier | None = None
    subject_user_id: Identifier | None = None
    agent_id: Literal["agent_permissions.chat"] | None = None
    parent_call_id: Identifier | None = None
    run_id: Identifier | None = None
    interaction_id: Identifier | None = None
    execution_id: Identifier | None = None
    control_request_id: Identifier | None = None
    business_request_id: Identifier | None = None
    business_audit_event_id: Identifier | None = None
    operation_id: Identifier | None = None
    tool_name: Identifier | None = None
    http_method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] | None = None
    route_template: (
        Annotated[str, Field(pattern=r"^/api/v1/[a-zA-Z0-9_/{\}-]{1,160}$")] | None
    ) = None
    effect: Literal["read", "write", "destructive"] | None = None
    target_type: Literal["user", "role", "permission", "session"] | None = None
    requested_target_id: Annotated[str, Field(pattern="^usr_[0-9a-f]{32}$")] | None = (
        None
    )
    target_id: Identifier | None = None
    catalog_revision: Annotated[str, Field(pattern="^[0-9a-f]{64}$")] | None = None
    permission_checks: list[Check] = Field(default_factory=list, max_length=12)
    reason_code: Identifier | None = None
    control_http_status: int | None = Field(default=None, ge=100, le=599)
    business_http_status: int | None = Field(default=None, ge=100, le=599)
    duration_ms: int | None = Field(default=None, ge=0)
    interaction_action: (
        Literal["resume", "submit", "cancel", "refresh", "options", "recover"] | None
    ) = None
    interaction_version: int | None = Field(default=None, ge=1)
    confirmed_by_user_id: Identifier | None = None
    grant_version: int | None = Field(default=None, ge=1)
    change_summary: Change | None = None
    result_summary: Result | None = None

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value: str) -> str:
        return utc(datetime.fromisoformat(value))

    @model_validator(mode="after")
    def validate_grant_change(self):
        if self.event_type == "grant.changed" and (
            self.purpose != "grant_change"
            or not self.actor_user_id
            or not self.subject_user_id
            or not self.operation_id
            or not self.grant_version
            or not self.change_summary
            or self.change_summary.before is None
            or self.change_summary.before == self.change_summary.after
            or self.change_summary.after is None
        ):
            raise ValueError("incomplete grant change")
        return self


class Batch(Strict):
    schema_version: Literal[1]
    producer_id: Identifier
    events: list[Event] = Field(min_length=1, max_length=100)
