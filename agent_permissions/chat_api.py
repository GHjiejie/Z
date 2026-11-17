"""OpenAI Chat Completions facade over a permission-scoped business Agent."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from typing import Literal
from uuid import uuid4

import httpx
import openai
from fastapi import FastAPI, Request
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langgraph.errors import GraphRecursionError
from pydantic import ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, StreamingResponse

from agent_permissions.chat_model import create_chat_model
from agent_permissions.errors import ApiError
from agent_permissions.models import StrictModel
from agent_permissions.tooling import execution_context

logger = logging.getLogger(__name__)
CHAT_METADATA = {
    "x-agent": {
        "expose": False,
        "reason": "Agent chat entry point; excluded to prevent recursive tool calls",
    }
}
SYSTEM_PROMPT = """"""


class TextPart(StrictModel):
    type: Literal["text"]
    text: str


class ChatMessage(StrictModel):
    role: Literal["system", "developer", "user", "assistant"]
    content: str | list[TextPart]
    name: str | None = Field(default=None, max_length=64)


class StreamOptions(StrictModel):
    include_usage: bool = False


class ChatRequest(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    model: str = Field(min_length=1, max_length=200)
    messages: list[ChatMessage] = Field(min_length=1, max_length=128)
    stream: bool = False
    stream_options: StreamOptions | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1, le=32768)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=32768)
    n: Literal[1] = 1
    tool_choice: Literal["auto", "none"] = "auto"

    @model_validator(mode="after")
    def coherent_options(self) -> "ChatRequest":
        if self.max_tokens is not None and self.max_completion_tokens is not None:
            raise ValueError("max_tokens 和 max_completion_tokens 只能指定一个")
        if self.stream_options is not None and not self.stream:
            raise ValueError("stream_options 仅用于 stream=true")
        return self

    def agent_messages(self) -> list[dict]:
        return [
            {
                "role": "system" if item.role == "developer" else item.role,
                "content": item.content
                if isinstance(item.content, str)
                else "".join(part.text for part in item.content),
                **({"name": item.name} if item.name else {}),
            }
            for item in self.messages
        ]


def compatible_error(error: ApiError, request_id: str) -> JSONResponse:
    status = 400 if error.status == 422 else error.status
    kind = {
        401: "authentication_error",
        403: "permission_error",
        429: "rate_limit_error",
    }.get(status, "server_error" if status >= 500 else "invalid_request_error")
    param = error.details[0]["field"].removeprefix("body.") if error.details else None
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": error.message,
                "type": kind,
                "param": param,
                "code": error.code,
            }
        },
        headers={
            "X-Request-ID": request_id,
            "Cache-Control": "no-store",
            **error.headers,
            **({"WWW-Authenticate": "Bearer"} if status == 401 else {}),
        },
    )


def _model_error(error: Exception) -> ApiError:
    if isinstance(error, ApiError):
        return error
    if isinstance(error, (TimeoutError, openai.APITimeoutError)):
        return ApiError(
            504, "chat_timeout", "任务超时，部分操作可能已执行，请先核对结果"
        )
    if isinstance(error, GraphRecursionError):
        return ApiError(
            500,
            "agent_step_limit",
            "任务步骤超过限制，部分操作可能已执行，请先核对结果",
        )
    if isinstance(error, openai.RateLimitError):
        return ApiError(429, "model_rate_limit", "模型服务暂时繁忙，请稍后重试")
    if isinstance(error, (openai.APIError, httpx.HTTPError)):
        return ApiError(
            502, "model_unavailable", "模型服务暂时不可用，请凭请求标识排查"
        )
    return ApiError(
        500, "chat_internal_error", "任务未能正常完成，请凭请求标识核对结果"
    )


async def _cached_model(app: FastAPI) -> BaseChatModel:
    async with app.state.chat_model_lock:
        if app.state.chat_model is None:
            try:
                app.state.chat_model = await run_in_threadpool(
                    app.state.chat_model_factory
                )
            except (ValueError, OSError, openai.OpenAIError):
                raise ApiError(
                    503, "model_not_configured", "模型配置暂不可用，请联系管理员"
                ) from None
        return app.state.chat_model


async def close_chat_model(app: FastAPI) -> None:
    model = getattr(app.state, "chat_model", None)
    if model is not None:
        try:
            if getattr(model, "root_async_client", None) is not None:
                await model.root_async_client.close()
        finally:
            if getattr(model, "root_client", None) is not None:
                await run_in_threadpool(model.root_client.close)
            app.state.chat_model = None


async def _authenticated(request: Request):
    context = await execution_context(request)
    if not context.authenticated:
        raise ApiError(401, "UNAUTHENTICATED", "请使用登录接口返回的会话令牌")
    return context


def _finish(message: AIMessage) -> str:
    if (
        message.response_metadata.get("finish_reason") == "length"
        or message.response_metadata.get("status") == "incomplete"
    ):
        return "length"
    return "stop"


def _sse(value: dict) -> str:
    return "data: " + json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n\n"


async def _stream(
    request: Request, payload: ChatRequest, model: BaseChatModel, identity: dict
) -> AsyncIterator[str]:
    def chunk(delta: dict, finish: str | None = None) -> dict:
        return {
            **identity,
            "object": "chat.completion.chunk",
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish, "logprobs": None}
            ],
        }

    try:
        from agent_permissions.hitl_api import new_run
        from agent_permissions.hitl_runtime import run_events

        yield _sse(chunk({"role": "assistant", "content": ""}))
        run, fresh = await new_run(request, payload)
        final = None
        async with aclosing(run_events(request, run, model, fresh=fresh)) as events:
            async for event in events:
                if event["type"] == "text.delta":
                    yield _sse(chunk({"content": event["text"]}))
                if "run" in event:
                    final = event["run"]
                if event["type"] == "run.waiting":
                    yield _sse(chunk({"content": _waiting_text(final)}))
        _check_run_result(final)
        yield _sse(chunk({}, final.get("finish", "stop")))
        if (
            payload.stream_options
            and payload.stream_options.include_usage
            and final.get("usage")
        ):
            yield _sse(
                {
                    **identity,
                    "object": "chat.completion.chunk",
                    "choices": [],
                    "usage": final["usage"],
                }
            )
        yield "data: [DONE]\n\n"
    except Exception as error:  # noqa: BLE001 -- redact all errors after SSE headers have been sent
        safe = _model_error(error)
        logger.warning(
            "Chat stream failed request_id=%s reason=%s",
            request.state.info.request_id,
            safe.code,
        )
        response = compatible_error(safe, request.state.info.request_id)
        yield "data: " + response.body.decode() + "\n\n"


def _waiting_text(run: dict) -> str:
    title = (run.get("interaction") or {}).get("title", "业务操作")
    return f"\n{title}需要补充参数或确认，尚未执行。请在 Console 会话页面处理待办，任务标识：{run['id']}。"


def _check_run_result(run: dict | None) -> None:
    if run is None:
        raise ApiError(502, "agent_incomplete", "任务未正常结束，请核对结果")
    if run["status"] not in {"completed", "waiting"}:
        error = run.get("error") or {}
        raise ApiError(
            error.get("http_status", 500),
            error.get("code", "agent_incomplete"),
            error.get("message", "任务未正常结束，请核对结果"),
        )


def mount_chat_api(
    app: FastAPI, model_factory: Callable[[], BaseChatModel] | None = None
) -> None:
    app.state.chat_model_factory = model_factory or create_chat_model
    app.state.chat_model = None
    app.state.chat_model_lock = asyncio.Lock()

    @app.get("/v1/models", name="chat.models", openapi_extra=CHAT_METADATA)
    async def list_models(request: Request) -> dict:
        await _authenticated(request)
        model = await _cached_model(app)
        return {
            "object": "list",
            "data": [
                {
                    "id": model.model_name,
                    "object": "model",
                    "created": 0,
                    "owned_by": "agent-permissions",
                }
            ],
        }

    @app.post(
        "/v1/chat/completions",
        name="chat.completions",
        openapi_extra=CHAT_METADATA,
        response_model=None,
    )
    async def completions(payload: ChatRequest, request: Request):
        await _authenticated(request)
        model = await _cached_model(app)
        if payload.model != model.model_name:
            raise ApiError(
                404,
                "model_not_found",
                "请求的模型未在本服务配置",
                details=[{"field": "model", "message": "请选择 /v1/models 返回的模型"}],
            )
        identity = {
            "id": "chatcmpl-" + uuid4().hex,
            "created": int(app.state.service.clock().timestamp()),
            "model": payload.model,
        }
        if payload.stream:
            return StreamingResponse(
                _stream(request, payload, model, identity),
                media_type="text/event-stream",
                headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
            )
        try:
            from agent_permissions.hitl_api import new_run
            from agent_permissions.hitl_runtime import run_events

            run, fresh = await new_run(request, payload)
            final = None
            async for event in run_events(
                request, run, model, fresh=fresh, streaming=False
            ):
                if "run" in event:
                    final = event["run"]
            _check_run_result(final)
            return {
                **identity,
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": final["content"]
                            + (
                                _waiting_text(final)
                                if final["status"] == "waiting"
                                else ""
                            ),
                            "refusal": None,
                        },
                        "finish_reason": final.get("finish", "stop"),
                        "logprobs": None,
                    }
                ],
                **({"usage": final["usage"]} if final.get("usage") else {}),
            }
        except Exception as error:  # noqa: BLE001 -- provider exceptions can contain credentials and raw requests
            safe = _model_error(error)
            logger.warning(
                "Chat failed request_id=%s reason=%s",
                request.state.info.request_id,
                safe.code,
            )
            raise safe from None
