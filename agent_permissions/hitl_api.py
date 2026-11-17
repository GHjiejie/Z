"""Authenticated, owner-scoped HTTP facade for interactive Agent runs."""

import json
from contextlib import aclosing
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import FastAPI, Query, Request
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from agent_permissions.agui import encode_agui, mount_agui_api
from agent_permissions.chat_api import (
    CHAT_METADATA,
    ChatRequest,
    _authenticated,
    _cached_model,
)
from agent_permissions.errors import ApiError
from agent_permissions.hitl_forms import (
    apply_values,
    check_target,
    field_options,
    prepare_form,
    requires_confirmation,
    safe_arguments,
    target_path,
    validate_arguments,
)
from agent_permissions.hitl_runtime import http_client, require_tool, run_events
from agent_permissions.hitl_store import GRAPH_VERSION, RunStore
from agent_permissions.models import StrictModel


class RunRequest(ChatRequest):
    client_request_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=128)
    stream: Literal[True] = True


class InteractionSubmission(StrictModel):
    submission_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(strict=True, ge=1)
    decision: Literal["submit", "cancel"] = "submit"
    values: dict[str, Any] = Field(default_factory=dict, max_length=128)
    sensitive_values: dict[str, Any] = Field(
        default_factory=dict, max_length=32, repr=False
    )
    omit: list[str] = Field(default_factory=list, max_length=128)


class RefreshRequest(StrictModel):
    expected_version: int = Field(strict=True, ge=1)
    request_id: str = Field(min_length=1, max_length=128)


async def owner(request: Request) -> str:
    await _authenticated(request)
    return request.state.info.actor_user_id


async def new_run(
    request: Request,
    payload: ChatRequest,
    *,
    client_request_id: str | None = None,
    conversation_id: str | None = None,
) -> tuple[dict, bool]:
    user_id = await owner(request)
    model = await _cached_model(request.app)
    if payload.model != model.model_name:
        raise ApiError(404, "model_not_found", "请求的模型未在本服务配置")
    data = payload.model_dump(
        exclude={"client_request_id", "conversation_id", "stream", "stream_options"}
    )
    data["messages"] = payload.agent_messages()
    return await run_in_threadpool(
        request.app.state.run_store.create,
        user_id,
        conversation_id or "sdk_" + uuid4().hex,
        client_request_id or "sdk_" + uuid4().hex,
        data,
    )


async def event_response(
    request: Request,
    run: dict,
    *,
    fresh: bool,
    resume: dict | None = None,
    sensitive: dict | None = None,
    recovering: bool = False,
) -> StreamingResponse:
    model = await _cached_model(request.app)

    async def body():
        if getattr(request.state, "agui", None) is not None:
            async with (
                aclosing(
                    run_events(
                        request,
                        run,
                        model,
                        fresh=fresh,
                        resume=resume,
                        sensitive=sensitive,
                        recovering=recovering,
                    )
                ) as events,
                aclosing(encode_agui(events, request.state.agui)) as encoded,
            ):
                async for frame in encoded:
                    yield frame
            return
        async with aclosing(
            run_events(
                request,
                run,
                model,
                fresh=fresh,
                resume=resume,
                sensitive=sensitive,
                recovering=recovering,
            )
        ) as events:
            async for event in events:
                yield (
                    "event: "
                    + event["type"]
                    + "\ndata: "
                    + json.dumps(event, ensure_ascii=False, allow_nan=False)
                    + "\n\n"
                )

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def envelope(request: Request, value: Any) -> dict:
    return {"data": value, "request_id": request.state.info.request_id}


