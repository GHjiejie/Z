from __future__ import annotations

from packages.auth.resource_access import ResourceAccess, refresh_context

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from packages.application.services import ConflictError, NotFoundError, new_id
from packages.auth.permissions import Permission, authorize
from packages.auth.transactions import authorized_write, current_authority
from packages.domain.models import RunCreate, TenantContext, ThreadCreate, utc_now
from packages.persistence import Database
from packages.persistence.pagination import authorized_page, PageAccessChanged
from packages.routing.classifier import HybridIntentClassifier, IntentClassifier
from packages.routing.models import (
    IntentClassification,
    IntentRoutingResolve,
    RoutedRunCreate,
    RoutingProfileUpdate,
)
from packages.runtime.event_emitter import EventEmitter
from packages.runtime.model_gateway import ModelGateway
from packages.runtime.run_service import RunService
from packages.runtime.owned_io import owned_io


class IntentRoutingService:
    TAXONOMY_VERSION = "1.0"

    def __init__(
        self,
        db: Database,
        model_gateway: ModelGateway,
        runs: RunService,
        events: EventEmitter,
        classifier: Optional[IntentClassifier] = None,
    ):
        self.db = db
        self.model_gateway = model_gateway
        self.runs = runs
        self.events = events
        self.classifier = classifier or HybridIntentClassifier(model_gateway, db)

    def get_profile(self, context: TenantContext) -> Dict[str, Any]:
        profile = self._ensure_profile(context)
        profile["target_details"] = {
            intent: self._deployment_summary(deployment_id, context)
            if deployment_id
            else None
            for intent, deployment_id in profile["config"].get(
                "target_deployments", {}
            ).items()
        }
        return profile

    def update_profile(
        self, payload: RoutingProfileUpdate, context: TenantContext
    ) -> Dict[str, Any]:
        from packages.releases.service import ReleaseService
        with self.db.transaction():
            # Keep release's project -> sorted accounts -> resource order.
            ReleaseService(self.db).lock_project(context)
            with authorized_write(self.db, context, Permission.ROUTING_MANAGE) as context:
                return self._update_profile(payload, context)

    def _update_profile(self, payload, context):
        if context.environment_id == "env_production":
            raise ConflictError("Production routing requires an independently approved routing change request")
        current = self._ensure_profile(context)
        targets = dict(current["config"].get("target_deployments", {}))
        targets.update(payload.target_deployments)
        for intent, deployment_id in targets.items():
            if deployment_id is None:
                continue
            deployment = self._deployment(deployment_id, context)
            if intent == "coding" and not self._is_coding(deployment):
                raise ConflictError("The coding route must target a Coding Agent deployment")
            if intent == "knowledge" and not self._has_knowledge(deployment):
                raise ConflictError(
                    "The knowledge route must target a deployment with immutable knowledge bindings"
                )
            if intent in {"release", "general"} and self._is_coding(deployment):
                raise ConflictError(
                    f"The {intent} route must target a non-Coding Agent deployment"
                )

        now = utc_now()
        profile_id = new_id("router")
        revision_number = int(current["revision_number"]) + 1
        config = {
            "auto_route_threshold": payload.auto_route_threshold,
            "confirmation_threshold": payload.confirmation_threshold,
            "decision_ttl_seconds": payload.decision_ttl_seconds,
            "target_deployments": targets,
        }
        with self.db.transaction():
            self.db.execute(
                """UPDATE intent_router_revisions SET status='SUPERSEDED'
                   WHERE tenant_id=? AND project_id=? AND environment_id=? AND status='ACTIVE'""",
                (context.tenant_id, context.project_id, context.environment_id),
            )
            self.db.execute(
                """INSERT INTO intent_router_revisions
                   (id, tenant_id, project_id, environment_id, revision_number,
                    taxonomy_version, mode, config_json, model_snapshot_json,
                    status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)""",
                (
                    profile_id,
                    context.tenant_id,
                    context.project_id,
                    context.environment_id,
                    revision_number,
                    self.TAXONOMY_VERSION,
                    payload.mode,
                    self.db.encode(config),
                    self.db.encode(self.model_gateway.identity()),
                    now,
                ),
            )
            self.db.execute("""INSERT INTO governance_audit_events
                (id,tenant_id,project_id,actor_user_id,action,resource_id,details_json,created_at)
                VALUES(?,?,?,?,?,?,?,?)""", (new_id("audit"), context.tenant_id, context.project_id,
                context.user_id, "routing.profile.updated", profile_id,
                self.db.encode({"previous_revision_id": current["id"], "revision_number": revision_number,
                                "environment_id": context.environment_id}), now))
        return self.get_profile(context)

    async def resolve(
        self, payload: IntentRoutingResolve, context: TenantContext
    ) -> Dict[str, Any]:
        authorize(context, Permission.ROUTING_USE)
        profile = self._ensure_profile(context)
        governed = context.environment_id == "env_production"
        if governed and profile["approval_state"] != "APPROVED" and not payload.preferred_deployment_id:
            raise ConflictError("Production routing requires approval; submit a routing change request")
        if profile["mode"] == "disabled" and not payload.preferred_deployment_id:
            classification = IntentClassification(
                primary_intent="general",
                subtype="routing_disabled",
                confidence=1.0,
                summary="意图路由已停用，使用项目默认 Agent。",
                source="fallback",
            )
        else:
            classification = await self.classifier.classify(payload.input, context)
        deployments = self._active_deployments(context)
        if not deployments:
            raise ConflictError("No active Agent deployment is available for routing")

        config = profile["config"]
        selected: Optional[Dict[str, Any]] = None
        predicted: Optional[Dict[str, Any]] = None
        reason = "intent_target_selected"
        fallback = False
        manual_override = bool(payload.preferred_deployment_id)

        if payload.preferred_deployment_id:
            selected = self._deployment(payload.preferred_deployment_id, context)
            predicted = selected
            reason = "user_selected_deployment"
        else:
            predicted = self._target_for_intent(
                classification.primary_intent, config, deployments, allow_discovery=not governed
            )
            selected = predicted

        default_deployment = self._default_deployment(config, deployments, allow_discovery=not governed)
        mode = profile["mode"]
        if mode == "disabled" and not manual_override:
            selected = default_deployment
            reason = "routing_disabled"
            fallback = True
        elif mode == "shadow" and not manual_override:
            selected = default_deployment
            reason = "shadow_mode_default_selected"
            fallback = True
        elif selected is None:
            selected = default_deployment
            reason = f"{classification.primary_intent}_target_unavailable"
            fallback = True

        if selected is None:
            raise ConflictError("No default Agent deployment is available")

        auto_threshold = float(config.get("auto_route_threshold", 0.80))
        confirmation_threshold = float(config.get("confirmation_threshold", 0.55))
        confirmation_required = bool(
            not manual_override
            and (
                classification.primary_intent == "ambiguous"
                or classification.confidence < auto_threshold
            )
        )
        low_confidence = classification.confidence < confirmation_threshold
        workspace_required = self._is_coding(selected) and payload.workspace is None

        if workspace_required:
            status = "NEEDS_WORKSPACE"
        elif confirmation_required:
            status = "NEEDS_CONFIRMATION"
        elif fallback:
            status = "FALLBACK"
        else:
            status = "READY"

        requirements = {
            "workspace": workspace_required,
            "confirmation": confirmation_required,
            "low_confidence": low_confidence,
        }
        decision_id = new_id("route")
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(
            seconds=int(config.get("decision_ttl_seconds", 600))
        )
        candidate_summaries = [self._public_deployment(item) for item in deployments]
        self.db.execute(
            """INSERT INTO intent_routing_decisions
               (id, tenant_id, project_id, environment_id, router_revision_id,
                input_hash, classification_json, status, selected_deployment_id,
                predicted_deployment_id, candidate_deployments_json, reason,
                requirements_json, override_deployment_id, thread_id, run_id,
                expires_at, created_at, committed_at, owner_user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL, ?)""",
            (
                decision_id,
                context.tenant_id,
                context.project_id,
                context.environment_id,
                profile["id"],
                self._input_hash(payload.input),
                self.db.encode(classification.model_dump()),
                status,
                selected["id"],
                predicted["id"] if predicted else None,
                self.db.encode(candidate_summaries),
                reason,
                self.db.encode(requirements),
                selected["id"] if manual_override else None,
                expires_at.isoformat(),
                now.isoformat(),
                context.user_id,
            ),
        )
        return self.get_decision(decision_id, context)

    async def create_routed_run(
        self, payload: RoutedRunCreate, context: TenantContext
    ) -> Dict[str, Any]:
        context = current_authority(self.db, context, Permission.ROUTING_USE, Permission.RUNTIME_USE)
        decision, deployment, _, _, _ = self._routed_state(payload, context)
        if decision.get('run_id'):
            return {'decision': decision, 'thread': self.runs.get_thread(decision['thread_id'], context),
                    'run': self.runs.get_run(decision['run_id'], context)}
        thread_payload = ThreadCreate(agent_deployment_id=deployment['id'],
            title=(payload.title or payload.input.strip()[:80] or 'New agent task').strip(), workspace=payload.workspace)
        prepared = await owned_io(self.runs.prepare_thread_workspace, thread_payload, context)
        with self.db.transaction():
            if context.environment_id == "env_production":
                from packages.releases.service import ReleaseService
                ReleaseService(self.db).lock_project(context)
            if self.db.dialect == "postgresql":
                self.db.fetch_one(
                    "SELECT id FROM intent_routing_decisions WHERE id=? AND tenant_id=? AND project_id=? FOR UPDATE",
                    (payload.decision_id, context.tenant_id, context.project_id),
                )
            decision, deployment, requirements, overridden, manual_selected = self._routed_state(payload, context)
            if decision.get("run_id"):
                return {
                    "decision": decision,
                    "thread": self.runs.get_thread(decision["thread_id"], context),
                    "run": self.runs.get_run(decision["run_id"], context),
                }
            deployment_id = deployment['id']
            title = (payload.title or payload.input.strip()[:80] or "New agent task").strip()
            thread = self.runs.create_thread(
                ThreadCreate(
                    agent_deployment_id=deployment_id,
                    title=title,
                    workspace=payload.workspace,
                ),
                context,
                routing_decision_id=decision["id"],
                prepared_workspace=prepared,
            )
            run = await self.runs.create_run(
                thread["id"],
                RunCreate(input=payload.input),
                context,
                idempotency_key=f"intent-routing:{decision['id']}",
                enqueue=False,
            )
            committed_at = utc_now()
            self.db.execute(
                """UPDATE intent_routing_decisions
                   SET selected_deployment_id=?, override_deployment_id=?, reason=?,
                       thread_id=?, run_id=?, committed_at=?
                   WHERE id=? AND tenant_id=? AND project_id=?""",
                (
                    deployment_id,
                    payload.override_deployment_id or decision.get("override_deployment_id"),
                    "user_overrode_routing_decision" if overridden else decision["reason"],
                    thread["id"],
                    run["id"],
                    committed_at,
                    decision["id"],
                    context.tenant_id,
                    context.project_id,
                ),
            )
            classification = IntentClassification.model_validate(
                decision["classification"]
            )
            self.events.append(
                run["id"],
                "intent.classification.started",
                {
                    "decision_id": decision["id"],
                    "router_revision_id": decision["router_revision_id"],
                    "taxonomy_version": classification.taxonomy_version,
                },
                span_id="span_intent_routing",
                execution_path=["routing", "classify"],
            )
            self.events.append(
                run["id"],
                "intent.classification.completed",
                {
                    "decision_id": decision["id"],
                    "primary_intent": classification.primary_intent,
                    "secondary_intents": classification.secondary_intents,
                    "subtype": classification.subtype,
                    "confidence": classification.confidence,
                    "source": classification.source,
                    "requires_repository": classification.requires_repository,
                    "requires_knowledge": classification.requires_knowledge,
                    "risk_hint": classification.risk_hint,
                    "summary": classification.summary,
                },
                span_id="span_intent_routing",
                execution_path=["routing", "classify"],
            )
            if requirements.get("workspace"):
                self.events.append(
                    run["id"],
                    "routing.workspace_required",
                    {
                        "decision_id": decision["id"],
                        "resolved": True,
                        "repository_id": payload.workspace.repository_id
                        if payload.workspace
                        else None,
                        "base_ref": payload.workspace.base_ref if payload.workspace else None,
                    },
                    span_id="span_intent_routing",
                    execution_path=["routing", "workspace"],
                )
            route_event = (
                "routing.user_overridden"
                if manual_selected
                else "routing.fallback"
                if decision["status"] == "FALLBACK"
                else "routing.agent.selected"
            )
            self.events.append(
                run["id"],
                route_event,
                {
                    "decision_id": decision["id"],
                    "deployment_id": deployment["id"],
                    "agent_name": deployment["agent_name"],
                    "reason": "user_overrode_routing_decision" if overridden else decision["reason"],
                    "manual_override": manual_selected,
                },
                span_id="span_intent_routing",
                execution_path=["routing", "select_agent"],
            )
            enqueued = bool(self.runs.orchestrator and self.runs.orchestrator.enqueue_in_transaction(run["id"]))
        if not enqueued:
            await self.runs.enqueue_run(run["id"])
        return {
            "decision": self.get_decision(decision["id"], context),
            "thread": self.runs.get_thread(thread["id"], context),
            "run": self.runs.get_run(run["id"], context),
        }

    def _routed_state(self, payload, context):
        current_authority(self.db, context, Permission.ROUTING_USE, Permission.RUNTIME_USE)
        decision = self.get_decision(payload.decision_id, context)
        if self._input_hash(payload.input) != decision['input_hash']:
            raise ConflictError('Routing decision does not match the submitted input')
        if decision.get('run_id'):
            return decision, None, {}, False, False
        if context.environment_id == 'env_production':
            profile = self._ensure_profile(context)
            if profile['id'] != decision['router_revision_id']:
                raise ConflictError('Production routing changed; classify the request again')
            if profile['approval_state'] != 'APPROVED' and not decision.get('override_deployment_id'):
                raise ConflictError('Production routing requires approval')
        if datetime.fromisoformat(decision['expires_at']) <= self.db.current_time():
            raise ConflictError('Routing decision expired; classify the request again')
        deployment_id = payload.override_deployment_id or decision.get('selected_deployment_id')
        if not deployment_id:
            raise ConflictError('Routing decision has no executable Agent deployment')
        deployment = self._deployment(deployment_id, context)
        requirements = decision.get('requirements') or {}
        overridden = bool(payload.override_deployment_id and payload.override_deployment_id != decision.get('selected_deployment_id'))
        manual_selected = bool(decision.get('override_deployment_id') or overridden)
        if requirements.get('confirmation') and not (payload.confirmed or overridden):
            raise ConflictError('Routing decision requires explicit user confirmation')
        if self._is_coding(deployment) and payload.workspace is None:
            raise ConflictError('Coding Agent routing requires a repository workspace')
        if not self._is_coding(deployment) and payload.workspace is not None:
            raise ConflictError('Workspace can only be supplied to a Coding Agent route')
        return decision, deployment, requirements, overridden, manual_selected

    def get_decision(self, decision_id: str, context: TenantContext) -> Dict[str, Any]:
        decision = self.db.fetch_one(
            """SELECT * FROM intent_routing_decisions
               WHERE id=? AND tenant_id=? AND project_id=? AND environment_id=?""",
            (
                decision_id,
                context.tenant_id,
                context.project_id,
                context.environment_id,
            ),
        )
        if not decision or not self._decision_visible(decision, context):
            raise NotFoundError("Intent routing decision not found")
        decision["selected_deployment"] = self._deployment_summary(
            decision.get("selected_deployment_id"), context
        )
        decision["predicted_deployment"] = self._deployment_summary(
            decision.get("predicted_deployment_id"), context
        )
        decision["committed"] = bool(decision.get("committed_at"))
        return decision

    def list_decisions(
        self, context: TenantContext, limit: int = 100
    ) -> List[Dict[str, Any]]:
        return self.decision_page(context, limit=limit)["items"]

    def decision_page(self, context: TenantContext, limit=100, cursor=None):
        context = refresh_context(self.db, context)
        authorize(context, Permission.ROUTING_READ)
        page = authorized_page(self.db, query="""SELECT d.* FROM intent_routing_decisions d
               WHERE d.tenant_id=? AND d.project_id=? AND d.environment_id=?
               AND (d.owner_user_id=? OR d.thread_id IS NOT NULL)""",
               params=(context.tenant_id, context.project_id, context.environment_id, context.user_id),
               alias="d", resource="routing-decisions", context=context,
               visible=lambda row: self._decision_visible(row, context), limit=limit, cursor=cursor)
        for row in page["items"]:
            row["selected_deployment"] = self._deployment_summary(
                row.get("selected_deployment_id"), context
            )
            row["predicted_deployment"] = self._deployment_summary(
                row.get("predicted_deployment_id"), context
            )
            row["committed"] = bool(row.get("committed_at"))
        if any(not self._decision_visible(row, context) for row in page["items"]):
            raise PageAccessChanged("Access changed while loading routing decisions; reload the page")
        return page

    def _decision_visible(self, decision, context):
        context = refresh_context(self.db, context)
        if decision.get("thread_id"):
            return ResourceAccess(self.db).can_thread(decision["thread_id"], context)
        return decision.get("owner_user_id") == context.user_id

    def _ensure_profile(self, context: TenantContext) -> Dict[str, Any]:
        existing = self.db.fetch_one(
            """SELECT * FROM intent_router_revisions
               WHERE tenant_id=? AND project_id=? AND environment_id=? AND status='ACTIVE'
               ORDER BY revision_number DESC LIMIT 1""",
            (context.tenant_id, context.project_id, context.environment_id),
        )
        if existing:
            return existing
        with self.db.transaction():
            if context.environment_id == "env_production":
                from packages.releases.service import ReleaseService
                ReleaseService(self.db).lock_project(context)
            row = self.db.fetch_one(
                """SELECT * FROM intent_router_revisions
                   WHERE tenant_id=? AND project_id=? AND environment_id=? AND status='ACTIVE'
                   ORDER BY revision_number DESC LIMIT 1""",
                (context.tenant_id, context.project_id, context.environment_id),
            )
            if row:
                return row
            # Candidate selection must share the production release lock.
            config = self._initial_config(context)
            profile_id = new_id("router")
            self.db.execute(
                """INSERT INTO intent_router_revisions
                   (id, tenant_id, project_id, environment_id, revision_number,
                    taxonomy_version, mode, config_json, model_snapshot_json,
                    status, created_at)
                   VALUES (?, ?, ?, ?, 1, ?, 'active', ?, ?, 'ACTIVE', ?)""",
                (profile_id, context.tenant_id, context.project_id, context.environment_id,
                 self.TAXONOMY_VERSION, self.db.encode(config),
                 self.db.encode(self.model_gateway.identity()), utc_now()),
            )
            return self.db.fetch_one("SELECT * FROM intent_router_revisions WHERE id=?", (profile_id,))

    def _initial_config(self, context: TenantContext) -> Dict[str, Any]:
        deployments = self._active_deployments(context)
        coding = next((item for item in deployments if self._is_coding(item)), None)
        release = next(
            (
                item
                for item in deployments
                if item["agent_name"] == "Release Sentinel"
                and not self._is_coding(item)
            ),
            None,
        )
        knowledge = next(
            (item for item in deployments if self._has_knowledge(item)), None
        )
        general = next(
            (item for item in deployments if not self._is_coding(item)), release
        )
        return {
            "auto_route_threshold": 0.80,
            "confirmation_threshold": 0.55,
            "decision_ttl_seconds": 600,
            "target_deployments": {
                "coding": coding["id"] if coding else None,
                "release": release["id"] if release else None,
                "knowledge": knowledge["id"] if knowledge else None,
                "general": general["id"] if general else None,
            },
        }

    def _active_deployments(self, context: TenantContext) -> List[Dict[str, Any]]:
        environment = context.environment_id.removeprefix("env_")
        return self.db.fetch_all(
            """SELECT d.*, a.name AS agent_name, a.description AS agent_description,
                      p.plan_json
               FROM agent_deployments d
               JOIN agents a ON a.id=d.agent_id
               JOIN resolved_execution_plans p ON p.id=d.resolved_plan_id
               WHERE d.tenant_id=? AND d.project_id=? AND d.environment=?
                 AND d.status='ACTIVE'
               ORDER BY d.created_at""",
            (context.tenant_id, context.project_id, environment),
        )

    def _deployment(
        self, deployment_id: str, context: TenantContext
    ) -> Dict[str, Any]:
        deployment = next(
            (
                item
                for item in self._active_deployments(context)
                if item["id"] == deployment_id
            ),
            None,
        )
        if not deployment:
            raise NotFoundError("Active Agent deployment not found for this routing scope")
        return deployment

    def _target_for_intent(
        self,
        intent: str,
        config: Dict[str, Any],
        deployments: List[Dict[str, Any]],
        *, allow_discovery: bool = True,
    ) -> Optional[Dict[str, Any]]:
        target_id = (config.get("target_deployments") or {}).get(intent)
        target = next(
            (item for item in deployments if item["id"] == target_id), None
        )
        if target and self._supports_intent(target, intent):
            return target
        if not allow_discovery:
            return self._default_deployment(config, deployments, allow_discovery=False)
        if intent == "coding":
            return next((item for item in deployments if self._is_coding(item)), None)
        if intent == "knowledge":
            return next((item for item in deployments if self._has_knowledge(item)), None)
        if intent == "release":
            return next(
                (
                    item
                    for item in deployments
                    if item["agent_name"] == "Release Sentinel"
                    and not self._is_coding(item)
                ),
                None,
            )
        if intent in {"general", "ambiguous"}:
            return next((item for item in deployments if not self._is_coding(item)), None)
        return None

    def _default_deployment(
        self, config: Dict[str, Any], deployments: List[Dict[str, Any]], *, allow_discovery: bool = True
    ) -> Optional[Dict[str, Any]]:
        target_id = (config.get("target_deployments") or {}).get("general")
        if not allow_discovery:
            return next((item for item in deployments if item["id"] == target_id and not self._is_coding(item)), None)
        return next(
            (item for item in deployments if item["id"] == target_id),
            next((item for item in deployments if not self._is_coding(item)), deployments[0] if deployments else None),
        )

    @staticmethod
    def _is_coding(deployment: Dict[str, Any]) -> bool:
        return bool(((deployment.get("plan") or {}).get("coding_profile") or {}).get("enabled"))

    @staticmethod
    def _has_knowledge(deployment: Dict[str, Any]) -> bool:
        return bool((deployment.get("plan") or {}).get("knowledge_bindings"))

    def _supports_intent(self, deployment: Dict[str, Any], intent: str) -> bool:
        if intent == "coding":
            return self._is_coding(deployment)
        if intent == "knowledge":
            return self._has_knowledge(deployment)
        return not self._is_coding(deployment)

    def _deployment_summary(
        self, deployment_id: Optional[str], context: TenantContext
    ) -> Optional[Dict[str, Any]]:
        if not deployment_id:
            return None
        try:
            return self._public_deployment(self._deployment(deployment_id, context))
        except NotFoundError:
            return None

    def _public_deployment(self, deployment: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": deployment["id"],
            "name": deployment["name"],
            "agent_name": deployment["agent_name"],
            "environment": deployment["environment"],
            "coding_enabled": self._is_coding(deployment),
            "knowledge_enabled": self._has_knowledge(deployment),
        }

    @staticmethod
    def _input_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
