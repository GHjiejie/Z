from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import timedelta, timezone
from decimal import Decimal

from packages.billing.errors import BillingConfigurationError, QuotaExceeded
from packages.billing.models import micro_cost, model_key
from packages.domain.models import TenantContext
from packages.persistence.fencing import IngestionWriteFence, LeaseLostError, RunWriteFence, current_write_fence


@dataclass(frozen=True)
class CallTicket:
    call_id: str
    token: str


class Meter:
    """Tenant-serialized admission. No lock spans a provider request.

    Accounting is canonical here; usage_ledger remains a Run-facing projection.
    Failed/unknown calls retain both spend and their bounded concurrency lease.
    All quota periods use the UTC admission date, including late settlements.
    """

    def __init__(self, db):
        self.db = db

    @staticmethod
    def production():
        return os.getenv("DEEPAGENT_ENVIRONMENT", "development").lower() in {"production", "prod"}

    def lock_tenant(self, tenant_id):
        self.db.execute("INSERT INTO billing_tenants(tenant_id) VALUES (?) ON CONFLICT DO NOTHING", (tenant_id,))
        suffix = " FOR UPDATE" if self.db.dialect == "postgresql" else ""
        self.db.fetch_one("SELECT tenant_id FROM billing_tenants WHERE tenant_id=?" + suffix, (tenant_id,))

    @staticmethod
    def identity(identity):
        value = {key: str(identity.get(key) or "") for key in ("provider", "route", "model")}
        if not value["provider"] or not value["model"]:
            raise BillingConfigurationError("A priced provider/model identity is required")
        return value

    def pricing(self, context, identity):
        identity = self.identity(identity)
        if identity["provider"] == "test_double" and not self.production():
            return {"input_per_million": "0", "output_per_million": "0", "source": "explicit_test_double"}
        policy = self.db.fetch_one("""SELECT * FROM billing_price_policies
            WHERE tenant_id=? AND project_id=? AND model_key=? AND enabled=1""",
            (context.tenant_id, context.project_id, model_key(identity)))
        if not policy:
            raise BillingConfigurationError("Configure pricing for this provider/model before making paid requests")
        return {**policy["pricing"], "source": f"price_policy:{model_key(identity)}:{policy['version']}"}

    @staticmethod
    def _owner():
        fence = current_write_fence()
        if isinstance(fence, RunWriteFence):
            return "run", fence.attempt_id, fence.lease_token
        if isinstance(fence, IngestionWriteFence):
            return "ingestion", fence.job_id, fence.lease_token
        token = secrets.token_hex(32)
        return "request", token[:24], token

    def reserve(self, context, identity, pricing, *, purpose, resource_id, input_tokens, output_tokens,
                run_id=None, duration_seconds=240, call_id=None, fingerprint=""):
        if (not isinstance(input_tokens, int) or not isinstance(output_tokens, int)
                or isinstance(input_tokens, bool) or isinstance(output_tokens, bool)
                or not 0 <= input_tokens <= 10_000_000 or not 0 <= output_tokens <= 10_000_000):
            raise QuotaExceeded("Invalid token reservation")
        identity = self.identity(identity)
        amount = micro_cost(input_tokens, output_tokens, pricing)
        owner_kind, owner_id, token = self._owner()
        call_id = call_id or "bill_" + secrets.token_hex(16)
        with self.db.transaction():
            fence = current_write_fence()
            if owner_kind == "run":
                run_id = run_id or fence.run_id
                run = self.db.fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
                if (run_id != fence.run_id or not run or run["tenant_id"] != context.tenant_id
                        or run["project_id"] != context.project_id or run["principal_user_id"] != context.user_id):
                    raise LeaseLostError("Billing principal does not match the leased Run")
                from packages.auth.resource_access import ResourceAccess
                ResourceAccess(self.db).require_execution(run_id)
            elif run_id is not None:
                raise LeaseLostError("Run billing requires its active execution lease")
            if owner_kind == "ingestion":
                job = self.db.fetch_one("SELECT * FROM knowledge_ingestion_jobs WHERE id=?", (owner_id,))
                if (not job or job["tenant_id"] != context.tenant_id or job["project_id"] != context.project_id
                        or job["requested_by"] != context.user_id):
                    raise LeaseLostError("Billing principal does not match the ingestion owner")
            self.lock_tenant(context.tenant_id)
            if str(pricing.get("source","")).startswith("price_policy:"):
                if self.pricing(context,identity) != pricing:
                    raise BillingConfigurationError("Provider price changed before admission; retry with current pricing")
            if owner_kind == "run":
                plan = self.db.fetch_one("SELECT plan_json FROM resolved_execution_plans WHERE id=?", (run["resolved_plan_id"],))["plan"]
                totals = self.db.fetch_one("""SELECT COALESCE(SUM(call_count),0) AS calls,
                    COALESCE(SUM(charged_micro_usd),0) AS cost FROM metered_calls WHERE run_id=?""", (run_id,))
                limits = plan.get("limits") or {}
                if totals["calls"] >= int(limits.get("max_model_calls", 20)):
                    raise QuotaExceeded("Run model call budget exhausted")
                if limits.get("max_cost") is not None and Decimal(totals["cost"] + amount) > Decimal(str(limits["max_cost"])) * 1_000_000:
                    raise QuotaExceeded("Insufficient Run budget for this provider call")
            now = self.db.current_time().astimezone(timezone.utc)
            candidate = {
                "id": call_id, "tenant_id": context.tenant_id, "project_id": context.project_id,
                "user_id": context.user_id, "run_id": run_id, "purpose": purpose, "resource_id": resource_id,
                "model_key": model_key(identity), "model_identity_json": self.db.encode(identity),
                "pricing_json": self.db.encode(pricing), "billing_status": "RESERVED", "owner_kind": owner_kind,
                "owner_id": owner_id, "owner_token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "reserved_input_tokens": input_tokens, "reserved_output_tokens": output_tokens,
                "reserved_micro_usd": amount, "charged_input_tokens": input_tokens,
                "charged_output_tokens": output_tokens, "charged_micro_usd": amount,
                "request_fingerprint": fingerprint, "admitted_at": now.isoformat(),
                "day_key": now.strftime("%Y-%m-%d"), "month_key": now.strftime("%Y-%m"),
                "active_until": (now + timedelta(seconds=max(1, min(duration_seconds, 86460)))).isoformat(),
            }
            if self.db.fetch_one("SELECT id FROM metered_calls WHERE id=?", (call_id,)):
                raise QuotaExceeded("A provider invocation identifier cannot be reused")
            violations = self.violations(candidate, addition=candidate)
            if violations:
                raise QuotaExceeded("; ".join(violations))
            columns = list(candidate)
            self.db.execute(f"INSERT INTO metered_calls ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                            tuple(candidate[key] for key in columns))
            if owner_kind == "run":
                self.db.execute("""INSERT INTO usage_ledger
                    (id,tenant_id,project_id,run_id,input_tokens,output_tokens,model_calls,tool_calls,subagent_calls,
                     cost,created_at,metering_version,attempt_id,billing_status,reserved_micro_usd,charged_micro_usd,
                     pricing_json,model_identity_json,purpose)
                    VALUES(?,?,?,?,0,0,1,0,0,?,?,1,?,'RESERVED',?,?,?,?,?)""",
                    (call_id,context.tenant_id,context.project_id,run_id,amount / 1_000_000,now.isoformat(),
                     fence.attempt_id,amount,amount,self.db.encode(pricing),self.db.encode(identity),purpose))
        return CallTicket(call_id, token)

    def applicable_policies(self, call, *, require_config=True):
        policies = self.db.fetch_all("SELECT * FROM billing_quota_policies WHERE tenant_id=? AND enabled=1", (call["tenant_id"],))
        targets = {"tenant": call["tenant_id"], "project": call["project_id"], "user": call["user_id"], "model": call["model_key"]}
        applicable = [policy for policy in policies if targets.get(policy["scope_type"]) == policy["subject_id"]]
        if require_config and self.production() and not any(
            policy["scope_type"] == "tenant" and policy["period"] == "month"
            and policy["limits"].get("max_cost_micro_usd") is not None
            and policy["limits"].get("max_concurrent_calls") is not None for policy in applicable
        ):
            raise BillingConfigurationError("Production requires a tenant monthly spending and concurrency policy")
        return applicable

    def usage(self, policy, period_key):
        column = {"tenant": "tenant_id", "project": "project_id", "user": "user_id", "model": "model_key"}[policy["scope_type"]]
        period_column = {"day": "day_key", "month": "month_key"}[policy["period"]]
        scope = f"tenant_id=? AND {column}=?"
        params = (policy["tenant_id"], policy["subject_id"])
        row = self.db.fetch_one(f"""SELECT COALESCE(SUM(charged_micro_usd),0) AS cost,
            COALESCE(SUM(call_count),0) AS calls, COALESCE(SUM(charged_input_tokens),0) AS input_tokens,
            COALESCE(SUM(charged_output_tokens),0) AS output_tokens,
            COALESCE(SUM(CASE WHEN billing_status!='ACTUAL' THEN charged_micro_usd ELSE 0 END),0) AS pending_micro_usd,
            COALESCE(SUM(CASE WHEN owner_kind='legacy' AND billing_status!='ACTUAL' THEN 1 ELSE 0 END),0) AS unknown_legacy_tokens
            FROM metered_calls WHERE {scope} AND {period_column}=?""", (*params, period_key))
        row["concurrent_calls"] = self.db.fetch_one(
            f"SELECT COUNT(*) AS count FROM metered_calls WHERE {scope} AND active_until>?",
            (*params, self.db.current_time().isoformat()),
        )["count"]
        return {key: int(value) for key, value in row.items()}

    def violations(self, call, *, addition=None):
        violations = []
        fields = {"max_cost_micro_usd": "cost", "max_calls": "calls", "max_input_tokens": "input_tokens",
                  "max_output_tokens": "output_tokens", "max_concurrent_calls": "concurrent_calls"}
        increments = {"cost": (addition or {}).get("charged_micro_usd", 0), "calls": int(addition is not None),
                      "input_tokens": (addition or {}).get("charged_input_tokens", 0),
                      "output_tokens": (addition or {}).get("charged_output_tokens", 0),
                      "concurrent_calls": int(addition is not None)}
        for policy in self.applicable_policies(call, require_config=addition is not None):
            usage = self.usage(policy, call[policy["period"] + "_key"])
            for limit_name, metric in fields.items():
                limit = policy["limits"].get(limit_name)
                if limit is not None and usage[metric] + increments[metric] > limit:
                    violations.append(f"{policy['scope_type']} {policy['period']} {limit_name} exceeded")
            if usage["unknown_legacy_tokens"] and any(policy["limits"].get(key) is not None for key in ("max_input_tokens", "max_output_tokens")):
                violations.append("Reconcile legacy token usage before spending a token-limited allowance")
        return violations

    def _owned(self, ticket):
        row = self.db.fetch_one("SELECT * FROM metered_calls WHERE id=?", (ticket.call_id,))
        if not row or row["owner_token_hash"] != hashlib.sha256(ticket.token.encode()).hexdigest():
            raise LeaseLostError("Metering reservation is not owned by this invocation")
        fence = current_write_fence()
        if row["owner_kind"] == "run" and (not isinstance(fence, RunWriteFence)
                or fence.attempt_id != row["owner_id"] or fence.lease_token != ticket.token):
            raise LeaseLostError("Metering callback does not own the Run lease")
        if row["owner_kind"] == "ingestion" and (not isinstance(fence, IngestionWriteFence)
                or fence.job_id != row["owner_id"] or fence.lease_token != ticket.token):
            raise LeaseLostError("Metering callback does not own the ingestion lease")
        if row["owner_kind"] == "legacy":
            raise LeaseLostError("Legacy usage requires audited reconciliation")
        return row

    def settle(self, ticket, *, input_tokens, output_tokens, estimated=False, provider_receipt=None):
        if (type(input_tokens) is not int or type(output_tokens) is not int
                or min(input_tokens, output_tokens) < 0 or max(input_tokens, output_tokens) > 10**9):
            raise ValueError("Invalid provider token usage")
        with self.db.transaction():
            row = self._owned(ticket)
            self.lock_tenant(row["tenant_id"])
            row = self._owned(ticket)
            if row["billing_status"] == "ACTUAL":
                return []
            amount = micro_cost(input_tokens, output_tokens, row["pricing"])
            if estimated:
                amount = max(amount, row["reserved_micro_usd"])
            status = "UNCERTAIN" if estimated else "ACTUAL"
            self.db.execute("""UPDATE metered_calls SET input_tokens=?, output_tokens=?, charged_input_tokens=?,
                charged_output_tokens=?, charged_micro_usd=?, billing_status=?, active_until=?, settled_at=?,
                provider_receipt=?, version=version+1 WHERE id=?""",
                (input_tokens, output_tokens, max(input_tokens, row["reserved_input_tokens"]) if estimated else input_tokens,
                 max(output_tokens, row["reserved_output_tokens"]) if estimated else output_tokens, amount, status,
                 row["active_until"] if estimated else None, self.db.current_time().isoformat(), provider_receipt, row["id"]))
            self.project_run_usage(row["id"])
            # Return violations, never raise inside the caller's transaction:
            # known provider spend must commit even when it exceeds a limit.
            violations = self.violations(row)
            if row["run_id"]:
                run = self.db.fetch_one("SELECT resolved_plan_id FROM runs WHERE id=?", (row["run_id"],))
                plan = self.db.fetch_one("SELECT plan_json FROM resolved_execution_plans WHERE id=?", (run["resolved_plan_id"],))["plan"]
                limit = (plan.get("limits") or {}).get("max_cost")
                total = self.db.fetch_one("SELECT COALESCE(SUM(charged_micro_usd),0) AS n FROM metered_calls WHERE run_id=?", (row["run_id"],))["n"]
                if limit is not None and Decimal(total) > Decimal(str(limit)) * 1_000_000:
                    violations.append("Provider usage exceeded the Run budget")
            return violations

    def project_run_usage(self, call_id):
        row = self.db.fetch_one("SELECT * FROM metered_calls WHERE id=?", (call_id,))
        if row["run_id"]:
            self.db.execute("""UPDATE usage_ledger SET input_tokens=?, output_tokens=?, cost=?,
                charged_micro_usd=?, billing_status=? WHERE id=? AND run_id=?""",
                (row["input_tokens"], row["output_tokens"], row["charged_micro_usd"] / 1_000_000,
                 row["charged_micro_usd"], row["billing_status"], row["id"], row["run_id"]))

    def uncertain(self, ticket):
        try:
            with self.db.transaction():
                row = self._owned(ticket)
                self.lock_tenant(row["tenant_id"])
                self.db.execute("""UPDATE metered_calls SET billing_status='UNCERTAIN', version=version+1
                    WHERE id=? AND billing_status='RESERVED'""", (ticket.call_id,))
                self.project_run_usage(ticket.call_id)
        except LeaseLostError:
            # The durable reservation, tokens and concurrency lease remain.
            pass
