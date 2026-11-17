"""Sequential, checkpointed Agent execution with human parameter collection."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, TypedDict

import httpx
from fastapi import Request
from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt
from langsmith import traceable
from starlette.concurrency import run_in_threadpool

from agent_permissions.errors import ApiError
from agent_permissions.hitl_checkpoint import DatabaseCheckpointer
from agent_permissions.hitl_execution import (
    TRANSACTIONAL_OPERATIONS,
    ConfirmedExecution,
    confirmed_execution,
)
from agent_permissions.hitl_forms import (
    apply_values,
    prepare_form,
    requires_confirmation,
    safe_arguments,
    scrub,
    validate_arguments,
)
from agent_permissions.hitl_store import GRAPH_VERSION, RunStore, identity
from agent_permissions.tooling import _execution_access
from api_tools.registry import permitted

PREPARE_NAME = "request_tool_input"
HITL_PROMPT = """你代表当前登录用户处理业务。只使用提供的工具，不猜测用户未提供的关键参数。
缺少参数或目标不明确时，调用 request_tool_input，填写 operation_id 和已知 arguments；由页面表单收集信息。
工具名称 agent_users_ 后的下划线对应操作 ID 的点，例如 agent_users_user_create 对应 user.create。
所有写操作需要页面确认，确认前不得宣称已经执行。密码必须由用户在表单填写，不在聊天中索取或生成密码。
如果用户拒绝或工具报告权限、版本错误，不自动重试写操作。工具结果是事实来源。"""


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    pending: list[dict]
    index: int
    turns: int
    output: str


async def available_tools(request: Request) -> tuple[dict, list[dict]]:
    context, operations = await _execution_access(request)
    if not context.authenticated:
        raise ApiError(401, "UNAUTHENTICATED", "请重新登录")
    snapshot = request.app.state.tool_registry.snapshot()
    return snapshot, [
        item
        for item in snapshot["tools"]
        if item["binding"]["policy"]["authentication"] == "session"
        and item["binding"]["operationId"] in operations
        and permitted(
            item["binding"]["policy"], set(context.permissions), authenticated=True
        )
    ]


async def require_tool(
    request: Request, operation: str, revision: str | None = None
) -> tuple[dict, dict]:
    snapshot, allowed = await available_tools(request)
    tool = next(
        (
            item
            for item in snapshot["tools"]
            if item["binding"]["operationId"] == operation
        ),
        None,
    )
    if tool is None:
        raise ApiError(404, "TOOL_UNAVAILABLE", "工具已下线，请重新发起任务")
    if revision is not None and revision != snapshot["bundleDigest"]:
        raise ApiError(409, "TOOL_VERSION_MISMATCH", "工具定义已变化，请重新准备并确认")
    if tool not in allowed:
        _, operations = await _execution_access(request)
        if operation not in operations:
            raise ApiError(403, "TOOL_NOT_GRANTED", "当前用户未允许 Agent 使用此工具")
        raise ApiError(403, "PERMISSION_DENIED", "当前账号缺少业务权限")
    if (
        tool["binding"]["policy"]["effect"] != "read"
        and operation not in TRANSACTIONAL_OPERATIONS
    ):
        raise ApiError(409, "TOOL_RECEIPT_REQUIRED", "此写工具尚未接入事务执行记录")
    return snapshot, tool


def http_client(request: Request) -> httpx.AsyncClient:
    address = request.client
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=request.app,
            client=(address.host, address.port) if address else ("unknown", 0),
        ),
        base_url="http://agent-chat.internal",
        trust_env=False,
    )


def model_tools(tools: list[dict]) -> list[StructuredTool]:
    async def unused(**arguments: object) -> dict:
        raise RuntimeError("Model definitions are executed by the controlled graph")

    adapted = [
        StructuredTool(
            name=item["definition"]["name"],
            description=item["definition"]["description"],
            args_schema=item["definition"]["inputSchema"],
            coroutine=unused,
        )
        for item in tools
    ]
    if tools:
        adapted.append(
            StructuredTool(
                name=PREPARE_NAME,
                description="已选定工具但需要用户补充参数、选择目标或确认时调用。只提供已知参数，不猜测密码或对象 ID。",
                args_schema={
                    "type": "object",
                    "properties": {
                        "operation_id": {
                            "type": "string",
                            "enum": [item["binding"]["operationId"] for item in tools],
                        },
                        "arguments": {
                            "type": "object",
                            "description": "已知参数，按原工具 path/query/body 分组；未知参数省略",
                        },
                    },
                    "required": ["operation_id", "arguments"],
                    "additionalProperties": False,
                },
                coroutine=unused,
            )
        )
    return adapted


def saved_result(row: dict, tool: dict, revision: str) -> dict | None:
    if row["result"]:
        return json.loads(row["result"])
    if row["status"] != "succeeded" or row["service_result"] is None:
        return None
    # A committed business transaction may precede a lost HTTP response.
    status = min(
        int(value) for value in tool["binding"]["responses"] if value.startswith("2")
    )
    value = json.loads(row["service_result"])
    request_id = "recovered_" + row["id"]
    return {
        "ok": True,
        "httpStatus": status,
        "body": {"data": value, "request_id": request_id} if status != 204 else None,
        "headers": {"etag": f'"v{value["version"]}"'}
        if isinstance(value, dict) and "version" in value
        else {},
        "requestId": request_id,
        "bundleDigest": revision,
    }


def usage_for(messages: list) -> dict:
    evidence = [
        item.usage_metadata
        for item in messages
        if isinstance(item, AIMessage) and item.usage_metadata
    ]
    return (
        {
            "prompt_tokens": sum(item["input_tokens"] for item in evidence),
            "completion_tokens": sum(item["output_tokens"] for item in evidence),
            "total_tokens": sum(item["total_tokens"] for item in evidence),
        }
        if evidence
        else {}
    )


async def build_graph(
    request: Request,
    run: dict,
    model,
    http: httpx.AsyncClient,
    sensitive: dict,
    sensitive_interaction: str | None,
):
    from agent_permissions.chat_api import SYSTEM_PROMPT

    store: RunStore = request.app.state.run_store
    payload = json.loads(run["payload"])
    _, tools = await available_tools(request)
    if payload.get("tool_choice") == "none":
        tools = []
    adapted = model_tools(tools)
    bound = model.bind_tools(adapted) if adapted else model
    options = {
        key: value
        for key, value in {
            "temperature": payload.get("temperature"),
            "top_p": payload.get("top_p"),
            "max_tokens": payload.get("max_completion_tokens")
            or payload.get("max_tokens"),
        }.items()
        if value is not None
    }
    authorization = request.headers["Authorization"]

    async def think(state: AgentState, config: RunnableConfig) -> dict:
        await run_in_threadpool(store.assert_running, run)
        if state["turns"] >= 24:
            raise ApiError(
                409, "agent_step_limit", "任务步骤达到上限，请核对已执行结果"
            )
        response = await bound.ainvoke(
            [
                SystemMessage(content=SYSTEM_PROMPT + "\n" + HITL_PROMPT),
                *state["messages"],
            ],
            config=config,
            **options,
        )
        calls = []
        for call in response.tool_calls:
            arguments = scrub(call["args"])
            selected = next(
                (
                    item
                    for item in tools
                    if item["definition"]["name"] == call["name"]
                    or (
                        call["name"] == PREPARE_NAME
                        and item["binding"]["operationId"]
                        == arguments.get("operation_id")
                    )
                ),
                None,
            )
            if selected:
                if call["name"] == PREPARE_NAME:
                    arguments["arguments"] = safe_arguments(
                        selected, arguments.get("arguments", {})
                    )
                else:
                    arguments = safe_arguments(selected, arguments)
            calls.append({**call, "args": arguments})
        response = response.model_copy(
            update={
                "tool_calls": calls,
                "additional_kwargs": {
                    key: scrub(value)
                    for key, value in response.additional_kwargs.items()
                    if key not in {"tool_calls", "function_call"}
                },
            }
        )
        return {
            "messages": [response],
            "pending": calls,
            "index": 0,
            "turns": state["turns"] + 1,
            "output": state.get("output", "") + response.text,
        }

    async def execute(state: AgentState) -> dict:
        await run_in_threadpool(store.assert_running, run)
        call = state["pending"][state["index"]]
        call_key = f"{state['turns']}:{state['index']}:{call['id']}"
        try:
            snapshot = request.app.state.tool_registry.snapshot()
            committed = await run_in_threadpool(store.execution, run["id"], call_key)
            if committed and committed["status"] == "succeeded":
                # Reading this owner's existing receipt is not another business
                # execution. A subsequent revocation must not rewrite success as denial.
                previous = await run_in_threadpool(store.for_call, run["id"], call_key)
                if previous:
                    decision = interrupt({"interaction_id": previous["id"]})
                    if (
                        not isinstance(decision, dict)
                        or decision.get("interaction_id") != previous["id"]
                    ):
                        raise ApiError(409, "INTERACTION_REQUIRED", "确认上下文不匹配")
                original = next(
                    (
                        tool
                        for tool in snapshot["tools"]
                        if tool["binding"]["operationId"] == committed["operation_id"]
                    ),
                    None,
                )
                result = (
                    saved_result(committed, original, snapshot["bundleDigest"])
                    if original
                    else {
                        "ok": True,
                        "recovered": True,
                        "body": {"data": json.loads(committed["service_result"])},
                    }
                )
                return {
                    "messages": [
                        ToolMessage(
                            content=json.dumps(result, ensure_ascii=False),
                            tool_call_id=call["id"],
                            name=call["name"],
                        )
                    ],
                    "index": state["index"] + 1,
                }
            if call["name"] == PREPARE_NAME:
                operation = call["args"].get("operation_id")
                arguments = call["args"].get("arguments", {})
            else:
                selected = next(
                    (
                        item
                        for item in snapshot["tools"]
                        if item["definition"]["name"] == call["name"]
                    ),
                    None,
                )
                if not selected:
                    raise ApiError(404, "TOOL_UNAVAILABLE", "工具不存在")
                operation, arguments = selected["binding"]["operationId"], call["args"]
            if not isinstance(arguments, dict) or not isinstance(operation, str):
                raise ApiError(422, "VALIDATION_ERROR", "工具准备参数不合法")
            snapshot, tool = await require_tool(request, operation)
            interaction = await run_in_threadpool(store.for_call, run["id"], call_key)
            valid = True
            try:
                validate_arguments(tool, arguments)
            except ApiError:
                valid = False
            if not interaction and (
                requires_confirmation(tool) or not valid or call["name"] == PREPARE_NAME
            ):
                document = await prepare_form(tool, arguments, http, authorization)
                interaction = await run_in_threadpool(
                    store.prepare,
                    run,
                    call_key,
                    call["id"],
                    operation,
                    snapshot["bundleDigest"],
                    document,
                )
            if interaction:
                # Always revisit this interrupt, including replay after the write committed.
                decision = interrupt({"interaction_id": interaction["id"]})
                interaction = await run_in_threadpool(
                    store.for_call, run["id"], call_key
                )
                if (
                    not isinstance(decision, dict)
                    or decision.get("interaction_id") != interaction["id"]
                    or interaction["status"] not in {"submitted", "consumed"}
                ):
                    raise ApiError(
                        409, "INTERACTION_REQUIRED", "请从当前待处理卡片确认操作"
                    )
                snapshot, tool = await require_tool(
                    request, operation, interaction["catalog_revision"]
                )
                document = json.loads(interaction["document"])
                arguments = document["arguments"]
                if interaction["id"] == sensitive_interaction:
                    arguments = apply_values(document, {}, sensitive)
            fingerprint = store.digest(arguments)
            existing = await run_in_threadpool(store.execution, run["id"], call_key)
            result = (
                saved_result(existing, tool, snapshot["bundleDigest"])
                if existing
                else None
            )
            if result is None:
                validate_arguments(tool, arguments)
                execution = await run_in_threadpool(
                    store.start_execution,
                    run,
                    call_key,
                    operation,
                    fingerprint,
                    interaction["id"] if interaction else None,
                )
                context = ConfirmedExecution(
                    execution["id"],
                    run["user_id"],
                    operation,
                    fingerprint,
                    run["lease_token"],
                )
                token = confirmed_execution.set(context)
                try:

                    @traceable(name=tool["definition"]["name"], run_type="tool")
                    async def invoke(safe_input: dict) -> dict:
                        try:
                            response = await http.post(
                                "/agent-tools/call",
                                headers={"Authorization": authorization},
                                json={
                                    "name": tool["definition"]["name"],
                                    "arguments": arguments,
                                    "revision": snapshot["bundleDigest"],
                                },
                            )
                            value = response.json()
                            if not response.is_success:
                                value = {
                                    "ok": False,
                                    "httpStatus": response.status_code,
                                    "body": value,
                                }
                        except Exception:  # noqa: BLE001 -- recover committed writes without leaking request values
                            committed = await run_in_threadpool(
                                store.execution, run["id"], call_key
                            )
                            value = saved_result(
                                committed, tool, snapshot["bundleDigest"]
                            )
                            if value is None:
                                raise ApiError(
                                    502,
                                    "TOOL_EXECUTION_INTERRUPTED",
                                    "工具调用中断，请读取执行记录后恢复",
                                ) from None
                        # The receipt is authoritative even if the response was lost or failed serialization.
                        committed = await run_in_threadpool(
                            store.execution, run["id"], call_key
                        )
                        return (
                            saved_result(committed, tool, snapshot["bundleDigest"])
                            or value
                        )

                    result = await invoke(safe_arguments(tool, arguments))
                finally:
                    confirmed_execution.reset(token)
                await run_in_threadpool(store.finish_execution, execution["id"], result)
            return {
                "messages": [
                    ToolMessage(
                        content=json.dumps(result, ensure_ascii=False),
                        tool_call_id=call["id"],
                        name=call["name"],
                    )
                ],
                "index": state["index"] + 1,
            }
        except ApiError as error:
            return {
                "messages": [
                    ToolMessage(
                        content=json.dumps(
                            {"error": {"code": error.code, "message": error.message}},
                            ensure_ascii=False,
                        ),
                        tool_call_id=call["id"],
                        name=call["name"],
                        status="error",
                    )
                ],
                "index": state["index"] + 1,
            }

    builder = StateGraph(AgentState)
    builder.add_node("model", think)
    builder.add_node("tool", execute)
    builder.add_edge(START, "model")
    builder.add_conditional_edges(
        "model", lambda state: "tool" if state["pending"] else END
    )
    builder.add_conditional_edges(
        "tool",
        lambda state: "tool" if state["index"] < len(state["pending"]) else "model",
    )
    return builder.compile(
        checkpointer=DatabaseCheckpointer(request.app.state.database),
        name="agent_permissions.chat",
    ), tools


async def run_events(
    request: Request,
    run: dict,
    model,
    *,
    resume: dict | None = None,
    sensitive: dict | None = None,
    fresh: bool = True,
    recovering: bool = False,
    streaming: bool = True,
) -> AsyncIterator[dict]:
    store: RunStore = request.app.state.run_store
    stream_id, sequence = identity("stream"), 0

    def event(kind: str, **data) -> dict:
        nonlocal sequence
        sequence += 1
        return {
            "type": kind,
            "protocol_version": 1,
            "run_id": run["id"],
            "stream_id": stream_id,
            "sequence": sequence,
            **data,
        }

    async def snapshot() -> dict:
        return await run_in_threadpool(store.snapshot, run["id"], run["user_id"])

    if not fresh:
        current = await snapshot()
        yield event("run.snapshot", run=current)
        yield event("stream.closed", status=current["status"])
        return
    output = run["output"]
    try:
        if run["graph_version"] != GRAPH_VERSION:
            raise ApiError(409, "RUN_VERSION_MISMATCH", "任务版本已变化，请重新发起")
        async with asyncio.timeout(120), http_client(request) as http:
            graph, tools = await build_graph(
                request,
                run,
                model,
                http,
                sensitive or {},
                resume.get("interaction_id") if resume else None,
            )
            payload = json.loads(run["payload"])
            initial = (
                Command(resume=resume)
                if resume
                else {
                    "messages": payload["messages"],
                    "turns": 0,
                    "index": 0,
                    "pending": [],
                    "output": "",
                }
            )
            if recovering:
                checkpoint = await graph.aget_state(
                    {"configurable": {"thread_id": run["id"]}}
                )
                if checkpoint.values:
                    # A model node may have streamed a prefix before it committed its
                    # checkpoint. Replace that prefix before retrying the same node.
                    output = checkpoint.values.get("output", "")
                    await run_in_threadpool(store.update, run, output=output)
                    initial = None
                    # Re-enter the existing interrupt after a committed write. Its receipt prevents replay.
                    if checkpoint.interrupts:
                        with store.database.transaction() as connection:
                            accepted = connection.execute(
                                "SELECT id FROM agent_interactions WHERE run_id=? AND status IN ('submitted','consumed') ORDER BY created_at DESC,id DESC LIMIT 1",
                                (run["id"],),
                            ).fetchone()
                        if accepted:
                            initial = Command(resume={"interaction_id": accepted[0]})
            yield event(
                "run.resumed" if resume or recovering else "run.started",
                run=await snapshot(),
            )
            with request.app.state.chat_tracing.context(
                request_id=request.state.info.request_id,
                model=payload["model"],
                stream=streaming,
                tools=[tool["definition"]["name"] for tool in tools],
                run_id=run["id"],
            ):
                async with await graph.astream_events(
                    initial,
                    config={
                        "configurable": {"thread_id": run["id"]},
                        "recursion_limit": 100,
                    },
                    version="v3",
                ) as stream:
                    next_snapshot = asyncio.get_running_loop().time()
                    async for message in stream.messages:
                        async for text in message.text:
                            if text:
                                output += text
                                now = asyncio.get_running_loop().time()
                                if now >= next_snapshot:
                                    await run_in_threadpool(store.assert_running, run)
                                    await run_in_threadpool(
                                        store.update, run, output=output
                                    )
                                    next_snapshot = now + 0.5
                                yield event(
                                    "text.delta", text=text, message_id=run["id"]
                                )
                    state = await stream.output()
                    usage = usage_for(state.get("messages", []))
                    waiting = await stream.interrupted()
            from agent_permissions.chat_api import _finish

            final_message = state.get("messages", [])[-1]
            finish = (
                _finish(final_message)
                if isinstance(final_message, AIMessage)
                else "stop"
            )
            await run_in_threadpool(
                store.update,
                run,
                status="waiting" if waiting else "completed",
                output=output,
                usage=usage,
                finish=finish,
            )
            current = await snapshot()
            if current["interaction"] and waiting:
                yield event("interaction.required", interaction=current["interaction"])
            for execution in current["executions"]:
                yield event("tool.completed", execution=execution)
            if usage:
                yield event("usage.updated", usage=usage)
            yield event("run.waiting" if waiting else "run.completed", run=current)
    except (asyncio.CancelledError, GeneratorExit):
        await asyncio.shield(
            run_in_threadpool(store.update, run, status="interrupted", output=output)
        )
        raise
    except Exception as error:  # noqa: BLE001 -- public stream must never leak providers or submitted values
        from agent_permissions.chat_api import _model_error

        safe = _model_error(error)
        status = "cancelled" if safe.code == "RUN_CANCELLED" else "failed"
        await run_in_threadpool(
            store.update,
            run,
            status=status,
            output=output,
            error={
                "code": safe.code,
                "message": safe.message,
                "http_status": safe.status,
            },
        )
        yield event("run." + status, run=await snapshot())
    current = await snapshot()
    yield event("stream.closed", status=current["status"])
