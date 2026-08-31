from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_CEILING
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def model_key(identity: dict) -> str:
    return hashlib.sha256(json.dumps(
        [str(identity.get(key) or "") for key in ("provider", "route", "model")],
        ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()


def micro_cost(input_tokens: int, output_tokens: int, pricing: dict) -> int:
    rates = [Decimal(str(pricing[key])) for key in ("input_per_million", "output_per_million")]
    if any(not value.is_finite() or value < 0 or value > 1_000_000 for value in rates):
        raise ValueError("Invalid model pricing")
    return int((input_tokens * rates[0] + output_tokens * rates[1]).to_integral_value(rounding=ROUND_CEILING))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Change(StrictModel):
    version: int = Field(ge=0)
    reason: str = Field(min_length=5, max_length=500)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value):
        if len(value.strip()) < 5:
            raise ValueError("A meaningful change reason is required")
        return value.strip()


class QuotaPolicy(Change):
    scope_type: Literal["tenant", "project", "user", "model"]
    subject_id: str = Field(min_length=1, max_length=256)
    period: Literal["day", "month"] = "month"
    enabled: bool = True
    max_cost_micro_usd: int | None = Field(default=None, ge=0, le=10**15)
    max_calls: int | None = Field(default=None, ge=0, le=10**9)
    max_input_tokens: int | None = Field(default=None, ge=0, le=10**12)
    max_output_tokens: int | None = Field(default=None, ge=0, le=10**12)
    max_concurrent_calls: int | None = Field(default=None, ge=0, le=10000)


class ModelIdentity(StrictModel):
    provider: str = Field(min_length=1, max_length=100)
    route: str = Field(default="", max_length=255)
    model: str = Field(min_length=1, max_length=255)


class PricePolicy(Change):
    identity: ModelIdentity
    input_per_million: Decimal = Field(ge=0, le=1_000_000, allow_inf_nan=False)
    output_per_million: Decimal = Field(ge=0, le=1_000_000, allow_inf_nan=False)
    enabled: bool = True


class Reconciliation(Change):
    input_tokens: int = Field(ge=0, le=10**9)
    output_tokens: int = Field(ge=0, le=10**9)
    actual_cost_micro_usd: int = Field(ge=0, le=10**15)
    provider_receipt: str = Field(min_length=5, max_length=2000)
