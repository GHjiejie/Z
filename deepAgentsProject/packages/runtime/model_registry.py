"""Operator-approved model profiles, project-scoped registration and binding."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.application.services import ConflictError, NotFoundError, new_id
from packages.auth.permissions import Permission, authorize
from packages.auth.resource_access import refresh_context
from packages.auth.transactions import authorized_write
from packages.billing.models import Change
from packages.http_security import validate_provider_url
from packages.runtime.model_gateway import OpenAICompatibleConfig, OpenAICompatibleModelGateway
from packages.secrets import read_secret


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    tenant_id: str = Field(min_length=1,max_length=100)
    project_id: str = Field(min_length=1,max_length=100)
    name: str = Field(min_length=1,max_length=100)
    model: str = Field(min_length=1,max_length=255)
    base_url: str = Field(max_length=255)
    credential_env: str = Field(pattern=r"^(OPENAI_API_KEY|DEEPAGENT_MODEL_KEY_[A-Z0-9_]+)$")
    api_style: Literal["chat_completions","responses","anthropic_messages"] = "chat_completions"
    auth_style: Literal["auto","bearer","anthropic"] = "auto"
    timeout_seconds: int = Field(default=180,ge=1,le=1800)
    max_completion_tokens: int = Field(default=4096,ge=1,le=1_000_000)
    temperature: float = Field(default=.7,ge=0,le=2,allow_inf_nan=False)
    reasoning_effort: str | None = Field(default=None,max_length=32)
    anthropic_thinking_mode: Literal["enabled","adaptive","disabled","provider_default"] = "provider_default"
    anthropic_thinking_budget_tokens: int = Field(default=2048,ge=1024,le=1_000_000)
    context_window_tokens: int = Field(default=131072,ge=1024,le=4_000_000)
    capabilities: list[str] = Field(default_factory=lambda:["streaming"],max_length=20)
    input_per_million: Decimal = Field(ge=0,le=1_000_000,allow_inf_nan=False)
    output_per_million: Decimal = Field(ge=0,le=1_000_000,allow_inf_nan=False)

    @field_validator("base_url")
    @classmethod
    def approved_url(cls,value):
        return validate_provider_url(value,allowlist_variable="DEEPAGENT_MODEL_ALLOWED_ORIGINS")

    def identity(self):
        return {"provider":self.api_style,"route":self.base_url,"model":self.model}

    @model_validator(mode="after")
    def token_limits(self):
        if self.max_completion_tokens > self.context_window_tokens:
            raise ValueError("Output limit exceeds the model context window")
        if self.api_style == "anthropic_messages" and self.anthropic_thinking_mode == "enabled" and self.anthropic_thinking_budget_tokens >= self.max_completion_tokens:
            raise ValueError("Thinking budget must be smaller than the output limit")
        return self

    def digest(self):
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"),sort_keys=True,separators=(",",":")).encode()).hexdigest()

    def binding(self):
        return {"profile_id":self.id,"profile_hash":self.digest(),"identity":self.identity()}

    def gateway(self):
        fields = {name:getattr(self,name) for name in ("base_url","model","api_style","auth_style",
            "timeout_seconds","max_completion_tokens","temperature","reasoning_effort",
            "anthropic_thinking_mode","anthropic_thinking_budget_tokens")}
        return OpenAICompatibleModelGateway(OpenAICompatibleConfig(**fields,
            api_key=read_secret(self.credential_env,required=True)))


class ModelRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(min_length=1,max_length=100)
    name: str | None = Field(default=None,min_length=1,max_length=100)
    reason: str = Field(min_length=5,max_length=500)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls,value):
        if len(value.strip()) < 5:
            raise ValueError("A meaningful change reason is required")
        return value.strip()


class ModelStatusUpdate(Change):
    enabled: bool


class ModelRegistry:
    def __init__(self, db, fallback_gateway, *, allow_test_override=False):
        self.db = db
        self.fallback_gateway = fallback_gateway
        self.allow_test_override = allow_test_override
        self.profiles = self._load_profiles()
        self._coding_models = {}

    @staticmethod
    def production():
        return os.getenv("DEEPAGENT_ENVIRONMENT","development").lower() in {"production","prod"}

    def _load_profiles(self):
        configured = os.getenv("DEEPAGENT_MODEL_PROFILES_FILE","")
        if not configured:
            return {}
        try:
            path = Path(configured)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 262144:
                raise ValueError()
            if self.production() and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise ValueError()
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data,list) or len(data)>1000:
                raise ValueError()
            profiles = [ModelProfile.model_validate(item) for item in data]
            if len({item.id for item in profiles}) != len(profiles):
                raise ValueError()
            return {profile.id:profile for profile in profiles}
        except (OSError,ValueError,TypeError):
            # Parser errors can include rejected secrets; never expose excerpts.
            raise RuntimeError("Invalid or unsafe model profiles configuration") from None

    def _profile(self, profile_id, tenant_id, project_id):
        profile = self.profiles.get(profile_id)
        if not profile or (profile.tenant_id,profile.project_id) != (tenant_id,project_id):
            raise NotFoundError("Approved model profile not found in this project")
        return profile

    def list_profiles(self, context):
        context = refresh_context(self.db,context)
        authorize(context,Permission.MODEL_MANAGE)
        return [{"id":item.id,"name":item.name,"identity":item.identity(),"profile_hash":item.digest(),
            "input_per_million":str(item.input_per_million),"output_per_million":str(item.output_per_million)}
            for item in self.profiles.values() if (item.tenant_id,item.project_id)==(context.tenant_id,context.project_id)]

    def _audit(self,context,action,model_id,details):
        self.db.execute("""INSERT INTO governance_audit_events
            (id,tenant_id,project_id,actor_user_id,action,resource_id,details_json,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",(new_id("audit"),context.tenant_id,context.project_id,context.user_id,
            action,model_id,self.db.encode(details),self.db.current_time().isoformat()))

    def register(self,payload:ModelRegistration,context):
        model_id = new_id("model")
        with authorized_write(self.db, context, Permission.MODEL_MANAGE) as context:
            profile = self._profile(payload.profile_id,context.tenant_id,context.project_id)
            self.db.execute("""INSERT INTO model_deployments
                (id,tenant_id,project_id,name,provider,model,endpoint_region,status,capabilities_json,pricing_json,
                 created_at,context_window_tokens,runtime_binding_json,version)
                VALUES(?,?,?,?,?,?,?,'healthy',?,?,?,?,?,1)""",
                (model_id,context.tenant_id,context.project_id,payload.name or profile.name,profile.api_style,profile.model,
                 "operator-approved",self.db.encode(profile.capabilities),self.db.encode({
                     "input_per_million":str(profile.input_per_million),"output_per_million":str(profile.output_per_million)}),
                 self.db.current_time().isoformat(),profile.context_window_tokens,self.db.encode(profile.binding())))
            self._audit(context,"model.registered",model_id,{"binding":profile.binding(),"reason":payload.reason})
            return self.db.fetch_one("SELECT * FROM model_deployments WHERE id=?",(model_id,))

    def update_status(self,model_id,payload:ModelStatusUpdate,context):
        with authorized_write(self.db, context, Permission.MODEL_MANAGE) as context:
            row = self.db.fetch_one("SELECT * FROM model_deployments WHERE id=? AND tenant_id=? AND project_id=?",
                (model_id,context.tenant_id,context.project_id))
            if not row:
                raise NotFoundError("Model deployment not found")
            changed = self.db.execute_count("UPDATE model_deployments SET status=?,version=version+1 WHERE id=? AND version=?",
                ("healthy" if payload.enabled else "disabled",model_id,payload.version))
            if changed != 1:
                raise ConflictError("Model status changed; reload before updating")
            self._audit(context,"model.status.updated",model_id,{"enabled":payload.enabled,"reason":payload.reason,"version":payload.version+1})
            return self.db.fetch_one("SELECT * FROM model_deployments WHERE id=?",(model_id,))

    def validate_plan(self,plan):
        snapshot = plan.get("model_snapshot") or {}
        row = self.db.fetch_one("SELECT * FROM model_deployments WHERE id=? AND tenant_id=? AND project_id=?",
            (plan.get("model_deployment_revision_id"),snapshot.get("tenant_id"),snapshot.get("project_id")))
        if not row or row["status"] != "healthy":
            raise ConflictError("The bound model deployment is disabled or unavailable")
        binding = snapshot.get("runtime_binding")
        if not binding:
            if self.production():
                raise ConflictError("Production requires a registered immutable model profile; republish this Agent")
            if not self.allow_test_override and snapshot.get("model") != self.fallback_gateway.identity().get("model"):
                raise ConflictError("Legacy model differs from runtime configuration; register a model profile and republish")
            return None
        profile = self._profile(binding.get("profile_id"),row["tenant_id"],row["project_id"])
        if binding != profile.binding() or binding != row.get("runtime_binding"):
            raise ConflictError("Model profile changed; restore its approved configuration or publish a new version")
        if snapshot.get("model") != profile.model or snapshot.get("provider") != profile.api_style:
            raise ConflictError("Model snapshot does not match its runtime binding")
        expected_price = {"input_per_million":str(profile.input_per_million),"output_per_million":str(profile.output_per_million)}
        if snapshot.get("pricing") != expected_price:
            raise ConflictError("Model pricing does not match its immutable profile")
        return profile

    def gateway(self,plan):
        profile = self.validate_plan(plan)
        return profile.gateway() if profile else self.fallback_gateway

    def coding_model(self,plan,gateway,fallback):
        profile = self.validate_plan(plan)
        if profile is None:
            return fallback
        from packages.runtime.coding_model import create_coding_chat_model
        key = profile.digest()
        if key not in self._coding_models:
            self._coding_models[key] = create_coding_chat_model(gateway)
        return self._coding_models[key]

    async def close(self):
        from packages.runtime.coding_model import close_coding_chat_model
        for model in self._coding_models.values():
            await close_coding_chat_model(model)
        self._coding_models.clear()
