from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Environment = Literal["development", "staging", "production"]


class ReleaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("reason", check_fields=False)
    @classmethod
    def meaningful_reason(cls, value):
        value = value.strip()
        if len(value) < 5:
            raise ValueError("Provide a meaningful reason of at least five characters")
        return value


class EnvironmentGrantUpdate(ReleaseModel):
    user_id: str = Field(min_length=1, max_length=100)
    environment: Environment
    can_deploy: bool
    can_approve: bool
    version: int = Field(ge=0)
    reason: str = Field(min_length=5, max_length=500)


class ReleaseCreate(ReleaseModel):
    agent_revision_id: str = Field(min_length=1, max_length=100)
    action: Literal["promote", "rollback"] = "promote"
    rollback_deployment_id: str | None = Field(default=None, min_length=1, max_length=100)
    expected_channel_version: int = Field(ge=0)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    reason: str = Field(min_length=5, max_length=1000)
    expires_in_seconds: int = Field(default=3600, ge=60, le=86400)

    @model_validator(mode="after")
    def rollback_target(self):
        if (self.action == "rollback") != bool(self.rollback_deployment_id):
            raise ValueError("Only rollback requests require a previous deployment ID")
        return self


class ReleaseDecision(ReleaseModel):
    version: int = Field(ge=1)
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=5, max_length=1000)


class ReleaseCancel(ReleaseModel):
    version: int = Field(ge=1)
    reason: str = Field(min_length=5, max_length=1000)
