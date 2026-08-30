from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from packages.auth.models import (
    AuthenticatedPrincipal,
    PasswordChangeRequest,
    PasswordResetRequest,
    UserCreate,
    UserDeleteRequest,
    UserUpdate,
)
from packages.domain.models import utc_now
from packages.persistence import Database


class AuthenticationError(Exception):
    pass


class AuthNotFoundError(Exception):
    pass


class AuthConflictError(Exception):
    pass


class AuthValidationError(Exception):
    pass


class AuthAuthorizationError(Exception):
    pass


class AuthRateLimitError(Exception):
    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.retry_after = max(1, retry_after)


class AuthService:
    PASSWORD_ALGORITHM = "pbkdf2_sha256"
    PASSWORD_ITERATIONS = 600_000
    USER_SORT_COLUMNS = {
        "username": "username COLLATE NOCASE",
        "display_name": "display_name COLLATE NOCASE",
        "status": "status",
        "created_at": "created_at",
        "updated_at": "updated_at",
        "last_login_at": "last_login_at",
    }

    def __init__(self, db: Database):
        self.db = db
        self.session_ttl_hours = self._env_int(
            "DEEPAGENT_SESSION_TTL_HOURS", 12, 1, 24 * 30
        )
        self.password_max_age_days = self._env_int(
            "DEEPAGENT_PASSWORD_MAX_AGE_DAYS", 90, 1, 3650
        )
        self.max_failed_logins = self._env_int(
            "DEEPAGENT_MAX_FAILED_LOGINS", 5, 2, 100
        )
        self.lockout_minutes = self._env_int(
            "DEEPAGENT_LOGIN_LOCKOUT_MINUTES", 15, 1, 24 * 60
        )
        self.rate_limit_window_minutes = self._env_int(
            "DEEPAGENT_LOGIN_RATE_WINDOW_MINUTES", 15, 1, 24 * 60
        )
        self.last_seen_interval_seconds = self._env_int(
            "DEEPAGENT_SESSION_LAST_SEEN_SECONDS", 60, 0, 3600
        )
        self.cookie_name = os.getenv(
            "DEEPAGENT_SESSION_COOKIE_NAME", "deepagent_session"
        )
        self.cookie_secure = os.getenv(
            "DEEPAGENT_SESSION_COOKIE_SECURE", "false"
        ).lower() in {"1", "true", "yes"}
        self._dummy_password_hash = self.hash_password(secrets.token_urlsafe(32))

    def bootstrap_super_admin(self, password: str) -> Dict[str, Any]:
        existing = self._raw_user_by_username("admin")
        if existing:
            updates: Dict[str, Any] = {}
            if not existing["is_super_admin"]:
                updates["is_super_admin"] = 1
            if existing["status"] != "ACTIVE":
                updates.update(
                    status="ACTIVE",
                    deleted_at=None,
                    deleted_by=None,
                    deletion_reason=None,
                )
            roles = list(existing.get("roles") or [])
            for role in ("owner", "admin"):
                if role not in roles:
                    roles.append(role)
            if roles != existing.get("roles"):
                updates["roles_json"] = self.db.encode(roles)
            if self.verify_password(password, existing["password_hash"]):
                if not existing.get("must_change_password"):
                    updates["must_change_password"] = 1
                if not existing.get("password_changed_at"):
                    updates["password_changed_at"] = existing["created_at"]
                if not existing.get("password_expires_at"):
                    updates["password_expires_at"] = self._password_expiry()
            if updates:
                updates["updated_at"] = utc_now()
                updates["version"] = int(existing.get("version") or 1) + 1
                assignments = ", ".join(f"{name}=?" for name in updates)
                self.db.execute(
                    f"UPDATE users SET {assignments} WHERE id=?",
                    (*updates.values(), existing["id"]),
                )
            return self.get_user(existing["id"])
        payload = UserCreate(
            username="admin",
            display_name="Super Administrator",
            password=password,
            roles=["owner", "admin"],
            is_super_admin=True,
        )
        return self.create_user(payload)

    def login(
        self,
        username: str,
        password: str,
        metadata: Optional[Dict[str, Optional[str]]] = None,
    ) -> Dict[str, Any]:
        normalized = username.strip().lower()
        meta = metadata or {}
        now = datetime.now(timezone.utc)
        rate_key = self._login_rate_key(normalized, meta.get("ip_address"))
        row = self._raw_user_by_username(normalized)
        try:
            self._assert_login_allowed(rate_key, now)
        except AuthRateLimitError:
            self._audit(
                "LOGIN_BLOCKED",
                "DENIED",
                target=row,
                details={"username": normalized, "reason": "rate_limit"},
                metadata=meta,
            )
            raise
        if row:
            try:
                self._assert_account_unlocked(row, now)
            except AuthRateLimitError:
                self._audit(
                    "LOGIN_BLOCKED",
                    "DENIED",
                    target=row,
                    details={"username": normalized, "reason": "account_locked"},
                    metadata=meta,
                )
                raise
        password_matches = self.verify_password(
            password, row["password_hash"] if row else self._dummy_password_hash
        )
        if not row or row["status"] != "ACTIVE" or not password_matches:
            blocked_until = self._record_login_failure(rate_key, row, now)
            self._audit(
                "LOGIN_FAILED",
                "DENIED",
                target=row,
                details={"username": normalized},
                metadata=meta,
            )
            if blocked_until:
                raise AuthRateLimitError(
                    "Too many failed sign-in attempts; try again later",
                    self._retry_after(blocked_until, now),
                )
            raise AuthenticationError("Invalid username or password")

        self.db.execute("DELETE FROM auth_login_limits WHERE key_hash=?", (rate_key,))
        expires_at = (now + timedelta(hours=self.session_ttl_hours)).isoformat()
        token = secrets.token_urlsafe(48)
        session_id = f"session_{secrets.token_hex(16)}"
        timestamp = now.isoformat()
        self.db.execute(
            """INSERT INTO auth_sessions(
                   id, token_hash, user_id, expires_at, revoked_at, created_at,
                   last_seen_at, ip_address, user_agent
               ) VALUES(?,?,?,?,NULL,?,?,?,?)""",
            (
                session_id,
                self.hash_token(token),
                row["id"],
                expires_at,
                timestamp,
                timestamp,
                meta.get("ip_address"),
                meta.get("user_agent"),
            ),
        )
        self.db.execute(
            """UPDATE users
               SET last_login_at=?, failed_login_count=0, last_failed_login_at=NULL,
                   locked_until=NULL, updated_at=?
               WHERE id=?""",
            (timestamp, timestamp, row["id"]),
        )
        self._audit(
            "LOGIN_SUCCEEDED",
            "SUCCEEDED",
            actor_user_id=row["id"],
            target=row,
            details={"session_id": session_id},
            metadata=meta,
        )
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_at": expires_at,
            "expires_in": self.session_ttl_hours * 3600,
            "user": self.get_user(row["id"]),
        }

    def authenticate(self, token: str) -> AuthenticatedPrincipal:
        session = self.db.fetch_one(
            """SELECT s.id AS session_id, s.expires_at, s.revoked_at, s.last_seen_at,
                      u.id AS user_id, u.username, u.display_name,
                      u.tenant_id, u.project_id, u.environment_id,
                      u.roles_json AS auth_roles_json, u.is_super_admin, u.status,
                      u.must_change_password, u.password_expires_at
               FROM auth_sessions s
               JOIN users u ON u.id=s.user_id
               WHERE s.token_hash=?""",
            (self.hash_token(token),),
        )
        if not session or session["revoked_at"] or session["status"] != "ACTIVE":
            raise AuthenticationError("Authentication session is invalid")
        expires_at = self._parse_datetime(session["expires_at"])
        if not expires_at:
            raise AuthenticationError("Authentication session is invalid")
        now = datetime.now(timezone.utc)
        if expires_at <= now:
            self.revoke_session(session["session_id"])
            raise AuthenticationError("Authentication session has expired")
        last_seen = self._parse_datetime(session.pop("last_seen_at"))
        if (
            not last_seen
            or (now - last_seen).total_seconds() >= self.last_seen_interval_seconds
        ):
            self.db.execute(
                "UPDATE auth_sessions SET last_seen_at=? WHERE id=?",
                (now.isoformat(), session["session_id"]),
            )
        roles = self._decode_roles(session.pop("auth_roles_json"))
        is_super_admin = bool(session.pop("is_super_admin"))
        must_change = bool(session.pop("must_change_password")) or self._date_expired(
            session.get("password_expires_at"), now
        )
        session.pop("status", None)
        session.pop("revoked_at", None)
        return AuthenticatedPrincipal(
            **session,
            roles=roles,
            is_super_admin=is_super_admin,
            must_change_password=must_change,
        )

    def logout(
        self,
        principal: AuthenticatedPrincipal,
        metadata: Optional[Dict[str, Optional[str]]] = None,
    ) -> None:
        self.revoke_session(principal.session_id)
        target = self._raw_user(principal.user_id)
        self._audit(
            "LOGOUT",
            "SUCCEEDED",
            actor=principal,
            target=target,
            details={"session_id": principal.session_id},
            metadata=metadata,
        )

    def revoke_session(self, session_id: str) -> int:
        return self.db.execute_count(
            "UPDATE auth_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
            (utc_now(), session_id),
        )

    def purge_expired_sessions(self) -> int:
        now = datetime.now(timezone.utc)
        revoked_cutoff = (now - timedelta(days=30)).isoformat()
        return self.db.execute_count(
            """DELETE FROM auth_sessions
               WHERE expires_at<=? OR (revoked_at IS NOT NULL AND revoked_at<=?)""",
            (now.isoformat(), revoked_cutoff),
        )

    def list_users(
        self,
        actor: AuthenticatedPrincipal,
        *,
        page: int = 1,
        page_size: int = 20,
        query: Optional[str] = None,
        status: Optional[str] = None,
        role: Optional[str] = None,
        tenant_id: Optional[str] = None,
        project_id: Optional[str] = None,
        sort_by: str = "username",
        sort_order: str = "asc",
    ) -> Dict[str, Any]:
        self._assert_manager(actor)
        where: list[str] = []
        params: list[Any] = []
        if actor.is_super_admin:
            if tenant_id:
                where.append("tenant_id=?")
                params.append(tenant_id)
        else:
            if tenant_id and tenant_id != actor.tenant_id:
                raise AuthAuthorizationError(
                    "Tenant administrators can only view their own tenant"
                )
            where.extend(("tenant_id=?", "is_super_admin=0"))
            params.append(actor.tenant_id)
        if project_id:
            where.append("project_id=?")
            params.append(project_id)
        if status and status != "ALL":
            where.append("status=?")
            params.append(status)
        if role:
            where.append("roles_json LIKE ?")
            params.append(f'%"{role.lower()}"%')
        if query:
            needle = f"%{query.strip().lower()}%"
            where.append(
                "(LOWER(username) LIKE ? OR LOWER(display_name) LIKE ? "
                "OR LOWER(tenant_id) LIKE ? OR LOWER(project_id) LIKE ?)"
            )
            params.extend((needle, needle, needle, needle))
        clause = " WHERE " + " AND ".join(where) if where else ""
        count = self.db.fetch_one(f"SELECT COUNT(*) AS count FROM users{clause}", params)
        total = int(count["count"] if count else 0)
        sort_column = self.USER_SORT_COLUMNS.get(sort_by, self.USER_SORT_COLUMNS["username"])
        direction = "DESC" if sort_order.lower() == "desc" else "ASC"
        offset = (page - 1) * page_size
        rows = self.db.fetch_all(
            f"SELECT * FROM users{clause} ORDER BY {sort_column} {direction}, id ASC LIMIT ? OFFSET ?",
            (*params, page_size, offset),
        )
        return {
            "items": [self._public_user(row) for row in rows],
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": math.ceil(total / page_size) if total else 0,
        }

    def find_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        row = self._raw_user(user_id)
        return self._public_user(row) if row else None

    def get_user(self, user_id: str) -> Dict[str, Any]:
        user = self.find_user(user_id)
        if not user:
            raise AuthNotFoundError("User not found")
        return user

    def get_managed_user(
        self, user_id: str, actor: AuthenticatedPrincipal
    ) -> Dict[str, Any]:
        row = self._require_raw_user(user_id)
        self._assert_can_manage_target(actor, row)
        return self._public_user(row)

    def create_user(
        self,
        payload: UserCreate,
        actor: Optional[AuthenticatedPrincipal] = None,
        metadata: Optional[Dict[str, Optional[str]]] = None,
    ) -> Dict[str, Any]:
        if actor:
            self._assert_can_create(actor, payload)
        now = utc_now()
        user_id = f"user_{secrets.token_hex(16)}"
        try:
            self.db.execute(
                """INSERT INTO users(
                       id, username, display_name, password_hash, tenant_id, project_id,
                       environment_id, roles_json, is_super_admin, status, version,
                       last_login_at, password_changed_at, password_expires_at,
                       must_change_password, failed_login_count, last_failed_login_at,
                       locked_until, deleted_at, deleted_by, deletion_reason,
                       created_at, updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,'ACTIVE',1,NULL,?,?,1,0,NULL,NULL,NULL,NULL,NULL,?,?)""",
                (
                    user_id,
                    payload.username,
                    payload.display_name,
                    self.hash_password(payload.password),
                    payload.tenant_id,
                    payload.project_id,
                    payload.environment_id,
                    self.db.encode(payload.roles),
                    int(payload.is_super_admin),
                    now,
                    self._password_expiry(),
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise AuthConflictError("Username already exists") from error
        row = self._require_raw_user(user_id)
        self._audit(
            "USER_CREATED",
            "SUCCEEDED",
            actor=actor,
            target=row,
            details={
                "username": payload.username,
                "roles": payload.roles,
                "is_super_admin": payload.is_super_admin,
            },
            metadata=metadata,
        )
        return self._public_user(row)

    def update_user(
        self,
        user_id: str,
        payload: UserUpdate,
        actor: AuthenticatedPrincipal,
        metadata: Optional[Dict[str, Optional[str]]] = None,
    ) -> Dict[str, Any]:
        current = self._require_raw_user(user_id)
        self._assert_can_manage_target(actor, current)
        updates = payload.model_dump(exclude_unset=True)
        version = updates.pop("version")
        if not updates:
            if int(current["version"]) != version:
                raise AuthConflictError("User was changed by another request; reload and retry")
            return self._public_user(current)
        next_super = updates.get("is_super_admin", bool(current["is_super_admin"]))
        next_roles = updates.get("roles", list(current.get("roles") or []))
        next_tenant = updates.get("tenant_id", current["tenant_id"])
        self._validate_user_transition(
            current, actor, updates, next_super, next_roles, next_tenant
        )
        if current["username"] == "admin" and updates.get("username", "admin") != "admin":
            raise AuthValidationError(
                "The built-in administrator username cannot be changed"
            )
        if "roles" in updates:
            updates["roles_json"] = self.db.encode(updates.pop("roles"))
        if "is_super_admin" in updates:
            updates["is_super_admin"] = int(bool(updates["is_super_admin"]))
        if updates.get("status") == "ACTIVE":
            updates.update(deleted_at=None, deleted_by=None, deletion_reason=None)
        updates["updated_at"] = utc_now()
        assignments = ", ".join(f"{name}=?" for name in updates)
        try:
            changed = self.db.execute_count(
                f"UPDATE users SET {assignments}, version=version+1 WHERE id=? AND version=?",
                (*updates.values(), user_id, version),
            )
        except sqlite3.IntegrityError as error:
            raise AuthConflictError("Username already exists") from error
        if not changed:
            raise AuthConflictError("User was changed by another request; reload and retry")
        updated = self._require_raw_user(user_id)
        self._audit(
            "USER_UPDATED" if current["status"] == updated["status"] else "USER_REACTIVATED",
            "SUCCEEDED",
            actor=actor,
            target=updated,
            details={"changed_fields": sorted(updates.keys() - {"updated_at"})},
            metadata=metadata,
        )
        return self._public_user(updated)

    def reset_password(
        self,
        user_id: str,
        payload: PasswordResetRequest,
        actor: AuthenticatedPrincipal,
        metadata: Optional[Dict[str, Optional[str]]] = None,
    ) -> Dict[str, Any]:
        current = self._require_raw_user(user_id)
        self._assert_can_manage_target(actor, current)
        now = utc_now()
        changed = self.db.execute_count(
            """UPDATE users
               SET password_hash=?, password_changed_at=?, password_expires_at=?,
                   must_change_password=1, failed_login_count=0,
                   last_failed_login_at=NULL, locked_until=NULL,
                   updated_at=?, version=version+1
               WHERE id=? AND version=?""",
            (
                self.hash_password(payload.password),
                now,
                self._password_expiry(),
                now,
                user_id,
                payload.version,
            ),
        )
        if not changed:
            raise AuthConflictError("User was changed by another request; reload and retry")
        keep_session_id = actor.session_id if user_id == actor.user_id else None
        revoked = self.revoke_user_sessions(user_id, keep_session_id)
        updated = self._require_raw_user(user_id)
        self._audit(
            "USER_PASSWORD_RESET",
            "SUCCEEDED",
            actor=actor,
            target=updated,
            details={"revoked_sessions": revoked, "must_change_password": True},
            metadata=metadata,
        )
        return self._public_user(updated)

    def change_own_password(
        self,
        payload: PasswordChangeRequest,
        actor: AuthenticatedPrincipal,
        metadata: Optional[Dict[str, Optional[str]]] = None,
    ) -> Dict[str, Any]:
        current = self._require_raw_user(actor.user_id)
        if not self.verify_password(payload.current_password, current["password_hash"]):
            self._audit(
                "SELF_PASSWORD_CHANGE",
                "DENIED",
                actor=actor,
                target=current,
                details={"reason": "current_password_mismatch"},
                metadata=metadata,
            )
            raise AuthValidationError("Current password is incorrect")
        now = utc_now()
        changed = self.db.execute_count(
            """UPDATE users
               SET password_hash=?, password_changed_at=?, password_expires_at=?,
                   must_change_password=0, failed_login_count=0,
                   last_failed_login_at=NULL, locked_until=NULL,
                   updated_at=?, version=version+1
               WHERE id=? AND version=?""",
            (
                self.hash_password(payload.new_password),
                now,
                self._password_expiry(),
                now,
                actor.user_id,
                payload.version,
            ),
        )
        if not changed:
            raise AuthConflictError("User was changed by another request; reload and retry")
        revoked = self.revoke_user_sessions(
            actor.user_id, keep_session_id=actor.session_id
        )
        updated = self._require_raw_user(actor.user_id)
        self._audit(
            "SELF_PASSWORD_CHANGE",
            "SUCCEEDED",
            actor=actor,
            target=updated,
            details={"revoked_other_sessions": revoked},
            metadata=metadata,
        )
        return self._public_user(updated)

    def deactivate_user(
        self,
        user_id: str,
        payload: UserDeleteRequest,
        actor: AuthenticatedPrincipal,
        metadata: Optional[Dict[str, Optional[str]]] = None,
    ) -> Dict[str, Any]:
        current = self._require_raw_user(user_id)
        self._assert_can_manage_target(actor, current)
        if current["status"] == "INACTIVE":
            raise AuthValidationError("User is already inactive")
        if current["username"] == "admin":
            raise AuthValidationError("The built-in administrator cannot be disabled")
        if user_id == actor.user_id:
            raise AuthValidationError("You cannot disable your own account")
        if current["is_super_admin"]:
            self._require_another_active_super_admin(user_id)
        now = utc_now()
        changed = self.db.execute_count(
            """UPDATE users
               SET status='INACTIVE', deleted_at=?, deleted_by=?, deletion_reason=?,
                   updated_at=?, version=version+1
               WHERE id=? AND version=?""",
            (now, actor.user_id, payload.reason, now, user_id, payload.version),
        )
        if not changed:
            raise AuthConflictError("User was changed by another request; reload and retry")
        revoked = self.revoke_user_sessions(user_id)
        updated = self._require_raw_user(user_id)
        self._audit(
            "USER_DEACTIVATED",
            "SUCCEEDED",
            actor=actor,
            target=updated,
            details={"reason": payload.reason, "revoked_sessions": revoked},
            metadata=metadata,
        )
        return self._public_user(updated)

    def list_sessions(
        self,
        user_id: str,
        actor: AuthenticatedPrincipal,
        current_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        target = self._require_raw_user(user_id)
        self._assert_self_or_manager(actor, target)
        rows = self.db.fetch_all(
            """SELECT id, user_id, expires_at, revoked_at, created_at, last_seen_at,
                      ip_address, user_agent
               FROM auth_sessions WHERE user_id=? ORDER BY created_at DESC""",
            (user_id,),
        )
        return {
            "items": [
                self._public_session(row, current_session_id or actor.session_id)
                for row in rows
            ]
        }

    def revoke_managed_session(
        self,
        user_id: str,
        session_id: str,
        actor: AuthenticatedPrincipal,
        metadata: Optional[Dict[str, Optional[str]]] = None,
    ) -> Dict[str, Any]:
        target = self._require_raw_user(user_id)
        self._assert_self_or_manager(actor, target)
        session = self.db.fetch_one(
            "SELECT id, revoked_at FROM auth_sessions WHERE id=? AND user_id=?",
            (session_id, user_id),
        )
        if not session:
            raise AuthNotFoundError("Session not found")
        changed = self.revoke_session(session_id)
        self._audit(
            "SESSION_REVOKED",
            "SUCCEEDED",
            actor=actor,
            target=target,
            details={"session_id": session_id, "revoked": bool(changed)},
            metadata=metadata,
        )
        return {
            "ok": True,
            "revoked_count": changed,
            "revoked_current": session_id == actor.session_id,
        }

    def revoke_all_managed_sessions(
        self,
        user_id: str,
        actor: AuthenticatedPrincipal,
        metadata: Optional[Dict[str, Optional[str]]] = None,
    ) -> Dict[str, Any]:
        target = self._require_raw_user(user_id)
        self._assert_self_or_manager(actor, target)
        current_active = self.db.fetch_one(
            """SELECT id FROM auth_sessions
               WHERE id=? AND user_id=? AND revoked_at IS NULL""",
            (actor.session_id, user_id),
        )
        changed = self.revoke_user_sessions(user_id)
        self._audit(
            "USER_SESSIONS_REVOKED",
            "SUCCEEDED",
            actor=actor,
            target=target,
            details={"revoked_sessions": changed},
            metadata=metadata,
        )
        return {
            "ok": True,
            "revoked_count": changed,
            "revoked_current": bool(current_active),
        }

    def revoke_user_sessions(
        self, user_id: str, keep_session_id: Optional[str] = None
    ) -> int:
        if keep_session_id:
            return self.db.execute_count(
                """UPDATE auth_sessions SET revoked_at=?
                   WHERE user_id=? AND id<>? AND revoked_at IS NULL""",
                (utc_now(), user_id, keep_session_id),
            )
        return self.db.execute_count(
            "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (utc_now(), user_id),
        )

    def list_audit_events(
        self,
        actor: AuthenticatedPrincipal,
        *,
        page: int = 1,
        page_size: int = 20,
        query: Optional[str] = None,
        action: Optional[str] = None,
        outcome: Optional[str] = None,
        target_user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._assert_manager(actor)
        where: list[str] = []
        params: list[Any] = []
        if not actor.is_super_admin:
            where.append("tenant_id=?")
            params.append(actor.tenant_id)
        if action:
            where.append("action=?")
            params.append(action)
        if outcome:
            where.append("outcome=?")
            params.append(outcome)
        if target_user_id:
            target = self._require_raw_user(target_user_id)
            self._assert_can_manage_target(actor, target)
            where.append("target_user_id=?")
            params.append(target_user_id)
        if query:
            needle = f"%{query.strip().lower()}%"
            where.append(
                "(LOWER(action) LIKE ? OR LOWER(outcome) LIKE ? "
                "OR LOWER(COALESCE(actor_user_id,'')) LIKE ? "
                "OR LOWER(COALESCE(target_user_id,'')) LIKE ?)"
            )
            params.extend((needle, needle, needle, needle))
        clause = " WHERE " + " AND ".join(where) if where else ""
        count = self.db.fetch_one(
            f"SELECT COUNT(*) AS count FROM auth_audit_events{clause}", params
        )
        total = int(count["count"] if count else 0)
        rows = self.db.fetch_all(
            f"""SELECT * FROM auth_audit_events{clause}
                ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?""",
            (*params, page_size, (page - 1) * page_size),
        )
        return {
            "items": rows,
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": math.ceil(total / page_size) if total else 0,
        }

    def _validate_user_transition(
        self,
        current: Dict[str, Any],
        actor: AuthenticatedPrincipal,
        updates: Dict[str, Any],
        next_super: bool,
        next_roles: list[str],
        next_tenant: str,
    ) -> None:
        if not actor.is_super_admin:
            if next_tenant != actor.tenant_id:
                raise AuthAuthorizationError(
                    "Tenant administrators cannot move users outside their tenant"
                )
            if next_super:
                raise AuthAuthorizationError(
                    "Tenant administrators cannot grant super administrator access"
                )
        if current["username"] == "admin" and not next_super:
            raise AuthValidationError(
                "The built-in administrator must remain a super administrator"
            )
        if current["is_super_admin"] and not next_super:
            if current["id"] == actor.user_id:
                raise AuthValidationError(
                    "You cannot remove your own super administrator access"
                )
            self._require_another_active_super_admin(current["id"])
        if (
            current["id"] == actor.user_id
            and actor.is_tenant_admin
            and "tenant_admin" not in next_roles
        ):
            raise AuthValidationError(
                "You cannot remove your own tenant administrator access"
            )

    def _assert_manager(self, actor: AuthenticatedPrincipal) -> None:
        if not actor.is_super_admin and not actor.is_tenant_admin:
            raise AuthAuthorizationError(
                "Platform or tenant administrator access is required"
            )

    def _assert_can_create(
        self, actor: AuthenticatedPrincipal, payload: UserCreate
    ) -> None:
        self._assert_manager(actor)
        if actor.is_super_admin:
            return
        if payload.tenant_id != actor.tenant_id:
            raise AuthAuthorizationError(
                "Tenant administrators can only create users in their own tenant"
            )
        if payload.is_super_admin:
            raise AuthAuthorizationError(
                "Tenant administrators cannot create super administrators"
            )

    def _assert_can_manage_target(
        self, actor: AuthenticatedPrincipal, target: Dict[str, Any]
    ) -> None:
        self._assert_manager(actor)
        if actor.is_super_admin:
            return
        if target["tenant_id"] != actor.tenant_id or target["is_super_admin"]:
            raise AuthAuthorizationError(
                "Tenant administrators can only manage non-super users in their own tenant"
            )

    def _assert_self_or_manager(
        self, actor: AuthenticatedPrincipal, target: Dict[str, Any]
    ) -> None:
        if actor.user_id == target["id"]:
            return
        self._assert_can_manage_target(actor, target)

    def _require_another_active_super_admin(self, excluded_user_id: str) -> None:
        result = self.db.fetch_one(
            """SELECT COUNT(*) AS count FROM users
               WHERE is_super_admin=1 AND status='ACTIVE' AND id<>?""",
            (excluded_user_id,),
        )
        if not result or result["count"] < 1:
            raise AuthValidationError("At least one active super administrator is required")

    def _assert_login_allowed(self, key_hash: str, now: datetime) -> None:
        limit = self.db.fetch_one(
            "SELECT * FROM auth_login_limits WHERE key_hash=?", (key_hash,)
        )
        if not limit:
            return
        blocked_until = self._parse_datetime(limit.get("blocked_until"))
        if blocked_until and blocked_until > now:
            raise AuthRateLimitError(
                "Too many failed sign-in attempts; try again later",
                self._retry_after(blocked_until, now),
            )
        window_started = self._parse_datetime(limit.get("window_started_at"))
        if not window_started or now - window_started >= timedelta(
            minutes=self.rate_limit_window_minutes
        ):
            self.db.execute("DELETE FROM auth_login_limits WHERE key_hash=?", (key_hash,))

    def _assert_account_unlocked(self, row: Dict[str, Any], now: datetime) -> None:
        locked_until = self._parse_datetime(row.get("locked_until"))
        if locked_until and locked_until > now:
            raise AuthRateLimitError(
                "Account is temporarily locked; try again later",
                self._retry_after(locked_until, now),
            )
        if locked_until:
            self.db.execute(
                """UPDATE users SET failed_login_count=0, last_failed_login_at=NULL,
                          locked_until=NULL WHERE id=?""",
                (row["id"],),
            )
            row["failed_login_count"] = 0
            row["locked_until"] = None

    def _record_login_failure(
        self, key_hash: str, row: Optional[Dict[str, Any]], now: datetime
    ) -> Optional[datetime]:
        limit = self.db.fetch_one(
            "SELECT * FROM auth_login_limits WHERE key_hash=?", (key_hash,)
        )
        window_started = self._parse_datetime(
            limit.get("window_started_at") if limit else None
        )
        if not window_started or now - window_started >= timedelta(
            minutes=self.rate_limit_window_minutes
        ):
            attempts = 1
            window_started = now
        else:
            attempts = int(limit.get("attempts") or 0) + 1
        blocked_until = (
            now + timedelta(minutes=self.lockout_minutes)
            if attempts >= self.max_failed_logins
            else None
        )
        self.db.execute(
            """INSERT INTO auth_login_limits(
                   key_hash, attempts, window_started_at, blocked_until, updated_at
               ) VALUES(?,?,?,?,?)
               ON CONFLICT(key_hash) DO UPDATE SET
                 attempts=excluded.attempts,
                 window_started_at=excluded.window_started_at,
                 blocked_until=excluded.blocked_until,
                 updated_at=excluded.updated_at""",
            (
                key_hash,
                attempts,
                window_started.isoformat(),
                blocked_until.isoformat() if blocked_until else None,
                now.isoformat(),
            ),
        )
        if row and row["status"] == "ACTIVE":
            account_attempts = int(row.get("failed_login_count") or 0) + 1
            account_locked = (
                now + timedelta(minutes=self.lockout_minutes)
                if account_attempts >= self.max_failed_logins
                else None
            )
            self.db.execute(
                """UPDATE users SET failed_login_count=?, last_failed_login_at=?,
                          locked_until=? WHERE id=?""",
                (
                    account_attempts,
                    now.isoformat(),
                    account_locked.isoformat() if account_locked else None,
                    row["id"],
                ),
            )
            if account_locked:
                blocked_until = max(blocked_until or account_locked, account_locked)
        return blocked_until

    def _audit(
        self,
        action: str,
        outcome: str,
        *,
        actor: Optional[AuthenticatedPrincipal] = None,
        actor_user_id: Optional[str] = None,
        target: Optional[Dict[str, Any]] = None,
        details: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Optional[str]]] = None,
    ) -> None:
        meta = metadata or {}
        self.db.execute(
            """INSERT INTO auth_audit_events(
                   id, actor_user_id, target_user_id, tenant_id, project_id,
                   action, outcome, ip_address, user_agent, details_json, created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"audit_{secrets.token_hex(16)}",
                actor_user_id or (actor.user_id if actor else None),
                target.get("id") if target else None,
                target.get("tenant_id") if target else (actor.tenant_id if actor else None),
                target.get("project_id") if target else (actor.project_id if actor else None),
                action,
                outcome,
                meta.get("ip_address"),
                meta.get("user_agent"),
                self.db.encode(self._safe_details(details or {})),
                utc_now(),
            ),
        )

    @classmethod
    def _safe_details(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): "[REDACTED]"
                if any(secret in str(key).lower() for secret in ("password", "token"))
                else cls._safe_details(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._safe_details(item) for item in value]
        return value

    def _raw_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        return self.db.fetch_one("SELECT * FROM users WHERE id=?", (user_id,))

    def _raw_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        return self.db.fetch_one(
            "SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)
        )

    def _require_raw_user(self, user_id: str) -> Dict[str, Any]:
        row = self._raw_user(user_id)
        if not row:
            raise AuthNotFoundError("User not found")
        return row

    def _public_user(self, row: Dict[str, Any]) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "tenant_id": row["tenant_id"],
            "project_id": row["project_id"],
            "environment_id": row["environment_id"],
            "roles": list(row.get("roles") or []),
            "is_super_admin": bool(row["is_super_admin"]),
            "status": row["status"],
            "version": int(row.get("version") or 1),
            "last_login_at": row.get("last_login_at"),
            "password_changed_at": row.get("password_changed_at"),
            "password_expires_at": row.get("password_expires_at"),
            "must_change_password": bool(row.get("must_change_password"))
            or self._date_expired(row.get("password_expires_at"), now),
            "failed_login_count": int(row.get("failed_login_count") or 0),
            "locked_until": row.get("locked_until"),
            "deleted_at": row.get("deleted_at"),
            "deleted_by": row.get("deleted_by"),
            "deletion_reason": row.get("deletion_reason"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _public_session(
        self, row: Dict[str, Any], current_session_id: Optional[str]
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        if row.get("revoked_at"):
            status = "REVOKED"
        elif self._date_expired(row.get("expires_at"), now):
            status = "EXPIRED"
        else:
            status = "ACTIVE"
        return {
            **row,
            "status": status,
            "current": row["id"] == current_session_id,
        }

    def _password_expiry(self) -> str:
        return (
            datetime.now(timezone.utc) + timedelta(days=self.password_max_age_days)
        ).isoformat()

    @staticmethod
    def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _date_expired(cls, value: Optional[str], now: datetime) -> bool:
        parsed = cls._parse_datetime(value)
        return bool(parsed and parsed <= now)

    @staticmethod
    def _retry_after(until: datetime, now: datetime) -> int:
        return max(1, math.ceil((until - now).total_seconds()))

    @staticmethod
    def _login_rate_key(username: str, ip_address: Optional[str]) -> str:
        value = f"{username.lower()}|{ip_address or 'unknown'}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _decode_roles(raw: str) -> list[str]:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return [str(role) for role in value] if isinstance(value, list) else []

    @staticmethod
    def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except ValueError:
            value = default
        return max(minimum, min(maximum, value))

    @classmethod
    def hash_password(cls, password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, cls.PASSWORD_ITERATIONS
        )
        return "$".join(
            (
                cls.PASSWORD_ALGORITHM,
                str(cls.PASSWORD_ITERATIONS),
                base64.b64encode(salt).decode("ascii"),
                base64.b64encode(digest).decode("ascii"),
            )
        )

    @classmethod
    def verify_password(cls, password: str, encoded: str) -> bool:
        try:
            algorithm, iterations_text, salt_text, digest_text = encoded.split("$", 3)
            if algorithm != cls.PASSWORD_ALGORITHM:
                return False
            iterations = int(iterations_text)
            salt = base64.b64decode(salt_text, validate=True)
            expected = base64.b64decode(digest_text, validate=True)
        except (ValueError, TypeError):
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
        return hmac.compare_digest(actual, expected)

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
