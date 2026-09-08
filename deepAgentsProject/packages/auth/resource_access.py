from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime

from packages.auth.permissions import Permission, authorize
from packages.auth.service import AuthenticationError, AuthAuthorizationError
from packages.domain.models import TenantContext


def refresh_context(db, context: TenantContext) -> TenantContext:
    """Revalidate an existing authority; never expand a saved role snapshot.

    Background Runs do not depend on the browser session staying signed in.
    They do depend on the account remaining active and retaining its roles.
    Unknown proxy identities must be provisioned locally in production.
    """
    user = db.fetch_one("SELECT * FROM users WHERE id=?", (context.user_id,))
    if user is None:
        if os.getenv("DEEPAGENT_ENVIRONMENT", "development").lower() in {"prod", "production"}:
            raise AuthAuthorizationError("The principal must have an active provisioned account")
        return context
    if (user["status"] != "ACTIVE" or user["tenant_id"] != context.tenant_id
            or user["project_id"] != context.project_id or user["environment_id"] != context.environment_id):
        raise AuthAuthorizationError("The principal's authorization has been revoked")
    if context.session_id:
        session = db.fetch_one("SELECT * FROM auth_sessions WHERE id=? AND user_id=?", (context.session_id, context.user_id))
        if (not session or session["revoked_at"] or datetime.fromisoformat(session["expires_at"]) <= db.current_time()):
            raise AuthenticationError("Session is no longer active")
    return context.model_copy(update={
        "roles": [role for role in context.roles if role in user["roles"]],
        "is_super_admin": context.is_super_admin and bool(user["is_super_admin"]),
    })


def document_policy(document):
    return {key: document[key] for key in ("tenant_id", "project_id", "created_by", "visibility", "allowed_roles")}


