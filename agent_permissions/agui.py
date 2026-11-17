"""AG-UI 1.0 transport over the existing owner-scoped, checkpointed runtime."""

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from ag_ui.core import Event, RunAgentInput
from ag_ui.encoder import EventEncoder
from fastapi import FastAPI, Request
from pydantic import BaseModel, TypeAdapter, ValidationError

from agent_permissions.chat_api import CHAT_METADATA, _cached_model
from agent_permissions.errors import ApiError

EVENTS = TypeAdapter(Event)


def validated[Model: BaseModel](model: type[Model], data: dict) -> Model:
    """Return sanitized HTTP validation errors for application-specific AG-UI input."""
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            "请求参数不符合要求",
            details=[
                {
                    "field": ".".join(str(part) for part in item["loc"]),
                    "message": item["msg"],
                }
                for item in error.errors(include_input=False, include_context=False)
            ],
        ) from None


@dataclass
class AguiContext:
    thread_id: str
    run_id: str
    warnings: list[str] = field(default_factory=list)


async def encode_agui(
    events: AsyncIterator[dict], context: AguiContext
) -> AsyncIterator[str]:
    """Emit official SDK events; application state carries durable run and HITL data."""
    encoder = EventEncoder(accept="text/event-stream")

    def encode(kind: str, **fields) -> str:
        return encoder.encode(
            EVENTS.validate_python(
                {"type": kind, "timestamp": int(time.time() * 1000), **fields}
            )
        )

    yield encode(
        "RUN_STARTED",
        threadId=context.thread_id,
        runId=context.run_id,
        protocolVersion="1.0",
    )
    for warning in context.warnings:
        yield encode("CUSTOM", name="input.warning", value={"message": warning})
    text_id = context.run_id + ":answer"
    text_open = False
    steps: dict[str, dict] = {}
    calls: set[str] = set()
    results: set[str] = set()
    reasoning: dict[str, str] = {}
    reasoning_closed: set[str] = set()
    current: dict | None = None
    async for item in events:
        if "run" in item:
            current = item["run"]
            yield encode("STATE_SNAPSHOT", snapshot={"run": current})
        if item["type"] == "text.delta" and item.get("text"):
            if not text_open:
                text_open = True
                yield encode("TEXT_MESSAGE_START", messageId=text_id, role="assistant")
            yield encode("TEXT_MESSAGE_CONTENT", messageId=text_id, delta=item["text"])
        if item["type"] == "workflow.updated":
            entry = item["entry"]
            key = entry["id"]
            metadata = {"workflowEntry": entry}
            yield encode(
                "ACTIVITY_SNAPSHOT",
                messageId=f"{context.run_id}:activity:{key}",
                activityType="workflow",
                content=entry,
                replace=True,
            )
            if entry["status"] == "running" and key not in steps:
                steps[key] = entry
                yield encode("STEP_STARTED", stepName=key, metadata=metadata)
            if entry["kind"] == "tool":
                call_id = context.run_id + ":" + key
                if key not in calls and entry.get("phase") in {"executing", "returned"}:
                    calls.add(key)
                    yield encode(
                        "TOOL_CALL_START",
                        toolCallId=call_id,
                        toolCallName=entry["tool_name"],
                        metadata=metadata,
                    )
                    yield encode(
                        "TOOL_CALL_ARGS",
                        toolCallId=call_id,
                        delta=json.dumps(
                            entry.get("arguments", {}), ensure_ascii=False
                        ),
                    )
                    yield encode("TOOL_CALL_END", toolCallId=call_id)
                if (
                    entry["status"] in {"succeeded", "failed"}
                    and "result" in entry
                    and key not in results
                ):
                    results.add(key)
                    yield encode(
                        "TOOL_CALL_RESULT",
                        messageId=call_id + ":result",
                        toolCallId=call_id,
                        content=json.dumps(entry["result"], ensure_ascii=False),
                        role="tool",
                        metadata=metadata,
                    )
            if entry["kind"] == "model" and entry.get("reasoning"):
                message_id = context.run_id + ":reasoning:" + key
                if key not in reasoning:
                    reasoning[key] = ""
                    yield encode("REASONING_START", messageId=message_id)
                    yield encode(
                        "REASONING_MESSAGE_START",
                        messageId=message_id,
                        role="reasoning",
                    )
                previous = reasoning[key]
                text = entry["reasoning"]
                delta = text[len(previous) :] if text.startswith(previous) else ""
                if delta:
                    yield encode(
                        "REASONING_MESSAGE_CONTENT", messageId=message_id, delta=delta
                    )
                reasoning[key] = text
            if entry["status"] not in {"running", "pending"}:
                if key in reasoning and key not in reasoning_closed:
                    message_id = context.run_id + ":reasoning:" + key
                    yield encode("REASONING_MESSAGE_END", messageId=message_id)
                    yield encode("REASONING_END", messageId=message_id)
                    reasoning_closed.add(key)
                if key in steps:
                    steps.pop(key)
                    yield encode("STEP_FINISHED", stepName=key, metadata=metadata)
        if item["type"] == "stream.closed":
            if current is None:
                yield encode(
                    "RUN_ERROR", code="INVALID_RUN_STATE", message="任务缺少最终状态"
                )
                return
            for key in reasoning.keys() - reasoning_closed:
                message_id = context.run_id + ":reasoning:" + key
                yield encode("REASONING_MESSAGE_END", messageId=message_id)
                yield encode("REASONING_END", messageId=message_id)
            for key in steps:
                yield encode(
                    "STEP_FINISHED",
                    stepName=key,
                    metadata={"status": current["status"]},
                )
            if text_open:
                yield encode("TEXT_MESSAGE_END", messageId=text_id)
            if current["status"] in {"failed", "needs_reconciliation"}:
                failure = current.get("error") or {}
                yield encode(
                    "RUN_ERROR",
                    code=failure.get("code", "RUN_FAILED"),
                    message=failure.get("message", "任务未完成，请读取最新状态"),
                )
            else:
                interaction = current.get("interaction")
                outcome = {"type": "success"}
                if current["status"] in {"cancelled", "expired"}:
                    outcome = {"type": "cancelled"}
                elif interaction or current["status"] in {
                    "waiting",
                    "running",
                    "interrupted",
                }:
                    outcome = {
                        "type": "interrupt",
                        "interrupts": [
                            {
                                "id": interaction["id"]
                                if interaction
                                else current["id"],
                                "reason": "tool_interaction"
                                if interaction
                                else current["status"],
                                "message": interaction["title"]
                                if interaction
                                else "请读取任务状态后继续",
                                "metadata": {"interaction": interaction}
                                if interaction
                                else {},
                            }
                        ],
                    }
                yield encode(
                    "RUN_FINISHED",
                    threadId=context.thread_id,
                    runId=context.run_id,
                    outcome=outcome,
                )
            return


