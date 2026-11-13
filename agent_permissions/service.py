"""Transactional user operations shared by HTTP and future Agent adapters."""

import json
import logging
import math
import re
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

import psycopg
from pydantic import ValidationError

from agent_permissions.config import Settings
from agent_permissions.errors import ApiError, invalid_field
from agent_permissions.models import (
    PasswordChange,
    PasswordReset,
    RoleCreate,
    RolesUpdate,
    RoleUpdate,
    StatusUpdate,
    User,
    UserCreate,
    UserQuery,
    UserUpdate,
)
from agent_permissions.permissions import permissions_for, roles_for
from agent_permissions.rbac import (
    assign_permissions,
    assign_roles,
    require_permissions,
    require_roles,
    role_permissions,
    role_record,
)
from agent_permissions.security import Passwords, token_digest
from agent_permissions.storage import Connection, DatabaseBackend

logger = logging.getLogger(__name__)
Clock = Callable[[], datetime]
Operation = Literal[
    "user.self.update",
    "user.self.password.change",
    "user.update",
    "user.delete",
    "user.status.update",
    "user.password.reset",
    "user.roles.assign",
]


def utc_now() -> datetime:
    return datetime.now(UTC)


def timestamp(now: datetime) -> str:
    return now.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Authority:
    """A reference to a server-side session, not a saved role snapshot."""

    token_digest: str


@dataclass
class RequestInfo:
    request_id: str
    source: str
    actor_user_id: str | None = None


def public_user(connection: Connection, row: Mapping[str, Any]) -> dict[str, Any]:
    fields = {name: row[name] for name in User.model_fields if name != "roles"}
    fields["roles"] = roles_for(connection, row["id"])
    return User.model_validate(fields).model_dump(mode="json")


class RateLimiter:
    """Persistent fixed-window buckets, shared by local service workers."""

    def __init__(self, database: DatabaseBackend, clock: Clock) -> None:
        self.database = database
        self.clock = clock

    def consume(self, buckets: list[tuple[str, int, int]]) -> None:
        now = self.clock().timestamp()
        retry_after = 0
        with self.database.transaction(write=True) as connection:
            connection.execute("DELETE FROM rate_limits WHERE expires_at<=?", (now,))
            updates = []
            for key, limit, seconds in buckets:
                key = token_digest(key)
                row = connection.execute(
                    "SELECT * FROM rate_limits WHERE bucket_key=?", (key,)
                ).fetchone()
                if row and row["attempts"] >= limit:
                    retry_after = max(retry_after, math.ceil(row["expires_at"] - now))
                updates.append(
                    (
                        key,
                        row["attempts"] + 1 if row else 1,
                        row["expires_at"] if row else now + seconds,
                    )
                )
            if not retry_after:
                connection.executemany(
                    """INSERT INTO rate_limits(bucket_key,attempts,expires_at)
                    VALUES(?,?,?) ON CONFLICT(bucket_key) DO UPDATE
                    SET attempts=excluded.attempts,expires_at=excluded.expires_at""",
                    updates,
                )
        if retry_after:
            raise ApiError(
                429,
                "RATE_LIMITED",
                "请求过于频繁，请稍后重试",
                headers={"Retry-After": str(max(1, retry_after))},
            )


