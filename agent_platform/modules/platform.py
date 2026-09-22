"""Tenant-scoped application services for users, agents and durable runs."""

import hashlib
import json
import secrets
import time
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.db import Database
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.billing.service import BillingService, reservations_table

ACTIVE_STATUSES = ("queued", "running", "cancelling")
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "expired")
hasher = PasswordHasher()
_DUMMY_HASH = hasher.hash(secrets.token_urlsafe(32))


def now() -> str:
    return datetime.now(UTC).isoformat()


def uid() -> str:
    return uuid4().hex


def public_user(row) -> dict:
    return {
        key: row[key]
        for key in ("id", "tenant_id", "email", "name", "role", "active", "created_at")
    }


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def audit(connection, user: dict, action: str, target: str) -> None:
    connection.execute(
        insert(t.audits).values(
            id=uid(),
            tenant_id=user["tenant_id"],
            created_at=now(),
            actor_id=user["id"],
            actor_email=user["email"],
            action=action,
            target=target,
        )
    )


def append_event(connection, run_id: str, kind: str, data: dict) -> dict:
    sequence = (
        connection.scalar(
            select(func.max(t.run_events.c.sequence)).where(
                t.run_events.c.run_id == run_id
            )
        )
        or 0
    ) + 1
    event = {
        "run_id": run_id,
        "sequence": sequence,
        "type": kind,
        "data": data,
        "created_at": now(),
    }
    connection.execute(insert(t.run_events).values(**event))
    return event


