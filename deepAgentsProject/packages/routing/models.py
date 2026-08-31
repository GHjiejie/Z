from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from packages.releases.models import ReleaseModel

from packages.coding.models import WorkspaceBinding


PrimaryIntent = Literal["coding", "release", "knowledge", "general", "ambiguous"]
RoutingMode = Literal["active", "shadow", "disabled"]
RoutingDecisionStatus = Literal[
    "READY",
    "NEEDS_WORKSPACE",
    "NEEDS_CONFIRMATION",
    "FALLBACK",
]


class IntentClassification(BaseModel):
    taxonomy_version: str = "1.0"
    primary_intent: PrimaryIntent
    secondary_intents: List[PrimaryIntent] = Field(default_factory=list)
    subtype: str = Field(default="unspecified", min_length=1, max_length=80)
    confidence: float = Field(ge=0, le=1)
    requires_repository: bool = False
    requires_knowledge: bool = False
    risk_hint: Literal["low", "medium", "high"] = "low"
    summary: str = Field(min_length=1, max_length=240)
    source: Literal["rules", "model", "fallback"] = "rules"

    @field_validator("secondary_intents")
    @classmethod
    def unique_secondary_intents(
        cls, value: List[PrimaryIntent]
    ) -> List[PrimaryIntent]:
        return list(dict.fromkeys(value))


class IntentRoutingResolve(BaseModel):
    input: str = Field(min_length=1, max_length=100_000)
    preferred_deployment_id: Optional[str] = Field(default=None, min_length=1)
    workspace: Optional[WorkspaceBinding] = None

    @field_validator("input")
    @classmethod
    def input_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input cannot be blank")
        return value


class RoutedRunCreate(BaseModel):
    decision_id: str = Field(min_length=1)
    input: str = Field(min_length=1, max_length=100_000)
    title: Optional[str] = Field(default=None, max_length=160)
    workspace: Optional[WorkspaceBinding] = None
    confirmed: bool = False
    override_deployment_id: Optional[str] = Field(default=None, min_length=1)

    @field_validator("input")
    @classmethod
    def routed_input_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input cannot be blank")
        return value


class RoutingProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    mode: RoutingMode = "active"
    auto_route_threshold: float = Field(default=0.80, ge=0, le=1)
    confirmation_threshold: float = Field(default=0.55, ge=0, le=1)
    decision_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    target_deployments: Dict[str, Optional[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def thresholds_are_ordered(self) -> "RoutingProfileUpdate":
        if self.confirmation_threshold > self.auto_route_threshold:
            raise ValueError(
                "confirmation_threshold cannot exceed auto_route_threshold"
            )
        unknown = set(self.target_deployments) - {
            "coding",
            "release",
            "knowledge",
            "general",
        }
        if unknown:
            raise ValueError(
                "Unknown routing targets: " + ", ".join(sorted(unknown))
            )
        return self


class RoutingChangeCreate(ReleaseModel):
    expected_router_revision_id: str = Field(min_length=1, max_length=100)
    action: Literal["update", "rollback"] = "update"
    profile: RoutingProfileUpdate | None = None
    rollback_revision_id: str | None = Field(default=None, min_length=1, max_length=100)
    reason: str = Field(min_length=5, max_length=1000)
    expires_in_seconds: int = Field(default=3600, ge=60, le=86400)

    @model_validator(mode="after")
    def exact_candidate(self):
        if self.action == "update" and (self.profile is None or self.rollback_revision_id is not None):
            raise ValueError("Update requires a profile and no rollback revision")
        if self.action == "rollback" and (self.rollback_revision_id is None or self.profile is not None):
            raise ValueError("Rollback requires a revision and no replacement profile")
        return self
