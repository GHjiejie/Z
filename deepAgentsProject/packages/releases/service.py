from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from packages.application.services import ConflictError, NotFoundError, new_id
from packages.auth.permissions import Permission, authorize
from packages.auth.resource_access import refresh_context
from packages.auth.service import AuthAuthorizationError
from packages.domain.models import TenantContext
from packages.evaluations.service import EvaluationService
from packages.persistence.pagination import authorized_page


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class ReleaseService:
    """Serialize grants and release decisions per project; apply approval atomically.

    Runtime conversation access remains environment-bound. An explicit release
    grant allows control-plane promotion, not access to target-environment data.
    """

    def __init__(self, db, models=None):
        self.db = db
        self.models = models

    def lock_project(self, context):
        self.db.execute("INSERT OR IGNORE INTO release_projects(tenant_id,project_id) VALUES(?,?)",
            (context.tenant_id, context.project_id))
        suffix = " FOR UPDATE" if self.db.dialect == "postgresql" else ""
        self.db.fetch_one("SELECT * FROM release_projects WHERE tenant_id=? AND project_id=?" + suffix,
            (context.tenant_id, context.project_id))

    def _lock_accounts(self, user_ids):
        if self.db.dialect == "postgresql":
            for user_id in sorted(set(user_ids)):
                self.db.fetch_one("SELECT id FROM users WHERE id=? FOR UPDATE", (user_id,))

    def _account_context(self, context):
        if not self.db.fetch_one("SELECT id FROM users WHERE id=?", (context.user_id,)):
            raise AuthAuthorizationError("Release authority requires a provisioned account")
        return refresh_context(self.db, context)

    @staticmethod
    def _grant_admin(context):
        return context.is_super_admin or "tenant_admin" in context.roles

    def _grant(self, context, environment):
        return self.db.fetch_one("""SELECT * FROM deployment_environment_grants
            WHERE tenant_id=? AND project_id=? AND environment=? AND user_id=?""",
            (context.tenant_id, context.project_id, environment, context.user_id))

    def require_target(self, context, environment, *, approve=False):
        context = refresh_context(self.db, context)
        authorize(context, Permission.RELEASE_APPROVE if approve else Permission.DEPLOYMENT_MANAGE)
        if not approve and environment != "production" and context.environment_id == f"env_{environment}":
            return context, None
        context = self._account_context(context)
        grant = self._grant(context, environment)
        if not grant or not grant["can_approve" if approve else "can_deploy"]:
            raise AuthAuthorizationError(f"Explicit {environment} release authority is required")
        return context, grant

    def audit(self, context, action, resource_id, details):
        self.db.execute("""INSERT INTO governance_audit_events
            (id,tenant_id,project_id,actor_user_id,action,resource_id,details_json,created_at)
            VALUES(?,?,?,?,?,?,?,?)""", (new_id("audit"), context.tenant_id, context.project_id,
            context.user_id, action, resource_id, self.db.encode(details), self.db.current_time().isoformat()))

    def update_grant(self, payload, context):
        with self.db.transaction():
            self.lock_project(context)
            self._lock_accounts([context.user_id, payload.user_id])
            context = self._account_context(context)
            authorize(context, Permission.RELEASE_GRANT_MANAGE)
            if not self._grant_admin(context):
                raise AuthAuthorizationError("Only tenant or platform administrators may grant release authority")
            target = self.db.fetch_one("SELECT * FROM users WHERE id=? AND tenant_id=? AND project_id=?",
                (payload.user_id, context.tenant_id, context.project_id))
            if not target or (target["status"] != "ACTIVE" and (payload.can_deploy or payload.can_approve)):
                raise NotFoundError("Active project account not found")
            key = (context.tenant_id, context.project_id, payload.environment, payload.user_id)
            before = self.db.fetch_one("""SELECT * FROM deployment_environment_grants
                WHERE tenant_id=? AND project_id=? AND environment=? AND user_id=?""", key)
            if payload.version != (before["version"] if before else 0):
                raise ConflictError("Environment grant changed; reload before updating")
            self.db.execute("""INSERT INTO deployment_environment_grants
                (tenant_id,project_id,environment,user_id,can_deploy,can_approve,version,updated_by,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant_id,project_id,environment,user_id) DO UPDATE SET
                can_deploy=excluded.can_deploy,can_approve=excluded.can_approve,version=excluded.version,
                updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                (*key, int(payload.can_deploy), int(payload.can_approve), payload.version + 1,
                 context.user_id, self.db.current_time().isoformat()))
            after = self.db.fetch_one("""SELECT * FROM deployment_environment_grants
                WHERE tenant_id=? AND project_id=? AND environment=? AND user_id=?""", key)
            self.audit(context, "release.authority.updated", payload.user_id,
                {"before": before, "after": after, "reason": payload.reason})
            return after

    def grants(self, context):
        context = self._account_context(context)
        authorize(context, Permission.DEPLOYMENT_READ)
        query = "SELECT * FROM deployment_environment_grants WHERE tenant_id=? AND project_id=?"
        params = [context.tenant_id, context.project_id]
        if not self._grant_admin(context):
            query += " AND user_id=?"
            params.append(context.user_id)
        return self.db.fetch_all(query + " ORDER BY environment,user_id", params)

    def _can_view(self, row, context):
        context = self._account_context(context)
        authorize(context, Permission.DEPLOYMENT_READ)
        if row.get("requested_by") == context.user_id or self._grant_admin(context):
            return True
        grant = self._grant(context, row["environment"])
        return bool(grant and (grant["can_deploy"] or grant["can_approve"]))

    def channel(self, agent_id, context):
        context = self._account_context(context)
        if not self._can_view({"environment": "production"}, context):
            raise NotFoundError("Release channel not found")
        if not self.db.fetch_one("SELECT id FROM agents WHERE id=? AND tenant_id=? AND project_id=?",
            (agent_id, context.tenant_id, context.project_id)):
            raise NotFoundError("Agent not found")
        return self._channel(agent_id, context)

    def _channel(self, agent_id, context):
        return self.db.fetch_one("""SELECT * FROM release_channels
            WHERE tenant_id=? AND project_id=? AND agent_id=? AND environment='production'""",
            (context.tenant_id, context.project_id, agent_id)) or {
                "agent_id": agent_id, "environment": "production", "version": 0, "active_deployment_id": None}

    def _routing_snapshot(self, agent_id, context):
        router = self.db.fetch_one("""SELECT * FROM intent_router_revisions
            WHERE tenant_id=? AND project_id=? AND environment_id='env_production' AND status='ACTIVE'
            ORDER BY revision_number DESC LIMIT 1""", (context.tenant_id, context.project_id))
        if not router:
            return {"router_id": None, "config_hash": None, "targets": []}
        deployments = {row["id"] for row in self.db.fetch_all("""SELECT id FROM agent_deployments
            WHERE tenant_id=? AND project_id=? AND agent_id=? AND environment='production'""",
            (context.tenant_id, context.project_id, agent_id))}
        return {"router_id": router["id"], "config_hash": digest(router["config"]),
            "targets": sorted(key for key, value in router["config"].get("target_deployments", {}).items() if value in deployments)}

    @staticmethod
    def _validate_routes(routing, plan):
        coding = bool((plan.get("coding_profile") or {}).get("enabled"))
        for intent in routing["targets"]:
            supported = (coding if intent == "coding" else bool(plan.get("knowledge_bindings"))
                if intent == "knowledge" else not coding)
            if not supported:
                raise ConflictError(f"Release candidate no longer supports its configured {intent} route")

    def _apply_routes(self, routing, deployment_id, context):
        if not routing["targets"]:
            return None
        router = self.db.fetch_one("SELECT * FROM intent_router_revisions WHERE id=?", (routing["router_id"],))
        config = router["config"]
        for intent in routing["targets"]:
            config["target_deployments"][intent] = deployment_id
        revision_id = new_id("router")
        self.db.execute("UPDATE intent_router_revisions SET status='SUPERSEDED' WHERE id=?", (router["id"],))
        self.db.execute("""INSERT INTO intent_router_revisions
            (id,tenant_id,project_id,environment_id,revision_number,taxonomy_version,mode,config_json,
             model_snapshot_json,status,created_at,approval_state) VALUES(?,?,?,'env_production',?,?,?,?,?,'ACTIVE',?,?)""",
            (revision_id, context.tenant_id, context.project_id, router["revision_number"] + 1,
             router["taxonomy_version"], router["mode"], self.db.encode(config), self.db.encode(router["model_snapshot"]),
             self.db.current_time().isoformat(), router["approval_state"]))
        return revision_id

    def _raw(self, request_id, context):
        row = self.db.fetch_one("SELECT * FROM release_requests WHERE id=? AND tenant_id=? AND project_id=?",
            (request_id, context.tenant_id, context.project_id))
        if not row:
            raise NotFoundError("Release request not found")
        snapshot = json.loads(row["snapshot_json"])
        if digest(snapshot) != row["snapshot_hash"] or any(snapshot[key] != row[key] for key in (
            "id", "tenant_id", "project_id", "agent_id", "environment", "requested_by", "created_at", "expires_at")):
            raise ConflictError("Release request integrity check failed")
        row["snapshot"] = snapshot
        return row

    def _public(self, row):
        snapshot = row.get("snapshot") or json.loads(row["snapshot_json"])
        result = {key: row[key] for key in (
            "id", "agent_id", "environment", "requested_by", "status", "version", "deployment_id",
            "decided_by", "decision_reason", "decided_at", "created_at", "expires_at", "snapshot_hash")}
        result.update({key: snapshot[key] for key in (
            "agent_revision_id", "plan_hash", "evaluation_id", "action", "rollback_deployment_id",
            "expected_channel_version", "reason", "name", "routing")})
        result["expired"] = row["status"] == "PENDING" and datetime.fromisoformat(row["expires_at"]) <= self.db.current_time()
        return result

    def get(self, request_id, context):
        row = self._raw(request_id, context)
        if not self._can_view(row, context):
            raise NotFoundError("Release request not found")
        return self._public(row)

    def list(self, context, *, limit=50, cursor=None):
        context = self._account_context(context)
        authorize(context, Permission.DEPLOYMENT_READ)
        page = authorized_page(self.db, query="SELECT r.* FROM release_requests r WHERE r.tenant_id=? AND r.project_id=?",
            params=(context.tenant_id, context.project_id), alias="r", resource="release-requests", context=context,
            visible=lambda row: self._can_view(row, context), limit=limit, cursor=cursor)
        page["items"] = [self._public(self._raw(row["id"], context)) for row in page["items"]]
        return page

    def _candidate(self, revision_id, context):
        suffix = " FOR UPDATE" if self.db.dialect == "postgresql" else ""
        revision = self.db.fetch_one("SELECT * FROM agent_revisions WHERE id=? AND tenant_id=? AND project_id=?" + suffix,
            (revision_id, context.tenant_id, context.project_id))
        if not revision:
            raise NotFoundError("Agent revision not found")
        plan = self.db.fetch_one("SELECT * FROM resolved_execution_plans WHERE agent_revision_id=?", (revision_id,))
        if not plan or not (plan["plan"].get("model_snapshot") or {}).get("runtime_binding"):
            raise ConflictError("Production releases require an immutable approved model binding; republish the revision")
        if self.models is None:
            raise ConflictError("Model registry is unavailable for release verification")
        self.models.validate_plan(plan["plan"])
        result = EvaluationService(self.db).require_production_result(revision_id, context)
        policy = self.db.fetch_one("SELECT version FROM evaluation_policies WHERE tenant_id=? AND project_id=?",
            (context.tenant_id, context.project_id))
        return revision, plan, result, policy["version"]

    def create(self, payload, context, idempotency_key=None):
        with self.db.transaction():
            self.lock_project(context)
            self._lock_accounts([context.user_id])
            context, grant = self.require_target(context, "production")
            scope = f"release:{context.project_id}:{context.user_id}"
            request_hash = digest(payload.model_dump())
            if idempotency_key:
                saved = self.db.fetch_one("SELECT response_json FROM idempotency_records WHERE tenant_id=? AND scope=? AND key=?",
                    (context.tenant_id, scope, idempotency_key))
                if saved:
                    saved = json.loads(saved["response_json"])
                    if saved["request_hash"] != request_hash:
                        raise ConflictError("Release idempotency key was used for different content")
                    return self.get(saved["id"], context)
            revision, plan, evaluation, policy_version = self._candidate(payload.agent_revision_id, context)
            channel = self._channel(revision["agent_id"], context)
            if payload.expected_channel_version != channel["version"]:
                raise ConflictError("Production channel changed; review the latest deployment")
            routing = self._routing_snapshot(revision["agent_id"], context)
            self._validate_routes(routing, plan["plan"])
            if payload.action == "rollback":
                target = self.db.fetch_one("""SELECT * FROM agent_deployments
                    WHERE id=? AND tenant_id=? AND project_id=? AND agent_id=? AND environment='production'""",
                    (payload.rollback_deployment_id, context.tenant_id, context.project_id, revision["agent_id"]))
                if not target or target["agent_revision_id"] != revision["id"]:
                    raise NotFoundError("Rollback target does not match the requested revision")
                require_approved_deployment(self.db, target)
                if target["id"] == channel["active_deployment_id"]:
                    raise ConflictError("Rollback target is already active")
            now = self.db.current_time()
            snapshot = {**payload.model_dump(), "id": new_id("release"), "tenant_id": context.tenant_id,
                "project_id": context.project_id, "agent_id": revision["agent_id"], "environment": "production",
                "requested_by": context.user_id, "requester_context": context.model_dump(exclude={"session_id", "is_super_admin"}),
                "grant_version": grant["version"], "resolved_plan_id": plan["id"], "plan_hash": plan["plan_hash"],
                "evaluation_id": evaluation["id"], "evaluation_hash": evaluation["result_hash"], "policy_version": policy_version,
                "routing": routing,
                "created_at": now.isoformat(), "expires_at": (now + timedelta(seconds=payload.expires_in_seconds)).isoformat()}
            self.db.execute("""INSERT INTO release_requests
                (id,tenant_id,project_id,agent_id,environment,requested_by,snapshot_json,snapshot_hash,
                 status,version,created_at,expires_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'PENDING',1,?,?,?)""",
                (snapshot["id"], context.tenant_id, context.project_id, revision["agent_id"], "production", context.user_id,
                 self.db.encode(snapshot), digest(snapshot), snapshot["created_at"], snapshot["expires_at"], snapshot["created_at"]))
            if idempotency_key:
                self.db.execute("INSERT INTO idempotency_records(tenant_id,scope,key,response_json,created_at) VALUES(?,?,?,?,?)",
                    (context.tenant_id, scope, idempotency_key,
                     self.db.encode({"id": snapshot["id"], "request_hash": request_hash}), snapshot["created_at"]))
            self.audit(context, "release.requested", snapshot["id"], {"snapshot_hash": digest(snapshot), "reason": payload.reason})
            return self.get(snapshot["id"], context)

    def decide(self, request_id, payload, context):
        with self.db.transaction():
            self.lock_project(context)
            row = self._raw(request_id, context)
            self._lock_accounts([context.user_id, row["requested_by"]])
            context, _ = self.require_target(context, "production", approve=True)
            if context.user_id == row["requested_by"]:
                raise AuthAuthorizationError("The requester cannot approve or reject their own release; cancel it instead")
            target_status = "APPLIED" if payload.decision == "approve" else "REJECTED"
            if (row["status"] == target_status and row["version"] == payload.version + 1
                    and row["decided_by"] == context.user_id and row["decision_reason"] == payload.reason):
                return self._public(row)
            if row["status"] != "PENDING" or row["version"] != payload.version:
                raise ConflictError("Release request changed or was already decided")
            if datetime.fromisoformat(row["expires_at"]) <= self.db.current_time():
                raise ConflictError("Release request expired; create a newly reviewed request")
            deployment_id = None
            routing_revision_id = None
            snapshot = row["snapshot"]
            if payload.decision == "approve":
                requester, grant = self.require_target(TenantContext(**snapshot["requester_context"]), "production")
                if grant["version"] != snapshot["grant_version"]:
                    raise ConflictError("Requester authority changed; create a new release request")
                revision, plan, evaluation, policy_version = self._candidate(snapshot["agent_revision_id"], requester)
                if (plan["id"] != snapshot["resolved_plan_id"] or plan["plan_hash"] != snapshot["plan_hash"]
                        or evaluation["id"] != snapshot["evaluation_id"] or evaluation["result_hash"] != snapshot["evaluation_hash"]
                        or policy_version != snapshot["policy_version"]):
                    raise ConflictError("Release plan or evaluation policy/evidence changed; request a fresh review")
                channel = self._channel(row["agent_id"], context)
                if channel["version"] != snapshot["expected_channel_version"]:
                    raise ConflictError("Production channel changed while this request was pending")
                if self._routing_snapshot(row["agent_id"], context) != snapshot["routing"]:
                    raise ConflictError("Production routing changed; review a fresh release request")
                self._validate_routes(snapshot["routing"], plan["plan"])
                deployment_id = new_id("dep")
                now = self.db.current_time().isoformat()
                # Old executions may drain; old deployments cannot accept new Runs.
                self.db.execute("""UPDATE agent_deployments SET status='DRAINING',updated_at=?
                    WHERE tenant_id=? AND project_id=? AND agent_id=? AND environment='production' AND status='ACTIVE'""",
                    (now, context.tenant_id, context.project_id, row["agent_id"]))
                self.db.execute("""INSERT INTO agent_deployments
                    (id,tenant_id,project_id,agent_id,agent_revision_id,resolved_plan_id,name,environment,status,
                     created_at,updated_at,evaluation_id,release_request_id) VALUES(?,?,?,?,?,?,?,'production','ACTIVE',?,?,?,?)""",
                    (deployment_id, context.tenant_id, context.project_id, row["agent_id"], revision["id"], plan["id"],
                     snapshot["name"] or f"production-{revision['revision_number']}", now, now, evaluation["id"], request_id))
                self.db.execute("""INSERT INTO release_channels
                    (tenant_id,project_id,agent_id,environment,version,active_deployment_id,updated_at)
                    VALUES(?,?,?,'production',?,?,?) ON CONFLICT(tenant_id,project_id,agent_id,environment) DO UPDATE SET
                    version=excluded.version,active_deployment_id=excluded.active_deployment_id,updated_at=excluded.updated_at""",
                    (context.tenant_id, context.project_id, row["agent_id"], channel["version"] + 1, deployment_id, now))
                routing_revision_id = self._apply_routes(snapshot["routing"], deployment_id, context)
            now = self.db.current_time().isoformat()
            self.db.execute("""UPDATE release_requests SET status=?,version=version+1,deployment_id=?,
                decided_by=?,decision_reason=?,decided_at=?,updated_at=? WHERE id=? AND version=? AND status='PENDING'""",
                (target_status, deployment_id, context.user_id, payload.reason, now, now, request_id, payload.version))
            self.audit(context, "release." + (snapshot["action"] + ".applied" if deployment_id else "rejected"), request_id,
                {"snapshot_hash": row["snapshot_hash"], "deployment_id": deployment_id,
                 "routing_revision_id": routing_revision_id, "reason": payload.reason})
            return self.get(request_id, context)

    def cancel(self, request_id, payload, context):
        with self.db.transaction():
            self.lock_project(context)
            context = self._account_context(context)
            row = self._raw(request_id, context)
            if row["requested_by"] != context.user_id:
                raise AuthAuthorizationError("Only the requester may cancel a release")
            if row["version"] != payload.version or row["status"] != "PENDING":
                raise ConflictError("Release request changed or was already decided")
            now = self.db.current_time().isoformat()
            self.db.execute("""UPDATE release_requests SET status='CANCELLED',version=version+1,
                decided_by=?,decision_reason=?,decided_at=?,updated_at=? WHERE id=?""",
                (context.user_id, payload.reason, now, now, request_id))
            self.audit(context, "release.cancelled", request_id, {"reason": payload.reason})
            return self.get(request_id, context)


def require_approved_deployment(db, deployment):
    if deployment["environment"] != "production":
        return
    request_id = deployment.get("release_request_id")
    if not request_id:
        raise ConflictError("Legacy production deployment requires a reviewed release before accepting new work")
    context = TenantContext(tenant_id=deployment["tenant_id"], project_id=deployment["project_id"])
    row = ReleaseService(db)._raw(request_id, context)
    snapshot = row["snapshot"]
    if (row["status"] != "APPLIED" or row["deployment_id"] != deployment["id"]
            or not row["decided_by"] or row["decided_by"] == row["requested_by"]
            or snapshot["resolved_plan_id"] != deployment["resolved_plan_id"]
            or snapshot["agent_revision_id"] != deployment["agent_revision_id"]
            or snapshot["agent_id"] != deployment["agent_id"]):
        raise ConflictError("Deployment does not have a valid independent release approval")