class UserService:
    def __init__(
        self, database: DatabaseBackend, settings: Settings, *, clock: Clock = utc_now
    ) -> None:
        self.database = database
        self.settings = settings
        self.clock = clock
        self.passwords = Passwords()
        self.limiter = RateLimiter(database, clock)

    def _audit(
        self,
        connection: Connection,
        info: RequestInfo,
        action: str,
        target: str | None,
        *,
        outcome: str = "success",
        reason: str | None = None,
        changes: dict | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO audit_events
            (id,actor_user_id,target_user_id,action,outcome,reason_code,
             request_id,source,changes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                f"evt_{uuid4().hex}",
                info.actor_user_id,
                target,
                action,
                outcome,
                reason,
                info.request_id,
                info.source,
                json.dumps(changes or {}, ensure_ascii=False),
                timestamp(self.clock()),
            ),
        )

    def record_failure(
        self,
        info: RequestInfo,
        action: str,
        target: str | None,
        code: str,
        *,
        role_code: str | None = None,
    ) -> None:
        try:
            with self.database.transaction(write=True) as connection:
                self._audit(
                    connection,
                    info,
                    action,
                    target,
                    outcome="failure",
                    reason=code,
                    changes={"role_code": role_code} if role_code else None,
                )
        except (sqlite3.Error, psycopg.Error):
            # The failed request remains failed; never log its body or token.
            logger.error("Failure audit unavailable request_id=%s", info.request_id)

    def _authorize(
        self,
        connection: Connection,
        authority: Authority,
        info: RequestInfo,
        permission: str | None,
    ) -> Mapping[str, Any]:
        session = connection.execute(
            "SELECT * FROM sessions WHERE token_digest=?", (authority.token_digest,)
        ).fetchone()
        if (
            not session
            or session["revoked_at"]
            or session["expires_at"] <= self.clock().timestamp()
        ):
            raise ApiError(401, "UNAUTHENTICATED", "请重新登录")
        user = connection.execute(
            "SELECT * FROM users WHERE id=?", (session["user_id"],)
        ).fetchone()
        if not user or user["deleted_at"] or user["status"] != "active":
            raise ApiError(401, "UNAUTHENTICATED", "请重新登录")
        info.actor_user_id = user["id"]
        if permission and permission not in permissions_for(connection, user["id"]):
            raise ApiError(403, "PERMISSION_DENIED", "没有执行该操作的权限")
        return user

    @staticmethod
    def _target(connection: Connection, user_id: str) -> Mapping[str, Any]:
        user = connection.execute(
            "SELECT * FROM users WHERE id=? AND deleted_at IS NULL", (user_id,)
        ).fetchone()
        if not user:
            raise ApiError(404, "USER_NOT_FOUND", "用户不存在或已删除")
        return user

    @staticmethod
    def _version(user: Mapping[str, Any], expected: str | None) -> None:
        if expected is None:
            raise ApiError(428, "PRECONDITION_REQUIRED", "请提供记录版本 If-Match")
        if not re.fullmatch(r'"v[1-9][0-9]{0,18}"', expected):
            raise invalid_field("If-Match", '版本须为单个带引号的标签，例如 "v3"')
        if expected != f'"v{user["version"]}"':
            raise ApiError(412, "VERSION_CONFLICT", "记录已变化，请重新读取后操作")

    def _insert_user(
        self,
        connection: Connection,
        payload: UserCreate,
        info: RequestInfo,
    ) -> dict:
        value = payload.password.get_secret_value()
        now = timestamp(self.clock())
        user_id = f"usr_{uuid4().hex}"
        try:
            connection.execute(
                """INSERT INTO users
                (id,username,display_name,email,password_hash,status,
                 must_change_password,version,created_at,updated_at)
                VALUES(?,?,?,?,?,'active',0,1,?,?)""",
                (
                    user_id,
                    payload.username,
                    payload.display_name,
                    str(payload.email) if payload.email is not None else None,
                    self.passwords.hash(value),
                    now,
                    now,
                ),
            )
        except (sqlite3.IntegrityError, psycopg.errors.UniqueViolation) as error:
            username_conflict = (
                isinstance(error, sqlite3.IntegrityError)
                and "users.username" in str(error)
            ) or (
                isinstance(error, psycopg.errors.UniqueViolation)
                and error.diag.constraint_name == "users_username_key"
            )
            if not username_conflict:
                raise
            raise ApiError(
                409,
                "USERNAME_ALREADY_EXISTS",
                "用户名已存在",
                details=[{"field": "username", "message": "请使用其他用户名"}],
            ) from None
        assign_roles(connection, user_id, payload.roles)
        self._audit(
            connection, info, "user.create", user_id, changes={"roles": payload.roles}
        )
        return public_user(connection, self._target(connection, user_id))

    def bootstrap_admin(self, payload: UserCreate) -> dict:
        """One-time local initialization; never exposed as a public route."""
        with self.database.transaction(write=True) as connection:
            if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise ApiError(
                    409, "BOOTSTRAP_ALREADY_COMPLETED", "已有用户，不能重复初始化"
                )
            payload = payload.model_copy(update={"roles": ["admin"]})
            return self._insert_user(
                connection, payload, RequestInfo(f"init_{uuid4().hex}", "local-cli")
            )

    def login(self, username: str, password: str, info: RequestInfo) -> dict:
        self.limiter.consume(
            [
                (
                    f"login-account:{username}",
                    self.settings.login_account_limit,
                    self.settings.account_window_seconds,
                ),
                (
                    f"login-source:{info.source}",
                    self.settings.login_source_limit,
                    self.settings.source_window_seconds,
                ),
            ]
        )
        with self.database.transaction(write=True) as connection:
            user = connection.execute(
                "SELECT * FROM users WHERE username=?", (username,)
            ).fetchone()
            # Perform the expensive verification for absent accounts as well.
            encoded = user["password_hash"] if user else self.passwords.dummy_hash
            valid_password = self.passwords.verify(encoded, password)
            if (
                not valid_password
                or not user
                or user["deleted_at"]
                or user["status"] != "active"
            ):
                raise ApiError(401, "INVALID_CREDENTIALS", "用户名或密码错误")
            scope = "full"
            seconds = self.settings.session_seconds
            now = self.clock()
            token = secrets.token_urlsafe(32)
            connection.execute(
                """INSERT INTO sessions
                (token_digest,user_id,scope,expires_at,created_at) VALUES(?,?,?,?,?)""",
                (
                    token_digest(token),
                    user["id"],
                    scope,
                    now.timestamp() + seconds,
                    timestamp(now),
                ),
            )
            connection.execute(
                """UPDATE users SET must_change_password=0,
                last_login_at=?,updated_at=?,version=version+1
                WHERE id=?""",
                (timestamp(now), timestamp(now), user["id"]),
            )
            info.actor_user_id = user["id"]
            self._audit(connection, info, "auth.login", user["id"])
            return {
                "session_token": token,
                "token_type": "Bearer",
                "expires_in": seconds,
                "auth_scope": scope,
                "user": public_user(connection, self._target(connection, user["id"])),
            }

    def logout(self, authority: Authority, info: RequestInfo) -> None:
        with self.database.transaction(write=True) as connection:
            user = self._authorize(connection, authority, info, None)
            connection.execute(
                "UPDATE sessions SET revoked_at=? WHERE token_digest=?",
                (timestamp(self.clock()), authority.token_digest),
            )
            self._audit(connection, info, "auth.logout", user["id"])

    def get_me(self, authority: Authority, info: RequestInfo) -> dict:
        with self.database.transaction() as connection:
            return public_user(
                connection,
                self._authorize(
                    connection,
                    authority,
                    info,
                    "user.self.read",
                ),
            )

    def own_permissions(self, authority: Authority, info: RequestInfo) -> dict:
        with self.database.transaction() as connection:
            user = self._authorize(connection, authority, info, "user.self.read")
            roles = roles_for(connection, user["id"])
            return {
                "user_id": user["id"],
                "roles": roles,
                "permissions": permissions_for(connection, user["id"]),
                "role_details": [
                    dict(row)
                    for row in connection.execute(
                        """SELECT r.code,r.display_name,r.description FROM roles r
                    JOIN user_roles ur ON ur.role_code=r.code WHERE ur.user_id=? ORDER BY r.code""",
                        (user["id"],),
                    )
                ],
            }

    def roles(self, authority: Authority, info: RequestInfo) -> list[dict]:
        with self.database.transaction() as connection:
            self._authorize(connection, authority, info, "role.read")
            return [
                role_record(connection, row[0])
                for row in connection.execute(
                    "SELECT code FROM roles ORDER BY built_in DESC,code"
                )
            ]

    def get_user(self, authority: Authority, user_id: str, info: RequestInfo) -> dict:
        with self.database.transaction() as connection:
            self._authorize(connection, authority, info, "user.read")
            return public_user(connection, self._target(connection, user_id))

    def create_user(
        self, authority: Authority, payload: UserCreate, info: RequestInfo
    ) -> dict:
        with self.database.transaction(write=True) as connection:
            actor = self._authorize(connection, authority, info, "user.create")
            require_roles(connection, payload.roles)
            if payload.roles != ["member"]:
                self._authorize(connection, authority, info, "user.roles.assign")
            self._check_grants(
                connection,
                actor,
                [
                    permission
                    for code in payload.roles
                    for permission in role_permissions(connection, code)
                ],
            )
            return self._insert_user(connection, payload, info)

    def list_users(
        self, authority: Authority, query: UserQuery, info: RequestInfo
    ) -> dict:
        conditions = ["u.deleted_at IS NULL"]
        params: list[Any] = []
        if query.status:
            conditions.append("u.status=?")
            params.append(query.status)
        if query.role:
            conditions.append(
                "EXISTS(SELECT 1 FROM user_roles ur WHERE ur.user_id=u.id AND ur.role_code=?)"
            )
            params.append(query.role)
        if query.q and query.q.strip():
            conditions.append(
                """(app_contains(u.username,?)>0 OR
                app_contains(u.display_name,?)>0 OR app_contains(u.email,?)>0)"""
            )
            params.extend([query.q.strip().casefold()] * 3)
        where = " AND ".join(conditions)
        # Only validated, explicitly mapped identifiers enter SQL syntax.
        column = {
            "username": "u.username",
            "display_name": "u.display_name",
            "created_at": "u.created_at",
            "updated_at": "u.updated_at",
            "last_login_at": "u.last_login_at",
        }[query.sort_by]
        direction = {"asc": "ASC", "desc": "DESC"}[query.sort_order]
        with self.database.transaction() as connection:
            self._authorize(connection, authority, info, "user.read")
            if query.role:
                require_roles(connection, [query.role])
            total = connection.execute(
                f"SELECT count(*) FROM users u WHERE {where}", params
            ).fetchone()[0]
            rows = connection.execute(
                f"""SELECT u.* FROM users u WHERE {where}
                ORDER BY ({column} IS NULL) ASC,{column} {direction},u.id ASC
                LIMIT ? OFFSET ?""",
                [*params, query.page_size, (query.page - 1) * query.page_size],
            ).fetchall()
            return {
                "items": [public_user(connection, row) for row in rows],
                "total": total,
                "page": query.page,
                "page_size": query.page_size,
            }

    def _password_limit(
        self, authority: Authority, operation: Operation, info: RequestInfo
    ) -> None:
        with self.database.transaction() as connection:
            actor = self._authorize(
                connection,
                authority,
                info,
                operation,
            )
            user_id = actor["id"]
        # Persist attempts outside the business transaction so failures count.
        self.limiter.consume(
            [
                (
                    f"{operation}:{user_id}",
                    self.settings.password_limit,
                    self.settings.password_window_seconds,
                )
            ]
        )

    @staticmethod
    def _guard_admin(
        connection: Connection,
        actor: Mapping[str, Any],
        target: Mapping[str, Any],
        changes: dict,
        operation: Operation,
    ) -> None:
        removes_admin = "roles" in changes and "admin" not in changes["roles"]
        disables = changes.get("status") == "disabled" or "deleted_at" in changes
        if actor["id"] == target["id"] and (
            removes_admin or disables or operation == "user.password.reset"
        ):
            raise ApiError(
                409, "SELF_OPERATION_NOT_ALLOWED", "不能对自己执行此管理操作"
            )
        if (
            (removes_admin or disables)
            and target["status"] == "active"
            and "admin" in roles_for(connection, target["id"])
        ):
            remaining = connection.execute(
                """SELECT count(*) FROM users WHERE id<>? AND status='active'
                AND deleted_at IS NULL
                AND EXISTS(SELECT 1 FROM user_roles ur WHERE ur.user_id=users.id AND ur.role_code='admin')""",
                (target["id"],),
            ).fetchone()[0]
            if not remaining:
                raise ApiError(
                    409, "LAST_ADMIN_PROTECTED", "系统必须至少保留一名启用的管理员"
                )

    def mutate_user(
        self,
        authority: Authority,
        operation: Operation,
        user_id: str | None,
        expected: str | None,
        payload: dict,
        info: RequestInfo,
    ) -> dict:
        model = {
            "user.self.update": UserUpdate,
            "user.update": UserUpdate,
            "user.status.update": StatusUpdate,
            "user.roles.assign": RolesUpdate,
            "user.password.reset": PasswordReset,
            "user.self.password.change": PasswordChange,
        }.get(operation)
        if model is not None:
            try:
                payload = model.model_validate(payload).model_dump(exclude_unset=True)
            except ValidationError:
                raise invalid_field("body", "操作参数不符合接口字段规则") from None
        elif operation != "user.delete" or payload:
            raise invalid_field("operation", "未知用户操作或无效请求体")
        password_operation = operation in {
            "user.self.password.change",
            "user.password.reset",
        }
        if password_operation:
            self._password_limit(authority, operation, info)
        with self.database.transaction(write=True) as connection:
            actor = self._authorize(
                connection,
                authority,
                info,
                operation,
            )
            # Self operations can never be redirected to a caller-supplied ID.
            target_id = actor["id"] if operation.startswith("user.self.") else user_id
            target = self._target(connection, target_id)
            if actor["id"] != target["id"]:
                self._check_grants(
                    connection, actor, permissions_for(connection, target["id"])
                )
            self._version(target, expected)
            now = timestamp(self.clock())
            changes: dict[str, Any] = {}
            if operation in {"user.update", "user.self.update"}:
                # Enforce the field boundary for non-HTTP callers as well.
                if not payload or not set(payload) <= {"display_name", "email"}:
                    raise invalid_field("body", "只允许更新展示名称和邮箱")
                changes = {
                    key: str(value) if value is not None else None
                    for key, value in payload.items()
                    if target[key] != value
                }
            elif operation == "user.status.update":
                changes = {"status": payload["status"]}
            elif operation == "user.roles.assign":
                require_roles(connection, payload["roles"])
                self._check_grants(
                    connection,
                    actor,
                    [
                        permission
                        for code in payload["roles"]
                        for permission in role_permissions(connection, code)
                    ],
                )
                changes = {"roles": sorted(set(payload["roles"]))}
            elif operation == "user.delete":
                changes = {"deleted_at": now, "status": "disabled"}
            elif password_operation:
                if operation == "user.password.reset" and actor["id"] == target["id"]:
                    raise ApiError(
                        409, "SELF_OPERATION_NOT_ALLOWED", "请通过本人改密接口修改密码"
                    )
                if operation == "user.self.password.change":
                    current = payload["current_password"].get_secret_value()
                    if not self.passwords.verify(target["password_hash"], current):
                        raise ApiError(
                            400, "CURRENT_PASSWORD_INCORRECT", "当前密码不正确"
                        )
                value = payload["new_password"].get_secret_value()
                changes = {
                    "password_hash": self.passwords.hash(value),
                    "must_change_password": 0,
                }
            else:
                raise invalid_field("operation", "未知用户操作")
            self._guard_admin(connection, actor, target, changes, operation)
            changes = {
                key: value
                for key, value in changes.items()
                if (
                    roles_for(connection, target["id"])
                    if key == "roles"
                    else target[key]
                )
                != value
            }
            # Hashing may take time; also reject a session that expired meanwhile.
            self._authorize(
                connection,
                authority,
                info,
                operation,
            )
            if changes:
                if "roles" in changes:
                    assign_roles(connection, target["id"], changes["roles"])
                fields = {
                    key: value for key, value in changes.items() if key != "roles"
                }
                assignments = "".join(f"{key}=?," for key in fields)
                connection.execute(
                    f"""UPDATE users SET {assignments}updated_at=?,version=version+1
                    WHERE id=? AND version=?""",
                    [*fields.values(), now, target["id"], target["version"]],
                )
                if operation == "user.delete":
                    connection.execute(
                        "DELETE FROM user_roles WHERE user_id=?", (target["id"],)
                    )
                if password_operation or changes.get("status") == "disabled":
                    connection.execute(
                        """UPDATE sessions SET revoked_at=?
                        WHERE user_id=? AND revoked_at IS NULL""",
                        (now, target["id"]),
                    )
            safe_changes = {"fields": sorted(set(changes) - {"password_hash"})}
            if password_operation:
                safe_changes = {"password_changed": True}
            self._audit(connection, info, operation, target["id"], changes=safe_changes)
            row = connection.execute(
                "SELECT * FROM users WHERE id=?", (target["id"],)
            ).fetchone()
            return public_user(connection, row)

    @staticmethod
    def _check_grants(
        connection: Connection, actor: Mapping[str, Any], permissions: list[str]
    ) -> None:
        if not set(permissions) <= set(permissions_for(connection, actor["id"])):
            raise ApiError(
                403, "PERMISSION_DENIED", "不能授予或管理超出自身权限范围的授权"
            )

    def role_options(self, authority: Authority, info: RequestInfo) -> list[dict]:
        with self.database.transaction() as connection:
            actor = self._authorize(connection, authority, info, None)
            if not {"user.read", "user.create", "user.roles.assign", "role.read"} & set(
                permissions_for(connection, actor["id"])
            ):
                raise ApiError(403, "PERMISSION_DENIED", "没有查看角色选项的权限")
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT code,display_name,description FROM roles ORDER BY built_in DESC,code"
                )
            ]

    def permission_catalog(self, authority: Authority, info: RequestInfo) -> list[dict]:
        with self.database.transaction() as connection:
            self._authorize(connection, authority, info, "role.read")
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM permissions ORDER BY resource,code"
                )
            ]

    def get_role(self, authority: Authority, code: str, info: RequestInfo) -> dict:
        with self.database.transaction() as connection:
            self._authorize(connection, authority, info, "role.read")
            return role_record(connection, code)

    def mutate_role(
        self,
        authority: Authority,
        operation: Literal["role.create", "role.update", "role.delete"],
        code: str,
        expected: str | None,
        payload: dict,
        info: RequestInfo,
    ) -> dict | None:
        if operation not in {"role.create", "role.update", "role.delete"}:
            raise invalid_field("operation", "未知角色操作")
        model = RoleCreate if operation == "role.create" else RoleUpdate
        if operation == "role.delete":
            if payload:
                raise invalid_field("body", "删除角色不接受请求体")
        else:
            try:
                payload = model.model_validate(payload).model_dump(exclude_unset=True)
            except ValidationError:
                raise invalid_field("body", "角色参数不符合接口字段规则") from None
        with self.database.transaction(write=True) as connection:
            actor = self._authorize(connection, authority, info, operation)
            now = timestamp(self.clock())
            if operation == "role.create":
                code = payload["code"]
                if connection.execute(
                    "SELECT 1 FROM roles WHERE code=?", (code,)
                ).fetchone():
                    raise ApiError(409, "ROLE_CODE_TAKEN", "角色编码已存在")
                old = None
                selected = payload.get("permissions", [])
            else:
                old = role_record(connection, code)
                if code == "admin" or (operation == "role.delete" and old["built_in"]):
                    raise ApiError(
                        409, "SYSTEM_ROLE_PROTECTED", "此内置角色受保护，不能执行该操作"
                    )
                self._version(old, expected)
                self._check_grants(connection, actor, old["permissions"])
                if operation == "role.delete":
                    if old["user_count"]:
                        raise ApiError(
                            409, "ROLE_IN_USE", "角色仍分配给用户，请先调整用户角色"
                        )
                    connection.execute("DELETE FROM roles WHERE code=?", (code,))
                    self._audit(
                        connection, info, operation, None, changes={"role_code": code}
                    )
                    return None
                selected = payload.get("permissions", old["permissions"])
            require_permissions(connection, selected)
            self._check_grants(connection, actor, selected)
            if old is None:
                connection.execute(
                    """INSERT INTO roles(code,display_name,description,built_in,version,created_at,updated_at)
                    VALUES(?,?,?,0,1,?,?)""",
                    (
                        code,
                        payload["display_name"],
                        payload.get("description", ""),
                        now,
                        now,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE roles SET display_name=?,description=?,updated_at=?,version=version+1 WHERE code=?",
                    (
                        payload.get("display_name", old["display_name"]),
                        payload.get("description", old["description"]),
                        now,
                        code,
                    ),
                )
            assign_permissions(connection, code, selected)
            self._audit(
                connection,
                info,
                operation,
                None,
                changes={"role_code": code, "permissions": selected},
            )
            return role_record(connection, code)