def policy_digest(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def document_allowed(document, context):
    if document["tenant_id"] != context.tenant_id or document["project_id"] != context.project_id:
        return False
    if document.get("visibility") == "private" and document.get("created_by") != context.user_id:
        return False
    roles = document.get("allowed_roles") or []
    return not roles or bool(set(roles).intersection(context.roles)) or "owner" in context.roles


class ResourceAccess:
    """Thread ACL AND every acquired source's frozen AND current ACL.

    Sources only accumulate, including failed/aborted attempts: outputs, tool
    state, checkpoints and future turns can all retain derived information.
    Administrators have no implicit bypass of private content.
    """

    def __init__(self, db):
        self.db = db

    def require_deployment(self, deployment_id, context, *, active=False):
        context = refresh_context(self.db, context)
        deployment = self.db.fetch_one("""SELECT * FROM agent_deployments
            WHERE id=? AND tenant_id=? AND project_id=? AND environment=?""",
            (deployment_id, context.tenant_id, context.project_id,
             context.environment_id.removeprefix("env_")))
        if not deployment or (active and deployment["status"] != "ACTIVE"):
            from packages.application.services import NotFoundError
            raise NotFoundError("Agent deployment not found in the authorized environment")
        if active and deployment["environment"] == "production":
            from packages.releases.service import require_approved_deployment
            require_approved_deployment(self.db, deployment)
        return deployment

    @staticmethod
    def thread_scope(context, alias="t"):
        """SQL prefilter; current/frozen source policies are checked on delivery.

        Alias is internal, never supplied by a request. Source policy checks
        remain mandatory even when this coarse predicate accepts the row.
        """
        if alias not in {"t", "threads"}:
            raise ValueError("Invalid thread scope alias")
        return f"""{alias}.tenant_id=? AND {alias}.project_id=? AND {alias}.access_state='ACTIVE'
            AND EXISTS (SELECT 1 FROM agent_deployments ad WHERE ad.id={alias}.agent_deployment_id
                AND ad.tenant_id={alias}.tenant_id AND ad.project_id={alias}.project_id AND ad.environment=?)
            AND ({alias}.legacy_access=0 OR {alias}.owner_user_id=?)
            AND ({alias}.owner_user_id=? OR {alias}.visibility='project' OR
                ({alias}.visibility='members' AND EXISTS (SELECT 1 FROM thread_members tm
                    WHERE tm.thread_id={alias}.id AND tm.user_id=?)))""", (
            context.tenant_id, context.project_id, context.environment_id.removeprefix("env_"),
            context.user_id, context.user_id, context.user_id,
        )

    def require_execution(self, run_id):
        run = self.db.fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run or not run.get("principal_verified"):
            raise AuthAuthorizationError("Run has no verified principal")
        context = TenantContext(tenant_id=run["tenant_id"], project_id=run["project_id"],
            environment_id=run["principal_environment_id"], user_id=run["principal_user_id"],
            roles=run["principal_roles"])
        context = refresh_context(self.db, context)
        from packages.application.services import NotFoundError
        plan_row = self.db.fetch_one("SELECT plan_json FROM resolved_execution_plans WHERE id=?",(run["resolved_plan_id"],))
        model_id = (plan_row or {}).get("plan",{}).get("model_deployment_revision_id")
        model = self.db.fetch_one("SELECT status FROM model_deployments WHERE id=? AND tenant_id=? AND project_id=?",
            (model_id,context.tenant_id,context.project_id))
        if not model or model["status"] != "healthy":
            raise AuthAuthorizationError("Run model deployment has been disabled")
        try:
            deployment = self.require_deployment(run["agent_deployment_id"], context)
            if deployment["status"] not in {"ACTIVE", "DRAINING"}:
                raise NotFoundError("Run deployment is disabled")
        except NotFoundError as exc:
            raise AuthAuthorizationError("Run deployment access has been revoked") from exc
        if not self.can_thread(run["thread_id"], context, write=True):
            raise AuthAuthorizationError("Run authorization or source access has been revoked")
        return context

    def can_thread(self, thread_id, context, *, write=False):
        context = refresh_context(self.db, context)
        authorize(context, Permission.RUNTIME_USE if write else Permission.RUNTIME_READ)
        clause, params = self.thread_scope(context)
        thread = self.db.fetch_one(f"SELECT t.* FROM threads t WHERE t.id=? AND {clause}",
                                   (thread_id, *params))
        if not thread or thread["access_state"] != "ACTIVE":
            return False
        owner = thread["owner_user_id"] == context.user_id
        member = self.db.fetch_one("SELECT access FROM thread_members WHERE thread_id=? AND user_id=?", (thread_id, context.user_id))
        granted = owner or thread["visibility"] == "project" or (
            thread["visibility"] == "members" and member and (not write or member["access"] == "write"))
        if not granted:
            return False
        # Legacy content has no historical consent record. It cannot be widened
        # by sharing an old thread; a clean new thread is required instead.
        if thread["legacy_access"] and not owner:
            return False
        for source in self.db.fetch_all("SELECT * FROM thread_knowledge_sources WHERE thread_id=?", (thread_id,)):
            try:
                policy = json.loads(source["policy_json"])
                if not isinstance(policy, dict):
                    return False
            except (TypeError, ValueError):
                return False
            current = self.db.fetch_one("SELECT * FROM knowledge_documents WHERE id=?", (source["document_id"],))
            if (policy_digest(policy) != source["policy_hash"] or not current
                    or not document_allowed(policy, context) or not document_allowed(current, context)):
                return False
        return True

    def require_thread(self, thread_id, context, *, write=False):
        if not self.can_thread(thread_id, context, write=write):
            # Existence, private titles, source identities and ACLs are concealed.
            from packages.application.services import NotFoundError
            raise NotFoundError("Thread not found or not accessible")

    def require_run(self, run_id, context, *, write=False):
        run = self.db.fetch_one("SELECT * FROM runs WHERE id=? AND tenant_id=? AND project_id=?",
                                (run_id, context.tenant_id, context.project_id))
        if not run:
            from packages.application.services import NotFoundError
            raise NotFoundError("Run not found")
        self.require_thread(run["thread_id"], context, write=write)
        return run

    def acquire_sources(self, run_id, context, hits):
        from packages.persistence.fencing import RunWriteFence, current_write_fence, LeaseLostError
        fence = current_write_fence()
        if not isinstance(fence, RunWriteFence) or fence.run_id != run_id:
            raise LeaseLostError("Knowledge provenance requires the active Run lease")
        with self.db.transaction():
            context = refresh_context(self.db, context)
            run = self.require_run(run_id, context, write=True)
            if run["principal_user_id"] != context.user_id:
                raise LeaseLostError("Knowledge provenance principal does not match the Run")
            for hit in hits:
                source = self.db.fetch_one("""SELECT d.*,c.document_version_id FROM knowledge_chunks c
                    JOIN knowledge_documents d ON d.id=c.document_id WHERE c.id=?""", (hit["chunk_id"],))
                if not source or not document_allowed(source, context):
                    raise AuthAuthorizationError("Knowledge source access changed before delivery")
                policy = document_policy(source)
                self.db.execute("""INSERT INTO thread_knowledge_sources
                    (thread_id,document_id,document_version_id,policy_hash,policy_json,acquired_by,created_at)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""", (
                    run["thread_id"], source["id"], source["document_version_id"], policy_digest(policy),
                    self.db.encode(policy), context.user_id, self.db.current_time().isoformat(),
                ))
