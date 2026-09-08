"""Production routing changes share the deployment/grant serialization boundary."""
from datetime import datetime, timedelta
import json

from packages.application.services import ConflictError, NotFoundError, new_id
from packages.auth.permissions import Permission, authorize
from packages.auth.service import AuthAuthorizationError
from packages.domain.models import TenantContext
from packages.persistence.pagination import authorized_page
from packages.releases.service import ReleaseService, digest, require_approved_deployment
from packages.routing.models import RoutingProfileUpdate


class RoutingChangeService:
    def __init__(self, db, routing, models):
        self.db, self.routing = db, routing
        self.releases = ReleaseService(db, models)

    @staticmethod
    def _scope(context):
        # A release grant authorizes this control-plane view, not conversation access.
        return context.model_copy(update={"environment_id": "env_production"})

    @staticmethod
    def _profile(row):
        return {key: row[key] for key in ("id", "revision_number", "mode", "config",
            "taxonomy_version", "model_snapshot", "approval_state", "created_at")}

    def profile(self, context):
        if not self.releases._can_view({"environment": "production"}, context):
            raise AuthAuthorizationError("Production routing visibility requires a release grant")
        scope = self._scope(context)
        current = self.routing._ensure_profile(scope)
        return {"profile": self._profile(current), "deployments": [self.routing._public_deployment(item)
            for item in self.routing._active_deployments(scope)]}

    def history(self, context, *, limit=50, cursor=None):
        self.profile(context)
        scope = self._scope(context)
        page = authorized_page(self.db, query="""SELECT r.* FROM intent_router_revisions r
            WHERE r.tenant_id=? AND r.project_id=? AND r.environment_id='env_production'""",
            params=(context.tenant_id, context.project_id), alias="r", resource="production-routing-history",
            context=scope, visible=lambda row: self.releases._can_view({"environment": "production"}, context),
            limit=limit, cursor=cursor)
        page["items"] = [self._profile(row) for row in page["items"]]
        return page

    def _authority(self, context, *, approve=False):
        context, grant = self.releases.require_target(context, "production", approve=approve)
        authorize(context, Permission.ROUTING_APPROVE if approve else Permission.ROUTING_REQUEST)
        return context, grant

    def _raw(self, request_id, context):
        row = self.db.fetch_one("""SELECT * FROM routing_change_requests
            WHERE id=? AND tenant_id=? AND project_id=?""", (request_id, context.tenant_id, context.project_id))
        if not row:
            raise NotFoundError("Routing change request not found")
        try:
            snapshot = json.loads(row["snapshot_json"])
            valid = digest(snapshot) == row["snapshot_hash"] and all(snapshot[key] == row[key] for key in (
                "id", "tenant_id", "project_id", "environment", "requested_by", "created_at", "expires_at"))
        except (ValueError, TypeError, KeyError):
            valid = False
        if not valid:
            raise ConflictError("Routing request integrity check failed")
        row["snapshot"] = snapshot
        return row

    def _public(self, row):
        snapshot = row["snapshot"]
        result = {key: row[key] for key in ("id", "environment", "requested_by", "status", "version",
            "router_revision_id", "decided_by", "decision_reason", "decided_at", "created_at", "expires_at", "snapshot_hash")}
        result.update({key: snapshot[key] for key in ("action", "reason", "rollback_revision_id", "before", "after", "targets")})
        result["expired"] = row["status"] == "PENDING" and datetime.fromisoformat(row["expires_at"]) <= self.db.current_time()
        return result

    def get(self, request_id, context):
        row = self._raw(request_id, context)
        if not self.releases._can_view(row, context):
            raise NotFoundError("Routing change request not found")
        return self._public(row)

    def list(self, context, *, limit=50, cursor=None):
        context = self.releases._account_context(context)
        authorize(context, Permission.ROUTING_READ)
        page = authorized_page(self.db, query="""SELECT r.* FROM routing_change_requests r
            WHERE r.tenant_id=? AND r.project_id=?""", params=(context.tenant_id, context.project_id),
            alias="r", resource="routing-change-requests", context=context,
            visible=lambda row: self.releases._can_view(row, context), limit=limit, cursor=cursor)
        page["items"] = [self._public(self._raw(row["id"], context)) for row in page["items"]]
        return page

    def _targets(self, profile, context):
        scope = self._scope(context)
        facts = {}
        for intent, deployment_id in sorted(profile["target_deployments"].items()):
            if deployment_id is None:
                continue
            deployment = self.routing._deployment(deployment_id, scope)
            if not self.routing._supports_intent(deployment, intent):
                raise ConflictError(f"Deployment does not support the {intent} route")
            require_approved_deployment(self.db, deployment)
            channel = self.releases._channel(deployment["agent_id"], context)
            if channel["active_deployment_id"] != deployment_id:
                raise ConflictError("Routing target is not the current approved production deployment")
            _, plan, evaluation, policy_version = self.releases._candidate(deployment["agent_revision_id"], context)
            facts[intent] = {"deployment_id": deployment_id, "channel_version": channel["version"],
                "plan_hash": plan["plan_hash"], "evaluation_id": evaluation["id"],
                "evaluation_hash": evaluation["result_hash"], "policy_version": policy_version}
        return facts

    def create(self, payload, context, idempotency_key=None):
        with self.db.transaction():
            self.releases.lock_project(context)
            self.releases._lock_accounts([context.user_id])
            context, grant = self._authority(context)
            scope = f"routing-change:{context.project_id}:{context.user_id}"
            request_hash = digest(payload.model_dump())
            if idempotency_key:
                saved = self.db.fetch_one("SELECT response_json FROM idempotency_records WHERE tenant_id=? AND scope=? AND key=?",
                    (context.tenant_id, scope, idempotency_key))
                if saved:
                    saved = json.loads(saved["response_json"])
                    if saved["request_hash"] != request_hash:
                        raise ConflictError("Routing idempotency key was used for different content")
                    return self.get(saved["id"], context)
            current = self.routing._ensure_profile(self._scope(context))
            if current["id"] != payload.expected_router_revision_id:
                raise ConflictError("Production routing changed; reload before requesting a review")
            if payload.action == "rollback":
                target = self.db.fetch_one("""SELECT * FROM intent_router_revisions
                    WHERE id=? AND tenant_id=? AND project_id=? AND environment_id='env_production'""",
                    (payload.rollback_revision_id, context.tenant_id, context.project_id))
                if not target or target["approval_state"] != "APPROVED":
                    raise NotFoundError("Previously approved production routing revision not found")
                if target["id"] == current["id"]:
                    raise ConflictError("Rollback target is already current")
                candidate = RoutingProfileUpdate(mode=target["mode"], **target["config"]).model_dump()
            else:
                candidate = payload.profile.model_dump()
                candidate["target_deployments"] = {**current["config"]["target_deployments"], **candidate["target_deployments"]}
            targets = self._targets(candidate, context)
            now = self.db.current_time()
            snapshot = {"id": new_id("routing_change"), "tenant_id": context.tenant_id, "project_id": context.project_id,
                "environment": "production", "requested_by": context.user_id,
                "requester_context": context.model_dump(exclude={"session_id", "is_super_admin"}), "grant_version": grant["version"],
                "action": payload.action, "rollback_revision_id": payload.rollback_revision_id, "reason": payload.reason,
                "before": self._profile(current), "after": candidate, "targets": targets,
                "classifier_identity": self.routing.model_gateway.identity(),
                "created_at": now.isoformat(), "expires_at": (now + timedelta(seconds=payload.expires_in_seconds)).isoformat()}
            self.db.execute("""INSERT INTO routing_change_requests
                (id,tenant_id,project_id,environment,requested_by,snapshot_json,snapshot_hash,status,version,created_at,expires_at,updated_at)
                VALUES(?,?,?,'production',?,?,?,'PENDING',1,?,?,?)""",
                (snapshot["id"], context.tenant_id, context.project_id, context.user_id, self.db.encode(snapshot), digest(snapshot),
                 snapshot["created_at"], snapshot["expires_at"], snapshot["created_at"]))
            if idempotency_key:
                self.db.execute("INSERT INTO idempotency_records(tenant_id,scope,key,response_json,created_at) VALUES(?,?,?,?,?)",
                    (context.tenant_id, scope, idempotency_key, self.db.encode({"id": snapshot["id"], "request_hash": request_hash}), snapshot["created_at"]))
            self.releases.audit(context, "routing.change.requested", snapshot["id"],
                {"snapshot_hash": digest(snapshot), "reason": payload.reason})
            return self.get(snapshot["id"], context)

    def decide(self, request_id, payload, context):
        with self.db.transaction():
            self.releases.lock_project(context)
            row = self._raw(request_id, context)
            self.releases._lock_accounts([context.user_id, row["requested_by"]])
            context, _ = self._authority(context, approve=True)
            if context.user_id == row["requested_by"]:
                raise AuthAuthorizationError("The requester cannot decide their own routing change; cancel it instead")
            status = "APPLIED" if payload.decision == "approve" else "REJECTED"
            if (row["status"] == status and row["version"] == payload.version + 1
                    and row["decided_by"] == context.user_id and row["decision_reason"] == payload.reason):
                return self._public(row)
            if row["status"] != "PENDING" or row["version"] != payload.version:
                raise ConflictError("Routing change was already decided or changed")
            if datetime.fromisoformat(row["expires_at"]) <= self.db.current_time():
                raise ConflictError("Routing change expired; request a new review")
            snapshot = row["snapshot"]
            revision_id = None
            if payload.decision == "approve":
                requester, grant = self._authority(TenantContext(**snapshot["requester_context"]))
                if grant["version"] != snapshot["grant_version"]:
                    raise ConflictError("Requester authority changed; request a new review")
                current = self.routing._ensure_profile(self._scope(context))
                if self._profile(current) != snapshot["before"]:
                    raise ConflictError("Production routing changed; request a new review")
                if (self._targets(snapshot["after"], requester) != snapshot["targets"]
                        or self.routing.model_gateway.identity() != snapshot["classifier_identity"]):
                    raise ConflictError("Routing targets or model/evaluation evidence changed; request a new review")
                revision_id = new_id("router")
                config = {key: value for key, value in snapshot["after"].items() if key != "mode"}
                self.db.execute("UPDATE intent_router_revisions SET status='SUPERSEDED' WHERE id=?", (current["id"],))
                self.db.execute("""INSERT INTO intent_router_revisions
                    (id,tenant_id,project_id,environment_id,revision_number,taxonomy_version,mode,config_json,
                     model_snapshot_json,status,created_at,approval_state)
                    VALUES(?,?,?,'env_production',?,?,?,?,?,'ACTIVE',?,'APPROVED')""",
                    (revision_id, context.tenant_id, context.project_id, current["revision_number"] + 1,
                     self.routing.TAXONOMY_VERSION, snapshot["after"]["mode"], self.db.encode(config),
                     self.db.encode(snapshot["classifier_identity"]), self.db.current_time().isoformat()))
            now = self.db.current_time().isoformat()
            self.db.execute("""UPDATE routing_change_requests SET status=?,version=version+1,router_revision_id=?,
                decided_by=?,decision_reason=?,decided_at=?,updated_at=? WHERE id=? AND version=? AND status='PENDING'""",
                (status, revision_id, context.user_id, payload.reason, now, now, request_id, payload.version))
            self.releases.audit(context, "routing." + (snapshot["action"] + ".applied" if revision_id else "change.rejected"),
                request_id, {"snapshot_hash": row["snapshot_hash"], "router_revision_id": revision_id, "reason": payload.reason})
            return self.get(request_id, context)

    def cancel(self, request_id, payload, context):
        with self.db.transaction():
            self.releases.lock_project(context)
            self.releases._lock_accounts([context.user_id])
            context = self.releases._account_context(context)
            row = self._raw(request_id, context)
            if row["requested_by"] != context.user_id:
                raise AuthAuthorizationError("Only the requester may cancel a routing change")
            if (row["status"] == "CANCELLED" and row["version"] == payload.version + 1
                    and row["decision_reason"] == payload.reason):
                return self._public(row)
            if row["version"] != payload.version or row["status"] != "PENDING":
                raise ConflictError("Routing change was already decided or changed")
            now = self.db.current_time().isoformat()
            self.db.execute("""UPDATE routing_change_requests SET status='CANCELLED',version=version+1,
                decided_by=?,decision_reason=?,decided_at=?,updated_at=? WHERE id=?""",
                (context.user_id, payload.reason, now, now, request_id))
            self.releases.audit(context, "routing.change.cancelled", request_id, {"reason": payload.reason})
            return self._public(self._raw(request_id, context))
