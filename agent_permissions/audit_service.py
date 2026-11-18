"""Private audit collector and public RBAC-protected query gateway."""

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field, ValidationError
from starlette.concurrency import run_in_threadpool

from agent_permissions.audit_contract import Batch, Event, Identifier, Strict
from agent_permissions.audit_delivery import PREFIX, audit_store, validate_credentials
from agent_permissions.audit_store import GrantQuery, OperationQuery
from agent_permissions.config import Settings
from agent_permissions.errors import ApiError

logger = logging.getLogger(__name__)


class Incoming(Strict):
    schema_version: Literal[1]
    producer_id: Identifier
    events: list[dict] = Field(min_length=1, max_length=100)


def create_audit_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    validate_credentials(settings)
    store = audit_store(settings)

    async def maintain():
        while True:
            await asyncio.sleep(3600)
            if settings.audit_store_mode == "managed":
                continue
            try:
                await run_in_threadpool(
                    store.cleanup, datetime.now(UTC), settings.audit_retention_days
                )
            except Exception:  # noqa: BLE001 -- retry maintenance without logging storage details
                logger.error("Audit retention unavailable")

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(
            store.initialize, managed=settings.audit_store_mode == "managed"
        )
        task = asyncio.create_task(maintain())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await run_in_threadpool(store.database.close)

    app = FastAPI(title="Agent 操作审计服务", lifespan=lifespan)
    app.state.store = store

    def authenticate(request, token):
        actual = request.headers.get("authorization", "").encode()
        if not hmac.compare_digest(actual, ("Bearer " + token).encode()):
            raise ApiError(401, "UNAUTHENTICATED", "无效的服务身份")

    def writer(request: Request):
        authenticate(request, settings.audit_write_token)

    def reader(request: Request):
        authenticate(request, settings.audit_read_token)

    @app.exception_handler(ApiError)
    async def error(request, exc):
        return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return JSONResponse(
            {"error": {"code": "INVALID_AUDIT_REQUEST"}}, status_code=422
        )

    @app.post(PREFIX + "/events", dependencies=[Depends(writer)])
    def ingest(payload: Incoming):
        if payload.producer_id != settings.audit_producer_id:
            raise ApiError(403, "PRODUCER_DENIED", "服务身份与生产方不匹配")
        valid, acks = [], []
        for index, raw in enumerate(payload.events):
            try:
                valid.append(Event.model_validate(raw))
            except ValidationError:
                # Never echo invalid user-provided strings into responses or logs.
                acks.append({"index": index, "status": "rejected"})
        if valid:
            acks.extend(
                store.receive(
                    Batch(
                        schema_version=1, producer_id=payload.producer_id, events=valid
                    )
                )
            )
        return {"acknowledgements": acks}

    @app.get(PREFIX + "/operations", dependencies=[Depends(reader)])
    def operations(query: Annotated[OperationQuery, Query()]):
        return store.page(query)

    @app.get(PREFIX + "/operations/{call_id}", dependencies=[Depends(reader)])
    def operation(call_id: Identifier):
        return store.detail(call_id)

    @app.get(PREFIX + "/grant-changes", dependencies=[Depends(reader)])
    def grants(query: Annotated[GrantQuery, Query()]):
        return store.page(query)

    return app


def mount_audit_api(app: FastAPI):
    from agent_permissions.api import current_authority, result
    from agent_permissions.tooling import CONTROL_METADATA

    def authorize(
        request: Request, identity: Annotated[object, Depends(current_authority)]
    ):
        app.state.service.authorize_audit(identity, request.state.info)

    options = {"dependencies": [Depends(authorize)], "openapi_extra": CONTROL_METADATA}

    @app.get("/api/v1/audit/agent-operations", name="audit.operations.list", **options)
    async def operations(request: Request, query: Annotated[OperationQuery, Query()]):
        return result(
            request.state.info, await app.state.audit.read("/operations", query)
        )

    @app.get(
        "/api/v1/audit/agent-operations/{call_id}",
        name="audit.operations.detail",
        **options,
    )
    async def operation(request: Request, call_id: Identifier):
        return result(
            request.state.info,
            await app.state.audit.read("/operations/" + call_id, call_id=call_id),
        )

    @app.get("/api/v1/audit/agent-grant-changes", name="audit.grants.list", **options)
    async def grants(request: Request, query: Annotated[GrantQuery, Query()]):
        return result(
            request.state.info, await app.state.audit.read("/grant-changes", query)
        )
