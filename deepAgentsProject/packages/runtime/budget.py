from __future__ import annotations

import json
import logging
import os
import secrets
from decimal import Decimal, ROUND_CEILING
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from packages.domain.models import utc_now
from packages.domain.models import TenantContext
from packages.billing.errors import BudgetExceeded, QuotaExceeded
from packages.billing.meter import CallTicket, Meter
from packages.billing.models import model_key
from packages.auth.resource_access import ResourceAccess
from packages.persistence import Database
from packages.persistence.fencing import LeaseLostError, RunWriteFence, current_write_fence


logger = logging.getLogger(__name__)


RunBudgetExceeded = BudgetExceeded


def _decimal(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Budget and pricing values must be finite and non-negative")
    return result


def _micro_cost(input_tokens: int, output_tokens: int, pricing: dict) -> int:
    # USD/million tokens is numerically micro-USD/token. Round reservations up.
    return int((
        input_tokens * _decimal(pricing["input_per_million"])
        + output_tokens * _decimal(pricing["output_per_million"])
    ).to_integral_value(rounding=ROUND_CEILING))


def input_token_reservation(messages: Any, extra: Any = None) -> int:
    """Conservative UTF-8 byte-based reservation, including schemas and framing.

    Provider-reported usage remains authoritative at settlement. This is not a
    tokenizer or a promise that a provider cannot bill above its declared limits.
    """
    def encode(value):
        if hasattr(value, "model_dump"):
            return value.model_dump(exclude_none=True)
        return str(value)

    content = json.dumps([messages, extra], default=encode, ensure_ascii=False)
    return len(content.encode("utf-8")) + 1024


class RunBudget:
    """Durable admission and accounting shared by all Attempts of one Run.

    A reservation is charged until authoritative usage settles it. Failure,
    cancellation and lease loss never release uncertain spend. Stale callbacks
    cannot change accounting; their original reservation remains effective.
    """

    def __init__(self, db: Database, run_id: str, plan: dict, model_identity: dict):
        self.db, self.run_id, self.plan = db, run_id, plan
        self.limits = plan.get("limits") or {}
        self.model_identity = model_identity

    def _owner(self) -> RunWriteFence:
        fence = current_write_fence()
        if not isinstance(fence, RunWriteFence) or fence.run_id != self.run_id:
            raise LeaseLostError("Model admission requires the current Run lease")
        return fence

    def _pricing(self) -> dict:
        snapshot = self.plan.get("model_snapshot") or {}
        pricing = snapshot.get("pricing") or {}
        binding = snapshot.get("runtime_binding")
        if binding:
            if model_key(binding.get("identity") or {}) != model_key(self.model_identity):
                raise RunBudgetExceeded("Runtime provider, endpoint, and model must match the priced execution plan")
        elif os.getenv("DEEPAGENT_ENVIRONMENT", "development").lower() in {"production", "prod"}:
            raise RunBudgetExceeded("Production model pricing requires an immutable runtime binding")
        if not {"input_per_million", "output_per_million"}.issubset(pricing):
            raise RunBudgetExceeded("Model pricing is missing from the execution plan")
        for key in ("input_per_million", "output_per_million"):
            _decimal(pricing[key])
        return {key: str(pricing[key]) for key in ("input_per_million", "output_per_million")}

    def _totals(self) -> tuple[int, int]:
        row = self.db.fetch_one("""SELECT COALESCE(SUM(call_count),0) AS calls,
            COALESCE(SUM(charged_micro_usd),0) AS cost FROM metered_calls WHERE run_id=?""", (self.run_id,))
        return int(row["calls"]), int(row["cost"])

    def _cost_limit(self) -> int | None:
        value = self.limits.get("max_cost")
        return None if value is None else int(_decimal(value) * 1_000_000)

    def reserve(self, *, input_tokens: int, output_tokens: int, call_id: str | None = None) -> str:
        owner = self._owner()
        ResourceAccess(self.db).require_execution(self.run_id)
        if input_tokens < 0 or output_tokens < 1:
            raise RunBudgetExceeded("Invalid model token reservation")
        pricing = self._pricing()
        amount = _micro_cost(input_tokens, output_tokens, pricing)
        call_id = call_id or f"mcall_{secrets.token_hex(16)}"
        with self.db.transaction():
            calls, charged = self._totals()
            if calls >= int(self.limits.get("max_model_calls", 20)):
                raise RunBudgetExceeded("Model call budget exhausted")
            limit = self._cost_limit()
            if limit is not None and charged + amount > limit:
                raise RunBudgetExceeded("Insufficient model budget for the requested call")
            if self.db.fetch_one("SELECT id FROM usage_ledger WHERE id=?", (call_id,)):
                raise RunBudgetExceeded("A model invocation identifier cannot be reused")
            run = self.db.fetch_one("SELECT tenant_id, project_id, principal_user_id FROM runs WHERE id=?", (self.run_id,))
            Meter(self.db).reserve(
                TenantContext(tenant_id=run["tenant_id"], project_id=run["project_id"], user_id=run["principal_user_id"]),
                self.model_identity, {**pricing, "source": "plan:" + str(self.plan.get("plan_hash", ""))},
                purpose="run_model", resource_id=self.run_id, run_id=self.run_id,
                input_tokens=input_tokens, output_tokens=output_tokens, call_id=call_id,
                duration_seconds=int(self.limits.get("max_duration_seconds", 600)) + 60,
            )
        return call_id

    def settle(self, call_id: str, *, input_tokens: int, output_tokens: int, estimated: bool = False) -> None:
        owner = self._owner()
        violations = Meter(self.db).settle(CallTicket(call_id, owner.lease_token),
            input_tokens=input_tokens, output_tokens=output_tokens, estimated=estimated)
        _, charged = self._totals()
        limit = self._cost_limit()
        # Do not roll back known spend when the provider exceeds the reservation.
        if limit is not None and charged > limit:
            raise RunBudgetExceeded("Provider usage exceeded the remaining Run budget")
        if violations:
            raise QuotaExceeded("; ".join(violations))

    def uncertain(self, call_id: str) -> None:
        try:
            owner = self._owner()
            Meter(self.db).uncertain(CallTicket(call_id, owner.lease_token))
        except LeaseLostError:
            # Its durable RESERVED charge already blocks accidental re-spending.
            pass
        except Exception:
            logger.exception("Could not annotate uncertain model spend; preserving its reservation")


class RunBudgetCallback(BaseCallbackHandler):
    """Inherited by nested models, including subagents and summarization calls."""

    raise_error = True

    def __init__(self, budget: RunBudget, max_output_tokens: int):
        self.budget = budget
        self.max_output_tokens = max_output_tokens

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        params = kwargs.get("invocation_params") or {}
        bound = next((params[key] for key in ("max_completion_tokens", "max_tokens", "max_tokens_to_sample")
                      if isinstance(params.get(key), int) and params[key] > 0), self.max_output_tokens)
        self.budget.reserve(call_id=f"mcall_{run_id}",
            input_tokens=input_token_reservation(messages, params.get("tools")), output_tokens=bound)

    def on_llm_end(self, response, *, run_id, **kwargs):
        usages = [getattr(generation.message, "usage_metadata", None)
                  for batch in response.generations for generation in batch if hasattr(generation, "message")]
        if not usages or any(not usage or not {"input_tokens", "output_tokens"}.issubset(usage) for usage in usages):
            self.budget.uncertain(f"mcall_{run_id}")
            return
        self.budget.settle(f"mcall_{run_id}",
            input_tokens=sum(int(usage.get("input_tokens", 0)) for usage in usages),
            output_tokens=sum(int(usage.get("output_tokens", 0)) for usage in usages))

    def on_llm_error(self, error, *, run_id, **kwargs):
        self.budget.uncertain(f"mcall_{run_id}")
