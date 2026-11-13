"""Strict request and response contracts for the user management API."""

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    StrictStr,
    field_validator,
    model_validator,
)

Role = Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")]
Status = Literal["active", "disabled"]


def normalized_username(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("用户名必须是字符串")  # noqa: TRY004 -- Pydantic requires ValueError
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{3,64}", value):
        raise ValueError("用户名须为 3～64 位 ASCII 字母、数字、点、下划线或连字符")
    return value


def normalized_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("展示名称必须是字符串")  # noqa: TRY004 -- Pydantic requires ValueError
    value = value.strip()
    if not 1 <= len(value) <= 100:
        raise ValueError("展示名称去除首尾空格后须为 1～100 字符")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("展示名称必须是有效的 Unicode 文本") from None
    return value


def normalized_email(value: object) -> object:
    if isinstance(value, str):
        value = value.strip()
        if len(value) > 254:
            raise ValueError("邮箱最长为 254 字符")
    return value


def normalized_description(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("角色说明必须是字符串")  # noqa: TRY004 -- Pydantic requires ValueError
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("角色说明必须是有效的 Unicode 文本") from None
    return value


def password_text(value: object) -> str:
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    if not isinstance(value, str):
        raise ValueError("密码必须是字符串")  # noqa: TRY004 -- Pydantic requires ValueError
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("密码必须是有效的 Unicode 文本") from None
    return value


Username = Annotated[str, BeforeValidator(normalized_username)]
DisplayName = Annotated[str, BeforeValidator(normalized_name)]
ContactEmail = Annotated[EmailStr | None, BeforeValidator(normalized_email)]
Password = Annotated[SecretStr, Field(min_length=1), BeforeValidator(password_text)]
Description = Annotated[
    str, Field(max_length=500), BeforeValidator(normalized_description)
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(StrictModel):
    username: Username
    password: Password


class UserCreate(StrictModel):
    username: Username
    display_name: DisplayName
    email: ContactEmail = None
    password: Password
    roles: list[Role] = Field(default_factory=lambda: ["member"], min_length=1)

    @field_validator("roles")
    @classmethod
    def unique_roles(cls, roles: list[Role]) -> list[Role]:
        return sorted(set(roles))


class UserUpdate(StrictModel):
    display_name: DisplayName | None = None
    email: ContactEmail = None

    @model_validator(mode="after")
    def valid_update(self) -> "UserUpdate":
        if not self.model_fields_set:
            raise ValueError("至少提交一个可更新字段")
        if "display_name" in self.model_fields_set and self.display_name is None:
            raise ValueError("展示名称不能为 null")
        return self


class PasswordChange(StrictModel):
    current_password: Password
    new_password: Password


class PasswordReset(StrictModel):
    new_password: Password


class StatusUpdate(StrictModel):
    status: Status


class RolesUpdate(StrictModel):
    roles: list[Role] = Field(min_length=1)

    @field_validator("roles")
    @classmethod
    def unique_roles(cls, roles: list[Role]) -> list[Role]:
        return sorted(set(roles))


class UserQuery(StrictModel):
    page: int = Field(default=1, ge=1, le=2_147_483_647)
    page_size: int = Field(default=20, ge=1, le=100)
    q: StrictStr | None = Field(default=None, max_length=100)
    status: Status | None = None
    role: Role | None = None
    sort_by: Literal[
        "username", "display_name", "created_at", "updated_at", "last_login_at"
    ] = "created_at"
    sort_order: Literal["asc", "desc"] = "desc"


class User(StrictModel):
    id: str
    username: str
    display_name: str
    email: str | None
    roles: list[Role]
    status: Status
    must_change_password: bool
    version: int
    last_login_at: datetime | None
    created_at: datetime
    updated_at: datetime


class Envelope[T](BaseModel):
    data: T
    request_id: str


class UserPage(StrictModel):
    items: list[User]
    total: int
    page: int
    page_size: int


class LoginResult(StrictModel):
    session_token: str
    token_type: Literal["Bearer"]
    expires_in: int
    auth_scope: Literal["full"]
    user: User


class OwnPermissions(StrictModel):
    user_id: str
    roles: list[Role]
    permissions: list[str]
    role_details: list["RoleOption"]


class RoleDefinition(StrictModel):
    code: Role
    display_name: str
    description: str
    permissions: list[str]
    built_in: bool
    user_count: int
    version: int
    created_at: datetime
    updated_at: datetime


class RoleOption(StrictModel):
    code: Role
    display_name: str
    description: str


class PermissionDefinition(StrictModel):
    code: str
    display_name: str
    resource: str


class RoleCreate(StrictModel):
    code: Role
    display_name: DisplayName
    description: Description = ""
    permissions: list[StrictStr] = Field(default_factory=list, max_length=128)

    @field_validator("permissions")
    @classmethod
    def unique_permissions(cls, permissions: list[str]) -> list[str]:
        return sorted(set(permissions))


class RoleUpdate(StrictModel):
    display_name: DisplayName | None = None
    description: Description | None = None
    permissions: list[StrictStr] | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def valid_update(self) -> "RoleUpdate":
        if not self.model_fields_set or any(
            getattr(self, field) is None for field in self.model_fields_set
        ):
            raise ValueError("至少提交一个非空的角色字段，不允许 null")
        if self.permissions is not None:
            self.permissions = sorted(set(self.permissions))
        return self


class ErrorDetail(StrictModel):
    field: str
    message: str


class ErrorValue(StrictModel):
    code: str
    message: str
    details: list[ErrorDetail]


class ErrorResponse(StrictModel):
    error: ErrorValue
    request_id: str
