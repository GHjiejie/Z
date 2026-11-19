"""Durable token usage records and date-bounded user reports."""

import json
import re
from datetime import date, datetime, time, timedelta
from typing import Literal, NamedTuple, Self
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import field_validator, model_validator

from agent_permissions.models import StrictModel
from agent_permissions.storage import Connection, DatabaseBackend

MODEL_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_model_usage (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    run_id TEXT NOT NULL,
    model TEXT,
    source TEXT NOT NULL CHECK(source IN ('model_call','legacy_run')),
    started_at REAL NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    cached_input_tokens INTEGER,
    reasoning_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS agent_model_usage_user_time
    ON agent_model_usage(user_id,started_at);
"""
MODEL_USAGE_COLUMNS = {
    "agent_model_usage": (
        "id",
        "user_id",
        "run_id",
        "model",
        "source",
        "started_at",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
    ),
}
REPORT_TIMEZONE = "Asia/Shanghai"


class TokenUsageQuery(StrictModel):
    from_date: date | None = None
    to_date: date | None = None

    @field_validator("from_date", "to_date", mode="before")
    @classmethod
    def iso_date(cls, value: object) -> object:
        if isinstance(value, str) and not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value
        ):
            raise ValueError("日期须采用 YYYY-MM-DD 格式")
        return value

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("结束日期不能早于开始日期")
        return self


class TokenUsageSummary(StrictModel):
    user_id: str
    from_date: date | None
    to_date: date | None
    timezone: Literal["Asia/Shanghai"] = REPORT_TIMEZONE
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cached_input_tokens: int | None
    reasoning_tokens: int | None
    model_calls: int
    unknown_calls: int
    legacy_runs: int


class TokenCounts(NamedTuple):
    input_tokens: int
    output_tokens: int
    total_tokens: int


def token_count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def confirmed_counts(
    input_: object, output: object, total: object
) -> TokenCounts | None:
    input_tokens, output_tokens, total_tokens = map(
        token_count, (input_, output, total)
    )
    if input_tokens is None or output_tokens is None or total_tokens is None:
        return None
    return TokenCounts(input_tokens, output_tokens, total_tokens)


def start_model_call(
    database: DatabaseBackend,
    *,
    user_id: str,
    run_id: str,
    model: str,
    started_at: float,
) -> str:
    call_id = f"model_{uuid4().hex}"
    with database.transaction(write=True) as connection:
        connection.execute(
            """INSERT INTO agent_model_usage(id,user_id,run_id,model,source,started_at)
               VALUES(?,?,?,?,'model_call',?)""",
            (call_id, user_id, run_id, model, started_at),
        )
    return call_id


def confirm_model_call(
    database: DatabaseBackend,
    call_id: str,
    counts: TokenCounts,
    cached_input_tokens: int | None,
    reasoning_tokens: int | None,
) -> None:
    with database.transaction(write=True) as connection:
        connection.execute(
            """UPDATE agent_model_usage SET input_tokens=?,output_tokens=?,total_tokens=?,
               cached_input_tokens=?,reasoning_tokens=? WHERE id=? AND total_tokens IS NULL""",
            (*counts, cached_input_tokens, reasoning_tokens, call_id),
        )


def backfill_run_usage(connection: Connection) -> None:
    """Copy retained snapshots once on upgrade, before new model calls are metered."""
    for row in connection.execute(
        """SELECT id,user_id,created_at,usage FROM agent_chat_runs
           WHERE purged=0 AND usage<>'{}'"""
    ):
        try:
            usage = json.loads(row["usage"])
        except ValueError:
            continue
        if not isinstance(usage, dict):
            continue
        counts = confirmed_counts(
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            usage.get("total_tokens"),
        )
        if counts is None:
            continue
        connection.execute(
            """INSERT INTO agent_model_usage
               (id,user_id,run_id,source,started_at,input_tokens,output_tokens,total_tokens)
               VALUES(?,?,?,'legacy_run',?,?,?,?) ON CONFLICT(id) DO NOTHING""",
            (
                f"legacy_{row['id']}",
                row["user_id"],
                row["id"],
                row["created_at"],
                *counts,
            ),
        )


def user_usage_summary(
    connection: Connection, user_id: str, query: TokenUsageQuery
) -> TokenUsageSummary:
    timezone = ZoneInfo(REPORT_TIMEZONE)
    clauses = ["user_id=?"]
    params: list[str | float] = [user_id]
    if query.from_date:
        clauses.append("started_at>=?")
        params.append(datetime.combine(query.from_date, time.min, timezone).timestamp())
    if query.to_date and query.to_date < date.max:
        clauses.append("started_at<?")
        next_day = query.to_date + timedelta(days=1)
        params.append(datetime.combine(next_day, time.min, timezone).timestamp())
    where = " AND ".join(clauses)
    row = connection.execute(
        f"""SELECT SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
                  SUM(total_tokens) AS total_tokens, SUM(cached_input_tokens) AS cached_input_tokens,
                  SUM(reasoning_tokens) AS reasoning_tokens,
                  COUNT(CASE WHEN source='model_call' THEN 1 END) AS model_calls,
                  COUNT(CASE WHEN source='model_call' AND total_tokens IS NULL THEN 1 END) AS unknown_calls,
                  COUNT(CASE WHEN source='legacy_run' THEN 1 END) AS legacy_runs
           FROM agent_model_usage WHERE {where}""",  # Only static filter clauses are interpolated.
        params,
    ).fetchone()
    return TokenUsageSummary(
        user_id=user_id, from_date=query.from_date, to_date=query.to_date, **dict(row)
    )