class Platform:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.billing = BillingService(db, settings.redis_url)

    def bootstrap(self) -> None:
        self._bootstrap_admin()
        self._bootstrap_default_model()

    def _bootstrap_admin(self) -> None:
        """Create the first tenant only with an explicit, non-default password."""
        with self.db.transaction("bootstrap") as connection:
            if connection.scalar(select(func.count()).select_from(t.users)):
                return
            if len(self.settings.admin_password) < 12:
                raise PlatformError(
                    503,
                    "bootstrap_required",
                    "首次启动请设置至少 12 位的 PLATFORM_ADMIN_PASSWORD。",
                )
            tenant_id, user_id = uid(), uid()
            stamp = now()
            connection.execute(
                insert(t.tenants).values(
                    id=tenant_id, name="我的组织", created_at=stamp
                )
            )
            user = {
                "id": user_id,
                "tenant_id": tenant_id,
                "email": self.settings.admin_email.strip().lower(),
                "name": "管理员",
                "role": "admin",
                "active": True,
                "created_at": stamp,
            }
            connection.execute(
                insert(t.users).values(
                    **user, password_hash=hasher.hash(self.settings.admin_password)
                )
            )
            audit(connection, user, "tenant.bootstrap", tenant_id)
        self.billing.wallet(tenant_id)

    def _bootstrap_default_model(self) -> None:
        """Register the configured model once, using only explicit platform prices."""
        if not self.settings.default_model:
            return
        with self.db.read() as connection:
            user = (
                connection.execute(
                    select(t.users).where(
                        t.users.c.email == self.settings.admin_email.strip().lower(),
                        t.users.c.role == "admin",
                        t.users.c.active.is_(True),
                    )
                )
                .mappings()
                .first()
            )
        if user is None:
            return
        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            existing = connection.scalar(
                select(t.models.c.id).where(
                    t.models.c.tenant_id == user["tenant_id"],
                    t.models.c.alias == self.settings.default_model,
                )
            )
            if existing:
                return
            if not (
                self.settings.default_model_input_price
                and self.settings.default_model_output_price
            ):
                return
            from agent_platform.apps.api.schemas import ModelCreate

            values = ModelCreate(
                name=self.settings.default_model,
                alias=self.settings.default_model,
                input_price=self.settings.default_model_input_price,
                output_price=self.settings.default_model_output_price,
            ).model_dump()
            self.require_current(connection, dict(user), admin=True)
            model_id = uid()
            connection.execute(
                insert(t.models).values(
                    **values,
                    id=model_id,
                    tenant_id=user["tenant_id"],
                    created_at=now(),
                    price_version=1,
                )
            )
            audit(connection, dict(user), "model.bootstrap", model_id)

    def login(self, email: str, password: str, address: str) -> dict:
        email = email.strip().lower()
        window = int(time.time() // 300)
        with self.db.transaction("authentication") as connection:
            for key, limit in (
                (digest("email:" + email), 10),
                (digest("ip:" + address), 50),
            ):
                row = (
                    connection.execute(
                        select(t.login_attempts).where(t.login_attempts.c.key == key)
                    )
                    .mappings()
                    .first()
                )
                count = row["count"] if row and row["window"] == window else 0
                if count >= limit:
                    raise PlatformError(
                        429, "login_rate_limited", "登录尝试过于频繁，请稍后重试。"
                    )
                if row:
                    connection.execute(
                        update(t.login_attempts)
                        .where(t.login_attempts.c.key == key)
                        .values(window=window, count=count + 1)
                    )
                else:
                    connection.execute(
                        insert(t.login_attempts).values(key=key, window=window, count=1)
                    )
            row = (
                connection.execute(select(t.users).where(t.users.c.email == email))
                .mappings()
                .first()
            )
        try:
            valid = hasher.verify(
                row["password_hash"] if row else _DUMMY_HASH, password
            )
        except (VerificationError, InvalidHashError):
            valid = False
        if not valid or not row or not row["active"]:
            raise PlatformError(401, "invalid_credentials", "邮箱或密码不正确。")
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        with self.db.transaction(f"tenant:{row['tenant_id']}") as connection:
            current = (
                connection.execute(select(t.users).where(t.users.c.id == row["id"]))
                .mappings()
                .one()
            )
            if (
                not current["active"]
                or current["password_hash"] != row["password_hash"]
            ):
                raise PlatformError(
                    401, "invalid_credentials", "账号状态已变化，请重新登录。"
                )
            connection.execute(
                insert(t.auth_sessions).values(
                    token_hash=digest(token),
                    user_id=row["id"],
                    csrf_token=csrf,
                    expires_at=time.time() + self.settings.session_hours * 3600,
                )
            )
            audit(connection, current, "auth.login", current["id"])
        return {"user": public_user(current), "csrf_token": csrf, "token": token}

    def authenticate(self, token: str | None) -> dict:
        if not token:
            raise PlatformError(401, "authentication_required", "请先登录。")
        with self.db.read() as connection:
            row = (
                connection.execute(
                    select(t.users, t.auth_sessions.c.csrf_token)
                    .join(t.auth_sessions, t.users.c.id == t.auth_sessions.c.user_id)
                    .where(
                        t.auth_sessions.c.token_hash == digest(token),
                        t.auth_sessions.c.expires_at > time.time(),
                        t.users.c.active.is_(True),
                    )
                )
                .mappings()
                .first()
            )
        if not row:
            raise PlatformError(401, "session_expired", "登录已失效，请重新登录。")
        return {"user": public_user(row), "csrf_token": row["csrf_token"]}

    def require_current(self, connection, user: dict, admin: bool = False) -> dict:
        row = (
            connection.execute(
                select(t.users).where(
                    t.users.c.id == user["id"],
                    t.users.c.tenant_id == user["tenant_id"],
                    t.users.c.active.is_(True),
                )
            )
            .mappings()
            .first()
        )
        if not row or row["role"] != user["role"]:
            raise PlatformError(403, "access_revoked", "账号权限已变化，请重新登录。")
        if admin and row["role"] != "admin":
            raise PlatformError(403, "admin_required", "此操作需要管理员权限。")
        return dict(row)

    def list_resources(
        self, table, user: dict, own: bool = False, limit: int = 200
    ) -> list[dict]:
        query = select(table).where(table.c.tenant_id == user["tenant_id"])
        if own and user["role"] != "admin":
            query = query.where(table.c.user_id == user["id"])
        with self.db.read() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    query.order_by(table.c.created_at.desc(), table.c.id.desc()).limit(
                        limit
                    )
                ).mappings()
            ]

    def owned(
        self, connection, table, user: dict, resource_id: str, own: bool = False
    ) -> dict:
        query = select(table).where(
            table.c.id == resource_id, table.c.tenant_id == user["tenant_id"]
        )
        if own and user["role"] != "admin":
            query = query.where(table.c.user_id == user["id"])
        row = connection.execute(query).mappings().first()
        if not row:
            raise PlatformError(404, "not_found", "资源不存在或无权访问。")
        return dict(row)

    def create_user(self, user: dict, values: dict) -> dict:
        values["email"] = values["email"].strip().lower()
        password_hash = hasher.hash(values.pop("password"))
        result = dict(
            values, id=uid(), tenant_id=user["tenant_id"], active=True, created_at=now()
        )
        try:
            with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
                self.require_current(connection, user, admin=True)
                connection.execute(
                    insert(t.users).values(**result, password_hash=password_hash)
                )
                audit(connection, user, "user.create", result["id"])
        except IntegrityError:
            raise PlatformError(409, "email_exists", "该邮箱已被使用。") from None
        return result

    def update_user(self, user: dict, user_id: str, values: dict) -> dict:
        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            self.require_current(connection, user, admin=True)
            current = self.owned(connection, t.users, user, user_id)
            revised = {**current, **values}
            if (
                current["role"] == "admin"
                and current["active"]
                and (revised["role"] != "admin" or not revised["active"])
            ):
                remaining = connection.scalar(
                    select(func.count())
                    .select_from(t.users)
                    .where(
                        t.users.c.tenant_id == user["tenant_id"],
                        t.users.c.active.is_(True),
                        t.users.c.role == "admin",
                        t.users.c.id != user_id,
                    )
                )
                if not remaining:
                    raise PlatformError(
                        409, "last_admin", "必须保留至少一名启用的管理员。"
                    )
            connection.execute(
                update(t.users).where(t.users.c.id == user_id).values(**values)
            )
            if "active" in values or "role" in values:
                connection.execute(
                    delete(t.auth_sessions).where(t.auth_sessions.c.user_id == user_id)
                )
            audit(connection, user, "user.update", user_id)
        return public_user(revised)

    def change_password(self, user: dict, old: str, new: str) -> None:
        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            current = self.require_current(connection, user)
            try:
                hasher.verify(current["password_hash"], old)
            except VerificationError:
                raise PlatformError(
                    400, "invalid_password", "当前密码不正确。"
                ) from None
            connection.execute(
                update(t.users)
                .where(t.users.c.id == user["id"])
                .values(password_hash=hasher.hash(new))
            )
            connection.execute(
                delete(t.auth_sessions).where(t.auth_sessions.c.user_id == user["id"])
            )
            audit(connection, user, "auth.password_changed", user["id"])

    def save_model(self, user: dict, values: dict, model_id: str | None = None) -> dict:
        try:
            with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
                self.require_current(connection, user, admin=True)
                current = (
                    self.owned(connection, t.models, user, model_id)
                    if model_id
                    else {
                        "id": uid(),
                        "tenant_id": user["tenant_id"],
                        "created_at": now(),
                        "price_version": 0,
                    }
                )
                result = {
                    **current,
                    **values,
                    "price_version": current["price_version"] + 1,
                }
                if result["max_output_tokens"] > result["context_window"]:
                    raise PlatformError(
                        400, "invalid_model_limits", "最大输出不能超过上下文窗口。"
                    )
                if model_id:
                    connection.execute(
                        update(t.models)
                        .where(t.models.c.id == model_id)
                        .values(**result)
                    )
                else:
                    connection.execute(insert(t.models).values(**result))
                audit(
                    connection,
                    user,
                    "model.update" if model_id else "model.create",
                    result["id"],
                )
        except IntegrityError:
            raise PlatformError(
                409, "model_alias_exists", "当前组织中已存在该模型别名。"
            ) from None
        return result

    def save_agent(self, user: dict, values: dict, agent_id: str | None = None) -> dict:
        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            self.require_current(connection, user, admin=True)
            current = (
                self.owned(connection, t.agents, user, agent_id)
                if agent_id
                else {
                    "id": uid(),
                    "tenant_id": user["tenant_id"],
                    "created_at": now(),
                    "published_version": 0,
                }
            )
            result = {**current, **values}
            model = self.owned(connection, t.models, user, result["model_id"])
            if result["max_tokens"] > model["max_output_tokens"]:
                raise PlatformError(
                    400, "output_limit_exceeded", "Agent 输出上限超过模型配置。"
                )
            if agent_id:
                connection.execute(
                    update(t.agents).where(t.agents.c.id == agent_id).values(**result)
                )
            else:
                connection.execute(insert(t.agents).values(**result))
            audit(
                connection,
                user,
                "agent.update" if agent_id else "agent.create",
                result["id"],
            )
        return result

    def publish(self, user: dict, agent_id: str) -> dict:
        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            self.require_current(connection, user, admin=True)
            agent = self.owned(connection, t.agents, user, agent_id)
            model = self.owned(connection, t.models, user, agent["model_id"])
            if not model["active"]:
                raise PlatformError(
                    400, "model_disabled", "请先启用 Agent 使用的模型。"
                )
            version = agent["published_version"] + 1
            connection.execute(
                insert(t.agent_versions).values(
                    agent_id=agent_id,
                    version=version,
                    tenant_id=user["tenant_id"],
                    spec=agent,
                    created_at=now(),
                )
            )
            connection.execute(
                update(t.agents)
                .where(t.agents.c.id == agent_id)
                .values(published_version=version)
            )
            audit(connection, user, "agent.publish", f"{agent_id}:{version}")
        return {"version": version}

    def create_session(self, user: dict, agent_id: str, title: str | None) -> dict:
        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            self.require_current(connection, user)
            agent = self.owned(connection, t.agents, user, agent_id)
            if not agent["published_version"]:
                raise PlatformError(400, "agent_not_published", "请先发布 Agent。")
            result = {
                "id": uid(),
                "tenant_id": user["tenant_id"],
                "user_id": user["id"],
                "agent_id": agent_id,
                "title": title or "新会话",
                "created_at": now(),
            }
            connection.execute(insert(t.sessions).values(**result))
        return result

    def session_detail(self, user: dict, session_id: str) -> dict:
        with self.db.read() as connection:
            result = self.owned(connection, t.sessions, user, session_id, own=True)
            result["messages"] = [
                dict(row)
                for row in connection.execute(
                    select(t.messages)
                    .where(t.messages.c.session_id == session_id)
                    .order_by(t.messages.c.created_at)
                ).mappings()
            ]
            result["runs"] = [
                self.public_run(dict(row))
                for row in connection.execute(
                    select(t.runs)
                    .where(t.runs.c.session_id == session_id)
                    .order_by(t.runs.c.created_at.desc())
                ).mappings()
            ]
        return result

    def enqueue(
        self, user: dict, session_id: str, message: str, idempotency_key: str
    ) -> dict:
        fingerprint = digest(
            json.dumps({"session_id": session_id, "message": message}, sort_keys=True)
        )
        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            self.require_current(connection, user)
            session = self.owned(connection, t.sessions, user, session_id, own=True)
            # Admin can inspect other members' sessions, but cannot impersonate them.
            if session["user_id"] != user["id"]:
                raise PlatformError(
                    403, "session_owner_required", "只能在自己的会话中发起运行。"
                )
            previous = (
                connection.execute(
                    select(t.runs).where(
                        t.runs.c.tenant_id == user["tenant_id"],
                        t.runs.c.user_id == user["id"],
                        t.runs.c.idempotency_key == idempotency_key,
                    )
                )
                .mappings()
                .first()
            )
            if previous:
                if previous["request_hash"] != fingerprint:
                    raise PlatformError(
                        409, "idempotency_conflict", "同一幂等键不能用于不同的请求。"
                    )
                return self.public_run(dict(previous))
            if connection.scalar(
                select(func.count())
                .select_from(t.runs)
                .where(
                    t.runs.c.session_id == session_id,
                    t.runs.c.status.in_(ACTIVE_STATUSES),
                )
            ):
                raise PlatformError(
                    409, "session_busy", "会话中已有运行，请等待完成或取消。"
                )
            if (
                connection.scalar(
                    select(func.count())
                    .select_from(t.runs)
                    .where(
                        t.runs.c.tenant_id == user["tenant_id"],
                        t.runs.c.status.in_(ACTIVE_STATUSES),
                    )
                )
                >= 32
            ):
                raise PlatformError(429, "queue_full", "组织运行队列已满，请稍后再试。")
            agent = self.owned(connection, t.agents, user, session["agent_id"])
            version = (
                connection.execute(
                    select(t.agent_versions).where(
                        t.agent_versions.c.agent_id == agent["id"],
                        t.agent_versions.c.version == agent["published_version"],
                    )
                )
                .mappings()
                .first()
            )
            if not version:
                raise PlatformError(400, "agent_not_published", "Agent 尚未发布。")
            spec = dict(version["spec"], version=version["version"])
            model = self.owned(connection, t.models, user, spec["model_id"])
            if not model["active"]:
                raise PlatformError(400, "model_disabled", "该模型已停用。")
            # Freeze model identity and rate card for every call of this run.
            spec["model"] = model
            result = {
                "id": uid(),
                "tenant_id": user["tenant_id"],
                "user_id": user["id"],
                "created_at": now(),
                "session_id": session_id,
                "agent_name": agent["name"],
                "spec": spec,
                "message": message,
                "status": "queued",
                "error": "",
                "idempotency_key": idempotency_key,
                "request_hash": fingerprint,
                "cancel_requested": False,
                "fence": 0,
            }
            connection.execute(insert(t.runs).values(**result))
            connection.execute(
                insert(t.messages).values(
                    id=uid(),
                    tenant_id=user["tenant_id"],
                    created_at=now(),
                    session_id=session_id,
                    run_id=result["id"],
                    role="user",
                    content=message,
                )
            )
            if session["title"] == "新会话":
                connection.execute(
                    update(t.sessions)
                    .where(t.sessions.c.id == session_id)
                    .values(title=message[:60])
                )
            append_event(connection, result["id"], "run.queued", {"status": "queued"})
        return self.public_run(result)

    @staticmethod
    def public_run(row: dict) -> dict:
        return {
            key: row.get(key)
            for key in (
                "id",
                "tenant_id",
                "session_id",
                "user_id",
                "agent_name",
                "created_at",
                "status",
                "error",
                "finished_at",
            )
        }

    def get_run(self, user: dict, run_id: str) -> dict:
        with self.db.read() as connection:
            run = self.owned(connection, t.runs, user, run_id, own=True)
            result = self.public_run(run)
            costs = self.call_records(user, run_id=run_id, limit=None)
            result["cost"] = str(sum((Decimal(x["cost"]) for x in costs), Decimal(0)))
            result["message"] = run["message"]
        return result

    def call_records(
        self, user: dict, *, run_id: str | None = None, limit: int | None = 200
    ) -> list[dict]:
        """Financial facts override a possibly stale worker display projection."""
        query = select(t.calls).where(t.calls.c.tenant_id == user["tenant_id"])
        if user["role"] != "admin":
            query = query.where(t.calls.c.user_id == user["id"])
        if run_id:
            query = query.where(t.calls.c.run_id == run_id)
        query = query.order_by(t.calls.c.created_at.desc())
        if limit:
            query = query.limit(limit)
        with self.db.read() as connection:
            rows = [dict(row) for row in connection.execute(query).mappings()]
            if rows:
                evidence = {
                    row["call_id"]: row
                    for row in connection.execute(
                        select(reservations_table).where(
                            reservations_table.c.tenant_id == user["tenant_id"],
                            reservations_table.c.call_id.in_(
                                [row["id"] for row in rows]
                            ),
                        )
                    ).mappings()
                }
                for row in rows:
                    if receipt := evidence.get(row["id"]):
                        row.update(
                            status={"settled": "confirmed", "reserved": "running"}.get(
                                receipt["status"], receipt["status"]
                            ),
                            cost=str(receipt["cost"]),
                            input_tokens=receipt["input_tokens"] or 0,
                            output_tokens=receipt["output_tokens"] or 0,
                            provider_cost=str(receipt["provider_cost"])
                            if receipt["provider_cost"] is not None
                            else None,
                        )
                    row["usage_source"] = "litellm_gateway"
                    row.pop("raw_usage", None)
        return rows

    def cancel_run(self, user: dict, run_id: str) -> dict:
        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            self.require_current(connection, user)
            run = self.owned(connection, t.runs, user, run_id, own=True)
            if run["status"] not in TERMINAL_STATUSES:
                status = "cancelled" if run["status"] == "queued" else "cancelling"
                changes = {"cancel_requested": True, "status": status}
                if status == "cancelled":
                    changes["finished_at"] = now()
                connection.execute(
                    update(t.runs).where(t.runs.c.id == run_id).values(**changes)
                )
                append_event(
                    connection,
                    run_id,
                    "run.cancelled" if status == "cancelled" else "run.cancelling",
                    {"status": status},
                )
                audit(connection, user, "run.cancel", run_id)
        return self.get_run(user, run_id)

    def events(self, user: dict, run_id: str, after: int) -> list[dict]:
        with self.db.read() as connection:
            self.owned(connection, t.runs, user, run_id, own=True)
            return [
                dict(row)
                for row in connection.execute(
                    select(t.run_events)
                    .where(
                        t.run_events.c.run_id == run_id, t.run_events.c.sequence > after
                    )
                    .order_by(t.run_events.c.sequence)
                    .limit(200)
                ).mappings()
            ]

    def dashboard(self, user: dict) -> dict:
        rows = self.call_records(user, limit=None)
        with self.db.read() as connection:
            active_filter = [
                t.runs.c.tenant_id == user["tenant_id"],
                t.runs.c.status.in_(ACTIVE_STATUSES),
            ]
            if user["role"] != "admin":
                active_filter.append(t.runs.c.user_id == user["id"])
            active = connection.scalar(
                select(func.count()).select_from(t.runs).where(and_(*active_filter))
            )
        daily, model_stats = {}, {}
        for row in rows:
            # Stored UTC; reporting day is explicitly Asia/Shanghai (UTC+08:00).
            from datetime import timedelta, timezone

            day = (
                datetime.fromisoformat(row["created_at"])
                .astimezone(timezone(timedelta(hours=8)))
                .date()
                .isoformat()
            )
            for target, key, field in (
                (daily, day, "date"),
                (model_stats, row["model"], "model"),
            ):
                value = target.setdefault(
                    key, {field: key, "requests": 0, "tokens": 0, "cost": Decimal(0)}
                )
                value["requests"] += 1
                value["tokens"] += row["input_tokens"] + row["output_tokens"]
                value["cost"] += Decimal(row["cost"])
        for value in [*daily.values(), *model_stats.values()]:
            value["cost"] = str(value["cost"])
        return {
            **self.billing.wallet(user["tenant_id"]),
            "requests": len(rows),
            "tokens": sum(x["input_tokens"] + x["output_tokens"] for x in rows),
            "cost": str(sum((Decimal(x["cost"]) for x in rows), Decimal(0))),
            "success_rate": round(
                sum(x["status"] == "confirmed" for x in rows) / len(rows) * 100, 1
            )
            if rows
            else 0,
            "active_runs": active,
            "daily": sorted(daily.values(), key=lambda x: x["date"])[-30:],
            "models": list(model_stats.values()),
            "gateway_configured": bool(
                self.settings.litellm_url and self.settings.litellm_key
            ),
        }