def mount_agui_api(app: FastAPI, *, start, resume, recover) -> None:
    @app.post("/api/v1/ag-ui", name="chat.agui.run", openapi_extra=CHAT_METADATA)
    async def run_agent(payload: RunAgentInput, request: Request):
        from starlette.concurrency import run_in_threadpool

        from agent_permissions.hitl_api import InteractionSubmission, RunRequest, owner

        user_id = await owner(request)
        if (
            not payload.thread_id
            or len(payload.thread_id) > 128
            or not payload.run_id
            or len(payload.run_id) > 128
        ):
            raise ApiError(422, "VALIDATION_ERROR", "会话或运行标识不合法")
        if payload.tools:
            raise ApiError(
                422,
                "AGUI_FRONTEND_TOOLS_UNSUPPORTED",
                "此 Agent 使用服务端授权工具，不接受客户端工具定义",
            )
        props = {} if payload.forwarded_props is None else payload.forwarded_props
        if not isinstance(props, dict):
            raise ApiError(422, "VALIDATION_ERROR", "forwardedProps 必须为对象")
        request.state.agui = AguiContext(payload.thread_id, payload.run_id)
        action = props.get("action", "start")
        if action in {"resume", "recover"}:
            run_id = props.get("businessRunId")
            if not isinstance(run_id, str):
                raise ApiError(422, "VALIDATION_ERROR", "缺少服务端任务标识")
            run = await run_in_threadpool(app.state.run_store.raw, run_id, user_id)
            if run["conversation_id"] != payload.thread_id:
                raise ApiError(409, "AGUI_THREAD_MISMATCH", "任务不属于当前会话")
            if action == "recover":
                if payload.resume:
                    raise ApiError(
                        422, "VALIDATION_ERROR", "恢复任务不接受新的批准参数"
                    )
                return await recover(run_id, request)
            if not payload.resume or len(payload.resume) != 1:
                raise ApiError(422, "VALIDATION_ERROR", "请提交一个待处理操作")
            entry = payload.resume[0]
            data = entry.payload if isinstance(entry.payload, dict) else {}
            submission = validated(
                InteractionSubmission,
                {
                    **data,
                    "submission_id": payload.run_id,
                    "decision": "cancel" if entry.status == "cancelled" else "submit",
                },
            )
            return await resume(run_id, entry.interrupt_id, submission, request)
        if action != "start" or payload.resume:
            raise ApiError(422, "VALIDATION_ERROR", "不支持此运行操作")
        messages = []
        for message in payload.messages:
            if message.role not in {"user", "assistant", "system", "developer"}:
                continue
            content = message.content
            if isinstance(content, list):
                if any(part.type != "text" for part in content):
                    request.state.agui.warnings.append(
                        "当前模型仅处理文本，已跳过非文本内容"
                    )
                content = "".join(part.text for part in content if part.type == "text")
                if not content:
                    content = "用户发送了当前模型无法读取的非文本内容，请说明能力限制并请求文字描述。"
            if content:
                messages.append({"role": message.role, "content": content})
        if payload.context:
            messages.insert(
                0,
                {
                    "role": "user",
                    "content": "用户提供的参考上下文：\n"
                    + "\n".join(
                        f"{item.description}: {item.value}" for item in payload.context
                    ),
                },
            )
        model = props.get("model") or (await _cached_model(app)).model_name
        data = validated(
            RunRequest,
            {
                "model": model,
                "messages": messages,
                "client_request_id": payload.run_id,
                "conversation_id": payload.thread_id,
                **{
                    key: props[key]
                    for key in (
                        "temperature",
                        "top_p",
                        "max_tokens",
                        "max_completion_tokens",
                        "tool_choice",
                    )
                    if key in props
                },
            },
        )
        return await start(data, request)
