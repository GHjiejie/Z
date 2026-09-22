"""Same-origin FastAPI application, persistent SSE and React static hosting."""

# FastAPI inspects these declarative dependency defaults; they are not executed
# as mutable application state.
# ruff: noqa: B008

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy import delete, select, text
from starlette.middleware.base import BaseHTTPMiddleware

from agent_platform.apps.api import schemas as s
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.db import Database
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.billing.service import BillingService
from agent_platform.modules.platform import (
    TERMINAL_STATUSES,
    Platform,
    audit,
    digest,
    public_user,
)

logger = logging.getLogger(__name__)
COOKIE = "agent_platform_session"


class BoundaryMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse(
                    {
                        "error": {
                            "code": "invalid_origin",
                            "message": "请求来源不匹配。",
                        }
                    },
                    status_code=403,
                )
            try:
                if int(request.headers.get("content-length", "0")) > 256000:
                    return JSONResponse(
                        {
                            "error": {
                                "code": "body_too_large",
                                "message": "请求内容过大。",
                            }
                        },
                        status_code=413,
                    )
            except ValueError:
                return JSONResponse(
                    {"error": {"code": "invalid_length", "message": "请求无效。"}},
                    status_code=400,
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response


def create_app(settings: Settings | None = None, *, gateway=None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.database_url)
    platform = Platform(db, settings)

    @asynccontextmanager
    async def lifespan(app):
        db.create_schema()
        platform.bootstrap()
        stop = asyncio.Event()
        task = None
        if settings.embedded_worker:
            from agent_platform.apps.worker.main import Worker

            task = asyncio.create_task(Worker(platform, gateway=gateway).serve(stop))
        yield
        stop.set()
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        db.close()

    app = FastAPI(title="Agent Platform", version="0.1.0", lifespan=lifespan)
    app.state.platform = platform
    app.state.database = db
    app.add_middleware(BoundaryMiddleware)

    @app.exception_handler(PlatformError)
    async def platform_error(_request, exc):
        headers = {"Retry-After": "60"} if exc.status == 429 else None
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message}},
            status_code=exc.status,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc):
        # Never echo password/request payloads in validation responses.
        details = [
            {"field": ".".join(str(x) for x in e["loc"]), "message": e["msg"]}
            for e in exc.errors()
        ]
        return JSONResponse(
            {
                "error": {
                    "code": "validation_error",
                    "message": "请检查输入内容。",
                    "details": details,
                }
            },
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(_request, exc):
        logger.error("platform request failed: %s", type(exc).__name__)
        return JSONResponse(
            {
                "error": {
                    "code": "internal_error",
                    "message": "服务暂时不可用，请重试。",
                }
            },
            status_code=500,
        )

    def auth(request: Request):
        context = platform.authenticate(request.cookies.get(COOKIE))
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            csrf = request.headers.get("x-csrf-token", "")
            if not secrets.compare_digest(csrf, context["csrf_token"]):
                raise PlatformError(403, "csrf_failed", "会话校验失败，请刷新后重试。")
        return context["user"]

    def admin(user=Depends(auth)):
        if user["role"] != "admin":
            raise PlatformError(403, "admin_required", "此操作需要管理员权限。")
        return user

    def billing_as(user, action=None, target=None):
        def authorize(connection):
            platform.require_current(connection, user, admin=True)
            if action:
                audit(connection, user, action, target)

        return BillingService(db, settings.redis_url, authorize=authorize)

    def idempotency(key: str | None) -> str:
        if not key or len(key) > 160:
            raise PlatformError(
                400, "idempotency_required", "请提供有效的 Idempotency-Key。"
            )
        return key

    @app.get("/api/v1/health")
    def health():
        with db.read() as connection:
            connection.execute(text("SELECT 1"))
        return {
            "status": "ok",
            "gateway_configured": bool(settings.litellm_url and settings.litellm_key),
            "rate_limit_storage": "redis+database"
            if settings.redis_url
            else "database",
        }

    @app.post("/api/v1/auth/login")
    def login(body: s.Login, request: Request):
        result = platform.login(
            body.email,
            body.password,
            request.client.host if request.client else "unknown",
        )
        token = result.pop("token")
        response = JSONResponse(result)
        response.set_cookie(
            COOKIE,
            token,
            max_age=settings.session_hours * 3600,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/api/v1/auth/me")
    def me(request: Request, _user=Depends(auth)):
        return platform.authenticate(request.cookies.get(COOKIE))

    @app.post("/api/v1/auth/logout")
    def logout(request: Request, user=Depends(auth)):
        with db.transaction(f"tenant:{user['tenant_id']}") as connection:
            connection.execute(
                delete(t.auth_sessions).where(
                    t.auth_sessions.c.token_hash
                    == digest(request.cookies.get(COOKIE, ""))
                )
            )
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.post("/api/v1/auth/password")
    def password(body: s.PasswordChange, user=Depends(auth)):
        platform.change_password(user, body.current_password, body.new_password)
        return {"ok": True}

    @app.get("/api/v1/dashboard")
    def dashboard(user=Depends(auth)):
        return platform.dashboard(user)

    @app.get("/api/v1/users")
    def users(user=Depends(admin)):
        return {
            "items": [
                public_user(row) for row in platform.list_resources(t.users, user)
            ]
        }

    @app.post("/api/v1/users", status_code=201)
    def create_user(body: s.UserCreate, user=Depends(admin)):
        return platform.create_user(user, body.model_dump())

    @app.patch("/api/v1/users/{user_id}")
    def patch_user(user_id: str, body: s.UserPatch, user=Depends(admin)):
        return platform.update_user(user, user_id, body.model_dump(exclude_none=True))

    @app.get("/api/v1/models")
    def models(user=Depends(auth)):
        rows = platform.list_resources(t.models, user)
        return {
            "items": [
                {**row, "is_default": row["alias"] == settings.default_model}
                for row in rows
            ],
            "default_model": settings.default_model if user["role"] == "admin" else "",
        }

    @app.post("/api/v1/models", status_code=201)
    def create_model(body: s.ModelCreate, user=Depends(admin)):
        return platform.save_model(user, body.model_dump())

    @app.patch("/api/v1/models/{model_id}")
    def patch_model(model_id: str, body: s.ModelPatch, user=Depends(admin)):
        return platform.save_model(user, body.model_dump(exclude_none=True), model_id)

    @app.get("/api/v1/agents")
    def agents(user=Depends(auth)):
        rows = platform.list_resources(t.agents, user)
        if user["role"] != "admin":
            # Members only see the actual published specification, not private drafts.
            published = []
            with db.read() as connection:
                for row in rows:
                    version = (
                        connection.execute(
                            select(t.agent_versions).where(
                                t.agent_versions.c.agent_id == row["id"],
                                t.agent_versions.c.version == row["published_version"],
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if version:
                        published.append(
                            {**version["spec"], "published_version": version["version"]}
                        )
            rows = published
        return {"items": rows}

    @app.post("/api/v1/agents", status_code=201)
    def create_agent(body: s.AgentCreate, user=Depends(admin)):
        return platform.save_agent(user, body.model_dump())

    @app.patch("/api/v1/agents/{agent_id}")
    def patch_agent(agent_id: str, body: s.AgentPatch, user=Depends(admin)):
        return platform.save_agent(user, body.model_dump(exclude_none=True), agent_id)

    @app.post("/api/v1/agents/{agent_id}/publish")
    def publish(agent_id: str, user=Depends(admin)):
        return platform.publish(user, agent_id)

    @app.get("/api/v1/sessions")
    def sessions(user=Depends(auth)):
        # Conversation picker shows only the caller's own sessions, including admins.
        return {
            "items": platform.list_resources(
                t.sessions, {**user, "role": "member"}, own=True
            )
        }

    @app.post("/api/v1/sessions", status_code=201)
    def create_session(body: s.SessionCreate, user=Depends(auth)):
        return platform.create_session(user, body.agent_id, body.title)

    @app.get("/api/v1/sessions/{session_id}")
    def session_detail(session_id: str, user=Depends(auth)):
        return platform.session_detail(user, session_id)

    @app.post("/api/v1/sessions/{session_id}/runs", status_code=202)
    def create_run(
        session_id: str,
        body: s.RunCreate,
        user=Depends(auth),
        idempotency_key: str | None = Header(default=None),
    ):
        if not (settings.litellm_url and settings.litellm_key) and gateway is None:
            raise PlatformError(
                503, "gateway_not_configured", "请先配置 LiteLLM 地址和受限密钥。"
            )
        return platform.enqueue(
            user, session_id, body.message, idempotency(idempotency_key)
        )

    @app.get("/api/v1/runs")
    def runs(user=Depends(auth)):
        return {
            "items": [
                platform.get_run(user, row["id"])
                for row in platform.list_resources(t.runs, user, own=True)
            ]
        }

    @app.get("/api/v1/runs/{run_id}")
    def run(run_id: str, user=Depends(auth)):
        return platform.get_run(user, run_id)

    @app.post("/api/v1/runs/{run_id}/cancel")
    def cancel(run_id: str, user=Depends(auth)):
        return platform.cancel_run(user, run_id)

    @app.get("/api/v1/runs/{run_id}/events")
    async def events(
        run_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
        user=Depends(auth),
    ):
        await asyncio.to_thread(platform.get_run, user, run_id)
        raw_cursor = request.headers.get("last-event-id")
        if raw_cursor:
            try:
                after = max(after, int(raw_cursor))
            except ValueError:
                raise PlatformError(400, "invalid_cursor", "事件游标无效。") from None

        async def stream():
            cursor = after
            idle = 0
            while not await request.is_disconnected():
                try:
                    # Recheck expiry/revocation during long-lived connections.
                    current = await asyncio.to_thread(
                        platform.authenticate, request.cookies.get(COOKIE)
                    )
                    rows = await asyncio.to_thread(
                        platform.events, current["user"], run_id, cursor
                    )
                except PlatformError:
                    return
                for row in rows:
                    cursor = row["sequence"]
                    payload = json.dumps(row, ensure_ascii=False)
                    yield f"id: {cursor}\nevent: {row['type']}\ndata: {payload}\n\n"
                detail = await asyncio.to_thread(
                    platform.get_run, current["user"], run_id
                )
                if detail["status"] in TERMINAL_STATUSES and len(rows) < 200:
                    # Re-read once: final event and status share one transaction.
                    tail = await asyncio.to_thread(
                        platform.events, current["user"], run_id, cursor
                    )
                    for row in tail:
                        yield f"id: {row['sequence']}\nevent: {row['type']}\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
                    return
                idle += 1
                if idle % 40 == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
        )

    @app.get("/api/v1/usage/calls")
    def calls(user=Depends(auth)):
        return {"items": platform.call_records(user)}

    @app.get("/api/v1/billing/wallet")
    def wallet(user=Depends(auth)):
        return platform.billing.wallet(user["tenant_id"])

    @app.get("/api/v1/billing/ledger")
    def ledger(user=Depends(admin)):
        return {"items": platform.billing.ledger(user["tenant_id"])}

    @app.post("/api/v1/billing/credits")
    def credit(
        body: s.Credit,
        user=Depends(admin),
        idempotency_key: str | None = Header(default=None),
    ):
        return billing_as(user).credit(
            user["tenant_id"],
            user["id"],
            body.amount,
            body.description,
            idempotency(idempotency_key),
        )

    @app.get("/api/v1/billing/reservations")
    def reservations(user=Depends(admin)):
        return {"items": platform.billing.reservations(user["tenant_id"])}

    @app.post("/api/v1/billing/reservations/{call_id}/resolve")
    def reconcile(
        call_id: str,
        body: s.Reconcile,
        user=Depends(admin),
        idempotency_key: str | None = Header(default=None),
    ):
        result = billing_as(user).resolve(
            user["tenant_id"],
            call_id,
            actor_id=user["id"],
            idempotency_key=idempotency(idempotency_key),
            **body.model_dump(),
        )
        from agent_platform.apps.worker.main import sync_call_receipt

        sync_call_receipt(platform, result)
        return result

    @app.post("/api/v1/billing/unblock")
    def unblock(body: s.Unblock, user=Depends(admin)):
        return billing_as(user).unblock(user["tenant_id"], user["id"], body.reason)

    @app.get("/api/v1/quotas")
    def quotas(user=Depends(admin)):
        return {"items": platform.billing.quotas(user["tenant_id"])}

    @app.put("/api/v1/quotas")
    def quota(body: s.Quota, user=Depends(admin)):
        if body.scope == "tenant" and body.subject_id != user["tenant_id"]:
            raise PlatformError(400, "invalid_subject", "租户配额必须属于当前组织。")
        if body.scope in ("user", "model"):
            with db.read() as connection:
                platform.owned(
                    connection,
                    t.users if body.scope == "user" else t.models,
                    user,
                    body.subject_id,
                )
        return billing_as(
            user, "quota.update", f"{body.scope}:{body.subject_id}"
        ).set_quota(user["tenant_id"], **body.model_dump())

    @app.get("/api/v1/audit")
    def audits(user=Depends(admin)):
        from agent_platform.modules.billing.service import billing_audit

        rows = platform.list_resources(t.audits, user)
        with db.read() as connection:
            financial = connection.execute(
                select(billing_audit).where(
                    billing_audit.c.tenant_id == user["tenant_id"]
                )
            ).mappings()
            for row in financial:
                data = dict(row)
                data["actor_email"] = (
                    connection.scalar(
                        select(t.users.c.email).where(
                            t.users.c.id == data.get("actor_id")
                        )
                    )
                    or "system"
                )
                rows.append(data)
        return {
            "items": sorted(rows, key=lambda x: x["created_at"], reverse=True)[:200]
        }

    @app.get("/{path:path}", include_in_schema=False)
    def web(path: str):
        if path.startswith("api/"):
            raise PlatformError(404, "not_found", "接口不存在。")
        directory = settings.web_directory.resolve()
        requested = (directory / path).resolve()
        if requested.is_relative_to(directory) and requested.is_file():
            return FileResponse(requested)
        if (directory / "index.html").exists():
            return FileResponse(directory / "index.html")
        return JSONResponse(
            {
                "message": "请先构建 agent_platform/apps/web，或运行前端开发服务器。",
                "api_docs": "/docs",
            }
        )

    return app
