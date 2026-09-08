from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
UserStatus = Literal["ACTIVE", "INACTIVE"]


def _validate_username(value: str) -> str:
    normalized = value.strip().lower()
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError(
            "username may contain only letters, numbers, dots, underscores, and hyphens"
        )
    return normalized


def _validate_password(value: str) -> str:
    if len(value) < 8:
        raise ValueError("password must contain at least 8 characters")
    if len(value) > 256:
        raise ValueError("password must contain at most 256 characters")
    checks = (
        any(character.islower() for character in value),
        any(character.isupper() for character in value),
        any(character.isdigit() for character in value),
        any(not character.isalnum() for character in value),
    )
    if not all(checks):
        raise ValueError(
            "password must include lowercase, uppercase, number, and symbol characters"
        )
    return value


def _validate_roles(values: List[str]) -> List[str]:
    normalized = list(
        dict.fromkeys(value.strip().lower() for value in values if value.strip())
    )
    if not normalized:
        return ["member"]
    if any(
        len(value) > 32 or not USERNAME_PATTERN.fullmatch(value)
        for value in normalized
    ):
        raise ValueError(
            "roles may contain only letters, numbers, dots, underscores, and hyphens"
        )
    return normalized


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("username")
    @classmethod
    def username_is_normalized(cls, value: str) -> str:
        return value.strip().lower()


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=256)
    tenant_id: str = Field(default="tenant_demo", min_length=1, max_length=100)
    project_id: str = Field(default="project_atlas", min_length=1, max_length=100)
    environment_id: str = Field(
        default="env_development", min_length=1, max_length=100
    )
    roles: List[str] = Field(default_factory=lambda: ["member"], max_length=20)
    is_super_admin: bool = False

    @field_validator("username")
    @classmethod
    def username_is_valid(cls, value: str) -> str:
        return _validate_username(value)

    @field_validator("display_name", "tenant_id", "project_id", "environment_id")
    @classmethod
    def text_is_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value cannot be blank")
        return normalized

    @field_validator("password")
    @classmethod
    def password_is_strong(cls, value: str) -> str:
        return _validate_password(value)

    @field_validator("roles")
    @classmethod
    def roles_are_valid(cls, values: List[str]) -> List[str]:
        return _validate_roles(values)


class UserUpdate(BaseModel):
    version: int = Field(ge=1)
    username: Optional[str] = Field(default=None, min_length=3, max_length=64)
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    tenant_id: Optional[str] = Field(default=None, min_length=1, max_length=100)
    project_id: Optional[str] = Field(default=None, min_length=1, max_length=100)
    environment_id: Optional[str] = Field(default=None, min_length=1, max_length=100)
    roles: Optional[List[str]] = Field(default=None, max_length=20)
    is_super_admin: Optional[bool] = None
    status: Optional[Literal["ACTIVE"]] = None

    @model_validator(mode="after")
    def supplied_fields_must_not_be_null(self) -> "UserUpdate":
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("User fields may be omitted, but cannot be null")
        return self

    @field_validator("username")
    @classmethod
    def username_is_valid(cls, value: Optional[str]) -> Optional[str]:
        return _validate_username(value) if value is not None else value

    @field_validator("display_name", "tenant_id", "project_id", "environment_id")
    @classmethod
    def text_is_not_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("value cannot be blank")
        return normalized

    @field_validator("roles")
    @classmethod
    def roles_are_valid(cls, values: Optional[List[str]]) -> Optional[List[str]]:
        return _validate_roles(values) if values is not None else values


class PasswordResetRequest(BaseModel):
    password: str = Field(min_length=8, max_length=256)
    version: int = Field(ge=1)

    @field_validator("password")
    @classmethod
    def password_is_strong(cls, value: str) -> str:
        return _validate_password(value)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)
    version: int = Field(ge=1)

    @field_validator("new_password")
    @classmethod
    def password_is_strong(cls, value: str) -> str:
        return _validate_password(value)

    @model_validator(mode="after")
    def password_is_new(self) -> "PasswordChangeRequest":
        if self.current_password == self.new_password:
            raise ValueError("new password must be different from the current password")
        return self


class UserDeleteRequest(BaseModel):
    version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def reason_is_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 3:
            raise ValueError("deactivation reason must contain at least 3 characters")
        return normalized


class UserResponse(BaseModel):
    id: str
    username: str
    display_name: str
    tenant_id: str
    project_id: str
    environment_id: str
    roles: List[str]
    is_super_admin: bool
    status: UserStatus
    version: int
    last_login_at: Optional[str] = None
    password_changed_at: Optional[str] = None
    password_expires_at: Optional[str] = None
    must_change_password: bool
    failed_login_count: int
    locked_until: Optional[str] = None
    deleted_at: Optional[str] = None
    deleted_by: Optional[str] = None
    deletion_reason: Optional[str] = None
    created_at: str
    updated_at: str


class UserListResponse(BaseModel):
    items: List[UserResponse]
    page: int
    page_size: int
    total: int
    pages: int


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: str
    expires_in: int
    user: UserResponse


class AuthSessionResponse(BaseModel):
    id: str
    user_id: str
    expires_at: str
    revoked_at: Optional[str] = None
    created_at: str
    last_seen_at: str
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    status: Literal["ACTIVE", "REVOKED", "EXPIRED"]
    current: bool = False


class AuthSessionListResponse(BaseModel):
    items: List[AuthSessionResponse]


class SessionMutationResponse(BaseModel):
    ok: bool = True
    revoked_count: int = 0
    revoked_current: bool = False


class OkResponse(BaseModel):
    ok: bool = True


class AuthAuditEventResponse(BaseModel):
    id: str
    actor_user_id: Optional[str] = None
    target_user_id: Optional[str] = None
    tenant_id: Optional[str] = None
    project_id: Optional[str] = None
    action: str
    outcome: str
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)
    created_at: str


class AuthAuditListResponse(BaseModel):
    items: List[AuthAuditEventResponse]
    page: int
    page_size: int
    total: int
    pages: int


class AuthenticatedPrincipal(BaseModel):
    session_id: str
    user_id: str
    username: str
    display_name: str
    tenant_id: str
    project_id: str
    environment_id: str
    roles: List[str]
    is_super_admin: bool
    expires_at: str
    must_change_password: bool
    password_expires_at: Optional[str] = None

    @property
    def is_tenant_admin(self) -> bool:
        return "tenant_admin" in self.roles


PasswordUpdate = PasswordResetRequest