def mount_hitl_api(app: FastAPI) -> None:
    store = RunStore(app.state.database, app.state.settings, app.state.service.clock)
    app.state.run_store = store

    @app.post("/api/v1/chat/runs", name="chat.runs.create", openapi_extra=CHAT_METADATA)
    async def create_run(payload: RunRequest, request: Request):
        run, fresh = await new_run(
            request,
            payload,
            client_request_id=payload.client_request_id,
            conversation_id=payload.conversation_id,
        )
        return await event_response(request, run, fresh=fresh)

    @app.get("/api/v1/chat/runs", name="chat.runs.list", openapi_extra=CHAT_METADATA)
    async def list_runs(
        request: Request,
        conversation_id: Annotated[str, Query(min_length=1, max_length=128)],
    ):
        user_id = await owner(request)
        ids = await run_in_threadpool(store.list_runs, user_id, conversation_id)
        return envelope(
            request,
            [
                await run_in_threadpool(store.snapshot, run_id, user_id)
                for run_id in ids
            ],
        )

    @app.get(
        "/api/v1/chat/runs/{run_id}", name="chat.runs.read", openapi_extra=CHAT_METADATA
    )
    async def get_run(run_id: str, request: Request):
        return envelope(
            request,
            await run_in_threadpool(store.snapshot, run_id, await owner(request)),
        )

    @app.post(
        "/api/v1/chat/runs/{run_id}/cancel",
        name="chat.runs.cancel",
        openapi_extra=CHAT_METADATA,
    )
    async def cancel_run(run_id: str, request: Request):
        return envelope(
            request, await run_in_threadpool(store.cancel, run_id, await owner(request))
        )

    @app.post(
        "/api/v1/chat/runs/{run_id}/recover",
        name="chat.runs.recover",
        openapi_extra=CHAT_METADATA,
    )
    async def recover_run(run_id: str, request: Request):
        await _cached_model(request.app)
        run, fresh = await run_in_threadpool(
            store.recover, run_id, await owner(request)
        )
        return await event_response(request, run, fresh=fresh, recovering=True)

    @app.get(
        "/api/v1/chat/runs/{run_id}/interactions/{interaction_id}/options",
        name="chat.interactions.options",
        openapi_extra=CHAT_METADATA,
    )
    async def options(
        run_id: str,
        interaction_id: str,
        request: Request,
        field: Annotated[str, Query(max_length=200)],
        q: Annotated[str, Query(max_length=100)] = "",
    ):
        item = await run_in_threadpool(
            store.interaction, run_id, interaction_id, await owner(request)
        )
        await require_tool(request, item["operation_id"], item["catalog_revision"])
        document = json.loads(item["document"])
        selected = next(
            (
                value
                for value in document.get("fields", [])
                if value["pointer"] == field
            ),
            None,
        )
        if not selected or selected["source"] not in {"users", "roles", "permissions"}:
            raise ApiError(422, "VALIDATION_ERROR", "字段没有可用的选项来源")
        async with http_client(request) as http:
            return envelope(
                request,
                await field_options(
                    selected["source"], http, request.headers["Authorization"], q
                ),
            )

    @app.post(
        "/api/v1/chat/runs/{run_id}/interactions/{interaction_id}/refresh",
        name="chat.interactions.refresh",
        openapi_extra=CHAT_METADATA,
    )
    async def refresh(
        run_id: str, interaction_id: str, payload: RefreshRequest, request: Request
    ):
        user_id = await owner(request)
        run = await run_in_threadpool(store.raw, run_id, user_id)
        if run["graph_version"] != GRAPH_VERSION:
            raise ApiError(409, "RUN_VERSION_MISMATCH", "任务版本已变化，请重新发起")
        item = await run_in_threadpool(
            store.interaction, run_id, interaction_id, user_id
        )
        fingerprint = store.digest({"refresh_version": payload.expected_version})
        if item["submission_id"] == payload.request_id:
            if item["submission_digest"] != fingerprint:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "请求标识已用于不同参数")
            return envelope(
                request, await run_in_threadpool(store.snapshot, run_id, user_id)
            )
        if item["version"] != payload.expected_version or item["status"] != "pending":
            raise ApiError(
                412, "INTERACTION_VERSION_CONFLICT", "操作已变化，请载入最新状态"
            )
        snapshot, tool = await require_tool(request, item["operation_id"])
        async with http_client(request) as http:
            document = await prepare_form(
                tool,
                json.loads(item["document"])["arguments"],
                http,
                request.headers["Authorization"],
            )
        await run_in_threadpool(
            store.prepare,
            run,
            item["call_key"],
            item["tool_call_id"],
            item["operation_id"],
            snapshot["bundleDigest"],
            document,
            replace=item,
            request_id=payload.request_id,
            request_digest=fingerprint,
        )
        return envelope(
            request, await run_in_threadpool(store.snapshot, run_id, user_id)
        )

    @app.post(
        "/api/v1/chat/runs/{run_id}/interactions/{interaction_id}/resume",
        name="chat.interactions.resume",
        openapi_extra=CHAT_METADATA,
    )
    async def resume(
        run_id: str,
        interaction_id: str,
        payload: InteractionSubmission,
        request: Request,
    ):
        user_id = await owner(request)
        run = await run_in_threadpool(store.raw, run_id, user_id)
        if run["graph_version"] != GRAPH_VERSION:
            raise ApiError(409, "RUN_VERSION_MISMATCH", "任务版本已变化，请重新发起")
        item = await run_in_threadpool(
            store.interaction, run_id, interaction_id, user_id
        )
        if payload.decision == "cancel":
            if payload.values or payload.sensitive_values or payload.omit:
                raise ApiError(422, "VALIDATION_ERROR", "取消操作不接受参数")
            await run_in_threadpool(store.cancel, run_id, user_id)
            return await event_response(request, run, fresh=False)
        # Check retries before reading targets that a successful call may have changed or deleted.
        fingerprint = store.digest(
            payload.model_dump(exclude={"submission_id", "expected_version"})
        )
        if item["submission_id"] == payload.submission_id:
            if item["submission_digest"] != fingerprint:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "提交标识已用于不同参数")
            return await event_response(request, run, fresh=False)
        if item["status"] == "expired" or run["purged"]:
            raise ApiError(410, "INTERACTION_EXPIRED", "待处理操作已过期")
        if item["status"] != "pending":
            raise ApiError(409, "INTERACTION_ALREADY_RESOLVED", "操作已提交或结束")
        if item["version"] != payload.expected_version:
            raise ApiError(
                412, "INTERACTION_VERSION_CONFLICT", "操作已变化，请重新读取"
            )
        snapshot, tool = await require_tool(
            request, item["operation_id"], item["catalog_revision"]
        )
        document = json.loads(item["document"])
        arguments = apply_values(document, payload.values, payload.sensitive_values)
        from agent_permissions.hitl_forms import parts

        fields = {value["pointer"]: value for value in document["fields"]}
        for pointer in payload.omit:
            field = fields.get(pointer)
            if (
                not field
                or not field["editable"]
                or field["required"]
                or field["sensitive"]
            ):
                raise ApiError(422, "VALIDATION_ERROR", "不能省略此字段")
            value = arguments
            path = parts(pointer)
            for name in path[:-1]:
                value = value.get(name, {})
            value.pop(path[-1], None)
        async with http_client(request) as http:
            if (
                requires_confirmation(tool)
                and not document["target"]
                and target_path(item["operation_id"], arguments)
            ):
                # Selecting a target needs a fresh snapshot before approving its mutation.
                prepared = await prepare_form(
                    tool, arguments, http, request.headers["Authorization"]
                )
                await run_in_threadpool(
                    store.prepare,
                    run,
                    item["call_key"],
                    item["tool_call_id"],
                    item["operation_id"],
                    snapshot["bundleDigest"],
                    prepared,
                    replace=item,
                    request_id=payload.submission_id,
                    request_digest=fingerprint,
                )
                return await event_response(request, run, fresh=False)
            validate_arguments(tool, arguments)
            await check_target(
                document,
                item["operation_id"],
                arguments,
                http,
                request.headers["Authorization"],
            )
        run, fresh = await run_in_threadpool(
            store.submit,
            run,
            item,
            payload.submission_id,
            payload.expected_version,
            safe_arguments(tool, arguments),
            fingerprint,
        )
        return await event_response(
            request,
            run,
            fresh=fresh,
            resume={"interaction_id": interaction_id},
            sensitive=payload.sensitive_values,
        )

    mount_agui_api(app, start=create_run, resume=resume, recover=recover_run)
