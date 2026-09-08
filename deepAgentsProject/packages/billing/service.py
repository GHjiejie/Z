from __future__ import annotations

from datetime import datetime

from packages.application.services import ConflictError, NotFoundError, new_id
from packages.auth.permissions import Permission, authorize
from packages.auth.resource_access import refresh_context
from packages.auth.transactions import authorized_write
from packages.auth.service import AuthAuthorizationError, AuthValidationError
from packages.billing.meter import Meter
from packages.billing.models import PricePolicy, QuotaPolicy, Reconciliation, model_key
from packages.persistence.pagination import authorized_page


class BillingService:
    def __init__(self, db):
        self.db = db
        self.meter = Meter(db)

    def _context(self, context):
        context = refresh_context(self.db, context)
        authorize(context, Permission.BILLING_MANAGE)
        return context

    @staticmethod
    def _tenant_admin(context):
        return context.is_super_admin or "tenant_admin" in context.roles

    def _audit(self, context, action, resource_id, before, after, reason):
        self.db.execute("""INSERT INTO governance_audit_events
            (id,tenant_id,project_id,actor_user_id,action,resource_id,details_json,created_at)
            VALUES(?,?,?,?,?,?,?,?)""", (new_id("audit"),context.tenant_id,context.project_id,context.user_id,
            action,resource_id,self.db.encode({"before":before,"after":after,"reason":reason}),self.db.current_time().isoformat()))

    def _quota_target(self, payload, context):
        if payload.scope_type in {"tenant", "model"}:
            if not self._tenant_admin(context):
                raise AuthAuthorizationError("Tenant-wide quotas require a tenant administrator")
            if payload.scope_type == "tenant" and payload.subject_id != context.tenant_id:
                raise NotFoundError("Quota subject not found")
            if payload.scope_type == "model" and not self.db.fetch_one(
                "SELECT model_key FROM billing_price_policies WHERE tenant_id=? AND model_key=?",
                (context.tenant_id,payload.subject_id)):
                raise NotFoundError("Configure model pricing before its quota")
        elif payload.scope_type == "project" and payload.subject_id != context.project_id:
            raise NotFoundError("Quota project not found")
        elif payload.scope_type == "user" and not self.db.fetch_one(
            "SELECT id FROM users WHERE id=? AND tenant_id=? AND project_id=?",
            (payload.subject_id,context.tenant_id,context.project_id)):
            raise NotFoundError("Quota user not found")

    def update_quota(self, payload: QuotaPolicy, context):
        targets = (payload.subject_id,) if payload.scope_type == "user" else ()
        with authorized_write(self.db, context, Permission.BILLING_MANAGE, user_ids=targets) as context:
            self.meter.lock_tenant(context.tenant_id)
            self._quota_target(payload, context)
            limits = {key:value for key,value in payload.model_dump().items() if key.startswith("max_") and value is not None}
            if payload.enabled and not limits:
                raise AuthValidationError("An enabled quota requires at least one limit")
            if (self.meter.production() and payload.scope_type == "tenant" and payload.period == "month"
                    and (not payload.enabled or not {"max_cost_micro_usd","max_concurrent_calls"}.issubset(limits))):
                raise AuthValidationError("Production tenant monthly cost and concurrency limits cannot be removed")
            key = (context.tenant_id,payload.scope_type,payload.subject_id,payload.period)
            before = self.db.fetch_one("""SELECT * FROM billing_quota_policies
                WHERE tenant_id=? AND scope_type=? AND subject_id=? AND period=?""", key)
            if payload.version != (before["version"] if before else 0):
                raise ConflictError("Quota changed; reload before updating")
            self.db.execute("""INSERT INTO billing_quota_policies
                (tenant_id,scope_type,subject_id,period,version,enabled,limits_json,updated_by,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant_id,scope_type,subject_id,period) DO UPDATE SET
                version=excluded.version,enabled=excluded.enabled,limits_json=excluded.limits_json,
                updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                (*key,payload.version+1,int(payload.enabled),self.db.encode(limits),context.user_id,self.db.current_time().isoformat()))
            after = self.db.fetch_one("""SELECT * FROM billing_quota_policies
                WHERE tenant_id=? AND scope_type=? AND subject_id=? AND period=?""", key)
            self._audit(context,"billing.quota.updated","/".join(key[1:]),before,after,payload.reason)
        return after

    def quotas(self, context):
        context = self._context(context)
        rows = self.db.fetch_all("SELECT * FROM billing_quota_policies WHERE tenant_id=? ORDER BY scope_type,subject_id,period", (context.tenant_id,))
        if not self._tenant_admin(context):
            users = {row["id"] for row in self.db.fetch_all("SELECT id FROM users WHERE tenant_id=? AND project_id=?", (context.tenant_id,context.project_id))}
            rows = [row for row in rows if (row["scope_type"] == "project" and row["subject_id"] == context.project_id)
                    or (row["scope_type"] == "user" and row["subject_id"] in users)]
        now = self.db.current_time()
        return [{**row,"usage":self.meter.usage(row, now.strftime("%Y-%m-%d" if row["period"] == "day" else "%Y-%m"))} for row in rows]

    def update_price(self, payload: PricePolicy, context):
        identity = payload.identity.model_dump()
        key = (context.tenant_id,context.project_id,model_key(identity))
        pricing = {"input_per_million":str(payload.input_per_million),"output_per_million":str(payload.output_per_million)}
        with authorized_write(self.db, context, Permission.BILLING_MANAGE) as context:
            self.meter.lock_tenant(context.tenant_id)
            before = self.db.fetch_one("SELECT * FROM billing_price_policies WHERE tenant_id=? AND project_id=? AND model_key=?", key)
            if payload.version != (before["version"] if before else 0):
                raise ConflictError("Price changed; reload before updating")
            self.db.execute("""INSERT INTO billing_price_policies
                (tenant_id,project_id,model_key,version,enabled,model_identity_json,pricing_json,updated_by,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant_id,project_id,model_key) DO UPDATE SET
                version=excluded.version,enabled=excluded.enabled,pricing_json=excluded.pricing_json,
                updated_by=excluded.updated_by,updated_at=excluded.updated_at""", (*key,payload.version+1,int(payload.enabled),
                self.db.encode(identity),self.db.encode(pricing),context.user_id,self.db.current_time().isoformat()))
            after = self.db.fetch_one("SELECT * FROM billing_price_policies WHERE tenant_id=? AND project_id=? AND model_key=?", key)
            self._audit(context,"billing.price.updated",key[-1],before,after,payload.reason)
        return after

    def prices(self, context):
        context = self._context(context)
        return self.db.fetch_all("SELECT * FROM billing_price_policies WHERE tenant_id=? AND project_id=? ORDER BY model_key", (context.tenant_id,context.project_id))

    @staticmethod
    def public_call(row):
        return {key:value for key,value in row.items() if key not in {"owner_token_hash","owner_id","request_fingerprint"}}

    def calls(self, context, *, limit=50, cursor=None, status=None):
        context = self._context(context)
        where = "r.tenant_id=? AND r.project_id=?"
        params = [context.tenant_id,context.project_id]
        if status:
            where += " AND r.billing_status=?"
            params.append(status)
        page = authorized_page(self.db,context=context,resource="billing:" + str(status),limit=limit,cursor=cursor,
            query="SELECT r.* FROM (SELECT *, admitted_at AS created_at FROM metered_calls) r WHERE " + where,
            params=params,alias="r",visible=lambda row:True)
        page["items"] = [self.public_call(row) for row in page["items"]]
        return page

    def reconcile(self, call_id, payload: Reconciliation, context):
        with authorized_write(self.db, context, Permission.BILLING_MANAGE) as context:
            self.meter.lock_tenant(context.tenant_id)
            row = self.db.fetch_one("SELECT * FROM metered_calls WHERE id=? AND tenant_id=? AND project_id=?",
                (call_id,context.tenant_id,context.project_id))
            if not row:
                raise NotFoundError("Metered call not found")
            if row["version"] != payload.version or row["billing_status"] == "ACTUAL":
                raise ConflictError("Call already settled or changed; reload before reconciling")
            if row["active_until"] and datetime.fromisoformat(row["active_until"]) > self.db.current_time():
                raise ConflictError("An active provider reservation cannot be manually released")
            self.db.execute("""UPDATE metered_calls SET input_tokens=?,output_tokens=?,charged_input_tokens=?,
                charged_output_tokens=?,charged_micro_usd=?,billing_status='ACTUAL',active_until=NULL,
                settled_at=?,provider_receipt=?,version=version+1 WHERE id=?""",
                (payload.input_tokens,payload.output_tokens,payload.input_tokens,payload.output_tokens,
                 payload.actual_cost_micro_usd,self.db.current_time().isoformat(),payload.provider_receipt,call_id))
            self.meter.project_run_usage(call_id)
            after = self.db.fetch_one("SELECT * FROM metered_calls WHERE id=?", (call_id,))
            self._audit(context,"billing.call.reconciled",call_id,self.public_call(row),self.public_call(after),payload.reason)
        return self.public_call(after)
