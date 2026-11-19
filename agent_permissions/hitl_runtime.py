"""Sequential, checkpointed Agent execution with human parameter collection."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Annotated, NotRequired, TypedDict

import httpx
from fastapi import Request
from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.config import merge_configs
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt
from langsmith import traceable
from starlette.concurrency import run_in_threadpool

from agent_permissions import audit
from agent_permissions.agent_grants import _label
from agent_permissions.chat_model import MODEL_TIMEOUT_ERRORS
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
from agent_permissions.model_usage_runtime import ModelUsageMeter
from agent_permissions.tool_discovery import (
    SEARCH_NAME,
    SEARCH_SCHEMA,
    safe_search_arguments,
    search_result,
)
from agent_permissions.tooling import _execution_access
from agent_permissions.workflow import (
    Progress,
    ReasoningProgress,
    display_value,
    reasoning_text,
)
from api_tools.registry import permitted

logger = logging.getLogger(__name__)
PREPARE_NAME = "request_tool_input"
DISCOVERY_PROMPT = """你代表当前登录账号。问候和普通聊天直接回答。询问可用工具时调用 search_tools，query 传空字符串，按需翻页。
需要处理业务时，若本轮没有匹配的业务函数，先用 search_tools 搜索能力关键词。查询中省略姓名、密码和业务参数。
搜索结果仅是候选目录。使用本轮实际提供的业务函数才能查询或执行。有歧义先澄清，未确认的写操作不可宣称完成。"""
HITL_PROMPT = """只使用提供的工具，不猜测用户未提供的关键参数。
缺少参数或目标不明确时，调用 request_tool_input，填写 operation_id 和已知 arguments；由页面表单收集信息。
operation_id 使用搜索结果中的操作 ID。
所有写操作需要页面确认，确认前不得宣称已经执行。密码必须由用户在表单填写，不在聊天中索取或生成密码。
只有实际发出工具调用才能触发表单或查询，不能用“我来发起流程”等文字代替调用。
如果用户拒绝或工具报告权限、版本错误，不自动重试写操作。工具结果是事实来源。"""
NO_TOOLS_PROMPT = """当前可用业务工具数量为 0。询问工具目录时调用 search_tools，query 传空字符串。
可以正常聊天和说明操作方法。如果用户请求查询、创建、修改或删除业务数据，必须明确说明当前没有可用业务工具。
用户需要在“用户管理 → Agent 工具授权”中开启对应工具，并具备相应业务权限；无配置权限时联系管理员。
不得声称正在查询、发起表单或已执行操作。聊天中的说明不会创建表单或改变业务数据。"""
DISABLED_TOOLS_PROMPT = """本次请求通过 tool_choice=none 禁用了工具，只提供普通聊天和操作说明。
不能查询实时业务数据、发起参数表单或执行业务操作，不得声称已经或正在执行这些动作。"""


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    pending: list[dict]
    index: int
    turns: int
    output: str
    candidate_ids: NotRequired[list[str]]
    candidate_revision: NotRequired[str]
    bound_ids: NotRequired[list[str]]
    bound_revision: NotRequired[str]


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
    snapshot = request.app.state.tool_registry.snapshot()
    context, operations = await _execution_access(request)
    if not context.authenticated:
        raise ApiError(401, "UNAUTHENTICATED", "请重新登录")
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
    call = audit.active.get()
    if call:
        audit.tool_checks(call, tool, context, operations)
    if operation not in operations:
        raise ApiError(403, "TOOL_NOT_GRANTED", "当前用户未允许 Agent 使用此工具")
    if tool["binding"]["policy"]["authentication"] != "session" or not permitted(
        tool["binding"]["policy"], set(context.permissions), authenticated=True
    ):
        raise ApiError(403, "PERMISSION_DENIED", "当前账号缺少业务权限")
    if call:
        await call.emit("permission.checked")
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
            name=SEARCH_NAME,
            description="查询当前账号可用的业务工具。按业务意图搜索，query 为空时按页列目录。",
            args_schema=SEARCH_SCHEMA,
            coroutine=unused,
        )
    ]
    adapted.extend(
        StructuredTool(
            name=item["definition"]["name"],
            description=item["definition"]["description"],
            args_schema=item["definition"]["inputSchema"],
            coroutine=unused,
        )
        for item in tools
    )
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


def selected_tool(tools: list[dict], call: dict) -> dict | None:
    if call["name"] == PREPARE_NAME:
        operation = call["args"].get("operation_id")
        return next(
            (tool for tool in tools if tool["binding"]["operationId"] == operation),
            None,
        )
    return next(
        (tool for tool in tools if tool["definition"]["name"] == call["name"]), None
    )


def safe_model_message(
    message: AIMessage, tools: list[dict], *, searchable: list[dict] | None = None
) -> AIMessage:
    """Keep only schema-scrubbed function arguments in the replayable model message."""
    calls = []
    for call in message.tool_calls:
        tool = selected_tool(tools, call)
        if call["name"] == SEARCH_NAME:
            arguments = safe_search_arguments(
                call["args"], tools if searchable is None else searchable
            )
        elif call["name"] == PREPARE_NAME:
            proposal = call["args"].get("arguments")
            arguments = {
                "operation_id": tool["binding"]["operationId"] if tool else None,
                "arguments": safe_arguments(tool, proposal)
                if tool and isinstance(proposal, dict)
                else {},
            }
        else:
            arguments = safe_arguments(tool, call["args"]) if tool else {}
        calls.append({**call, "args": arguments})

    content = message.content
    if isinstance(content, list):
        # Responses replays its content blocks in preference to tool_calls. Use the
        # same sanitized arguments in both, preserving provider item IDs and order.
        by_id = {(call["id"], call["name"]): call["args"] for call in calls}
        content = []
        for block in message.content:
            kind = block.get("type") if isinstance(block, dict) else None
            if kind in {"invalid_tool_call", "tool_call_chunk"}:
                continue
            if kind in {"function_call", "tool_call"}:
                key = "call_id" if kind == "function_call" else "id"
                args = by_id.get((block.get(key), block.get("name")))
                if args is None:
                    continue  # Unparsed calls have no executable counterpart.
                if kind == "function_call":
                    block = {**block, "arguments": json.dumps(args, ensure_ascii=False)}
                else:
                    block = {**block, "args": args}
            content.append(block)
    return message.model_copy(
        update={
            "content": content,
            "tool_calls": calls,
            "invalid_tool_calls": [],
            "additional_kwargs": {
                key: scrub(value)
                for key, value in message.additional_kwargs.items()
                if key not in {"tool_calls", "function_call"}
            },
        }
    )


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
    progress: Progress,
):
    from agent_permissions.chat_api import SYSTEM_PROMPT

    store: RunStore = request.app.state.run_store
    payload = json.loads(run["payload"])
    catalog, tools = await available_tools(request)
    discovery_enabled = payload.get("tool_choice") != "none"
    if not discovery_enabled:
        tools = []
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
        entry_id = f"model:{state['turns'] + 1}:{identity('attempt')}"
        previous_output = state.get("output", "")
        await progress(
            {
                "id": entry_id,
                "kind": "model",
                "answer_offset": len(previous_output),
                "title": f"分析请求 · 第 {state['turns'] + 1} 轮",
                "status": "running",
                "reasoning": "",
                "detail": "正在等待模型响应",
            }
        )
        reasoning = ReasoningProgress(entry_id, progress)
        snapshot, allowed = await available_tools(request)
        candidates = []
        if (
            discovery_enabled
            and state.get("candidate_revision") == snapshot["bundleDigest"]
        ):
            by_id = {item["binding"]["operationId"]: item for item in allowed}
            candidates = [
                by_id[op] for op in state.get("candidate_ids", []) if op in by_id
            ]
        bound = model
        tool_prompt = DISABLED_TOOLS_PROMPT
        if discovery_enabled:
            bound = model.bind_tools(model_tools(candidates))
            tool_prompt = DISCOVERY_PROMPT if allowed else NO_TOOLS_PROMPT
            if candidates:
                tool_prompt += "\n" + HITL_PROMPT
        messages = [
            SystemMessage(content=SYSTEM_PROMPT + "\n" + tool_prompt),
            *state["messages"],
        ]

        async def invoke_model() -> AIMessage:
            meter = await ModelUsageMeter.start(
                store.database,
                user_id=run["user_id"],
                run_id=run["id"],
                model=payload["model"],
                started_at=store.now(),
            )
            model_config = merge_configs(config, {"callbacks": [reasoning, meter]})
            response = await bound.ainvoke(messages, config=model_config, **options)
            await meter.record(response.usage_metadata)
            return response

        try:
            response = await invoke_model()
        except MODEL_TIMEOUT_ERRORS:
            # Only repeat the initial model request, before any streamed content or tool call.
            if state["turns"] or reasoning.received_output:
                raise
            await run_in_threadpool(store.assert_running, run)
            await progress(
                {"id": entry_id, "detail": "等待模型响应超时，正在重试首次请求"}
            )
            response = await invoke_model()
        response = safe_model_message(
            response, candidates, searchable=allowed if discovery_enabled else []
        )
        output = previous_output + response.text
        calls = response.tool_calls
        await progress(
            {
                "id": entry_id,
                "status": "succeeded",
                "reasoning": reasoning.text or reasoning_text(response)[:64000],
                "detail": f"已选择 {len(calls)} 个工具" if calls else "已生成回复",
            }
        )
        for index, call in enumerate(calls):
            if call["name"] == SEARCH_NAME:
                continue
            selected = selected_tool(candidates, call)
            operation = selected["binding"]["operationId"] if selected else ""
            requested = audit.runtime_call(
                request,
                run,
                f"{state['turns'] + 1}:{index}:{call['id']}",
                selected or selected_tool(snapshot["tools"], call),
                snapshot["bundleDigest"],
            )
            requested.requested_target(
                call["args"].get("arguments")
                if call["name"] == PREPARE_NAME
                else call["args"]
            )
            await requested.emit("requested", fact="requested")
            await progress(
                {
                    "id": f"tool:{state['turns'] + 1}:{index}:{call['id']}",
                    "kind": "tool",
                    "answer_offset": len(output),
                    "title": _label(operation, selected)[0]
                    if selected
                    else call["name"],
                    "tool_name": selected["definition"]["name"]
                    if selected
                    else call["name"],
                    "tool_call_id": call["id"],
                    "operation_id": operation,
                    "status": "pending",
                    "phase": "selected",
                    "detail": "已选择工具，等待检查参数",
                    "arguments": display_value(
                        call["args"].get("arguments", {})
                        if call["name"] == PREPARE_NAME
                        else call["args"]
                    ),
                }
            )
        return {
            "messages": [response],
            "pending": calls,
            "bound_ids": [item["binding"]["operationId"] for item in candidates],
            "bound_revision": snapshot["bundleDigest"],
            "index": 0,
            "turns": state["turns"] + 1,
            "output": output,
        }

    async def discover(state: AgentState) -> dict:
        await run_in_threadpool(store.assert_running, run)
        call = state["pending"][state["index"]]
        entry_id = f"search:{state['turns']}:{state['index']}:{call['id']}"
        await progress(
            {
                "id": entry_id,
                "kind": "step",
                "title": "查找可用工具",
                "answer_offset": len(state.get("output", "")),
                "status": "running",
                "detail": "读取当前账号的工具目录",
            }
        )
        current, allowed = await available_tools(request)
        revision = current["bundleDigest"]
        operations = []
        if not discovery_enabled:
            result = {
                "error": {"code": "TOOLS_DISABLED", "message": "本次请求禁用了工具"}
            }
        elif not call["args"]:
            result = {
                "error": {
                    "code": "INVALID_TOOL_ARGUMENTS",
                    "message": "请提供 query，浏览目录时可指定正整数 page",
                }
            }
        elif revision != state.get("bound_revision"):
            result = {
                "error": {
                    "code": "TOOL_VERSION_MISMATCH",
                    "message": "目录已更新，请重新搜索",
                }
            }
        else:
            result, operations = search_result(call["args"], allowed)
        if "error" in result:
            status, detail = "failed", result["error"]["message"]
        elif result["mode"] == "list":
            status, detail = "succeeded", f"当前可用业务工具：{result['total']} 个"
        else:
            status, detail = "succeeded", f"找到 {len(operations)} 个候选工具"
        await progress({"id": entry_id, "status": status, "detail": detail})
        return {
            "candidate_ids": operations,
            "candidate_revision": revision,
            "messages": [
                ToolMessage(
                    name=SEARCH_NAME,
                    tool_call_id=call["id"],
                    content=json.dumps(result, ensure_ascii=False),
                    status="error" if status == "failed" else "success",
                )
            ],
            "index": state["index"] + 1,
        }

    async def execute(state: AgentState) -> dict:
        await run_in_threadpool(store.assert_running, run)
        call = state["pending"][state["index"]]
        call_key = f"{state['turns']}:{state['index']}:{call['id']}"
        entry_id = "tool:" + call_key
        snapshot = request.app.state.tool_registry.snapshot()
        bound_ids = state.get("bound_ids")
        # Pre-discovery checkpoints only contain pending calls from the old full binding.
        selected = selected_tool(
            [
                tool
                for tool in snapshot["tools"]
                if bound_ids is None or tool["binding"]["operationId"] in bound_ids
            ],
            call,
        )
        evidence = audit.runtime_call(
            request,
            run,
            call_key,
            selected or selected_tool(snapshot["tools"], call),
            snapshot["bundleDigest"],
        )
        with audit.scope(evidence):
            await evidence.emit("attempt.started")
            return await execute_call(
                state, call, call_key, entry_id, snapshot, bound_ids, selected, evidence
            )

    async def execute_call(
        state, call, call_key, entry_id, snapshot, bound_ids, selected, evidence
    ):
        operation_hint = selected["binding"]["operationId"] if selected else ""
        await progress(
            {
                "id": entry_id,
                "kind": "tool",
                "answer_offset": len(state.get("output", "")),
                "title": _label(operation_hint, selected)[0]
                if selected
                else call["name"],
                "tool_name": selected["definition"]["name"]
                if selected
                else call["name"],
                "tool_call_id": call["id"],
                "operation_id": operation_hint,
                "status": "running",
                "phase": "validating",
                "detail": "正在校验权限与参数",
            }
        )
        try:
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
                await evidence.emit(
                    "receipt.reconciled",
                    execution_id=committed["id"],
                    interaction_id=previous["id"] if previous else None,
                )
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
                await progress(
                    {
                        "id": entry_id,
                        "status": "succeeded",
                        "phase": "returned",
                        "detail": "已恢复执行结果",
                        "result": display_value(result),
                        **(
                            {
                                "arguments": display_value(
                                    json.loads(previous["document"])["arguments"]
                                )
                            }
                            if previous
                            else {}
                        ),
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
            if not discovery_enabled:
                raise ApiError(409, "TOOLS_DISABLED", "本次请求禁用了工具")
            if bound_ids is not None and selected is None:
                raise ApiError(
                    409, "TOOL_NOT_LOADED", "此工具未在本轮加载，请先搜索可用工具"
                )
            if call["name"] == PREPARE_NAME:
                operation = call["args"].get("operation_id")
                arguments = call["args"].get("arguments", {})
            else:
                if not selected:
                    raise ApiError(404, "TOOL_UNAVAILABLE", "工具不存在")
                operation, arguments = selected["binding"]["operationId"], call["args"]
            if not isinstance(arguments, dict) or not isinstance(operation, str):
                raise ApiError(422, "VALIDATION_ERROR", "工具准备参数不合法")
            interaction = await run_in_threadpool(store.for_call, run["id"], call_key)
            expected_revision = (
                interaction["catalog_revision"]
                if interaction
                else state.get("bound_revision")
            )
            snapshot, tool = await require_tool(request, operation, expected_revision)
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
                await progress(
                    {
                        "id": entry_id,
                        "status": "waiting",
                        "phase": "waiting",
                        "detail": "等待填写参数或确认执行",
                    }
                )
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
            await progress(
                {
                    "id": entry_id,
                    "status": "running",
                    "phase": "executing",
                    "detail": "正在执行工具",
                    "arguments": display_value(safe_arguments(tool, arguments)),
                }
            )
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
                evidence.fields["execution_id"] = execution["id"]
                if interaction:
                    evidence.fields["interaction_id"] = interaction["id"]
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
                            if value is not None and committed["status"] == "succeeded":
                                await evidence.emit(
                                    "receipt.reconciled", execution_id=committed["id"]
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
            await progress(
                {
                    "id": entry_id,
                    "status": "succeeded"
                    if result.get("ok") is not False and not result.get("error")
                    else "failed",
                    "detail": "工具已返回结果",
                    "phase": "returned",
                    "result": display_value(result),
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
        except ApiError as error:
            await evidence.reject(error, control=False)
            await progress(
                {
                    "id": entry_id,
                    "status": "failed",
                    "phase": "returned",
                    "detail": error.message,
                    "result": {"error": {"code": error.code, "message": error.message}},
                }
            )
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

    def next_call(state: AgentState) -> str:
        if state["index"] >= len(state["pending"]):
            return "model"
        return (
            "discover"
            if state["pending"][state["index"]]["name"] == SEARCH_NAME
            else "tool"
        )

    builder = StateGraph(AgentState)
    builder.add_node("model", think)
    builder.add_node("discover", discover)
    builder.add_node("tool", execute)
    builder.add_edge(START, "model")
    builder.add_conditional_edges(
        "model", lambda state: next_call(state) if state["pending"] else END
    )
    builder.add_conditional_edges("tool", next_call)
    builder.add_conditional_edges("discover", next_call)
    return builder.compile(
        checkpointer=DatabaseCheckpointer(
            request.app.state.database,
            restore_ai_message=lambda message: safe_model_message(
                message, catalog["tools"]
            ),
        ),
        name="agent_permissions.chat",
    ), tools


async def _run_events(
    request: Request,
    run: dict,
    model,
    *,
    resume: dict | None = None,
    sensitive: dict | None = None,
    fresh: bool = True,
    recovering: bool = False,
    streaming: bool = True,
    progress: Progress,
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
            yield event(
                "run.resumed" if resume or recovering else "run.started",
                run=await snapshot(),
            )
            access_id = identity("access")
            await progress(
                {
                    "id": access_id,
                    "kind": "step",
                    "answer_offset": len(output),
                    "title": "检查可用工具",
                    "status": "running",
                    "detail": "正在读取当前账号的工具授权和业务权限",
                }
            )
            graph, tools = await build_graph(
                request,
                run,
                model,
                http,
                sensitive or {},
                resume.get("interaction_id") if resume else None,
                progress,
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
            await progress(
                {
                    "id": access_id,
                    "status": "succeeded",
                    "answer_offset": len(output),
                    "detail": f"当前可用业务工具：{len(tools)} 个",
                }
            )
            if recovering:
                yield event("run.snapshot", run=await snapshot())
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
        if status == "failed":
            logger.warning(
                "Agent run failed request_id=%s run_id=%s reason=%s error_type=%s",
                request.state.info.request_id,
                run["id"],
                safe.code,
                type(error).__qualname__,
            )
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


async def run_events(
    request: Request, run: dict, model, **options
) -> AsyncIterator[dict]:
    """Multiplex live graph text and node progress without waiting for a tool to finish."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    store: RunStore = request.app.state.run_store
    stream_id, sequence = identity("stream"), 0

    async def progress(entry: dict) -> None:
        document = await run_in_threadpool(store.record_workflow, run, entry)
        await queue.put({"type": "workflow.updated", "entry": document})

    async def produce() -> None:
        try:
            async for item in _run_events(
                request, run, model, progress=progress, **options
            ):
                await queue.put(item)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 -- forward producer failures to the stream consumer
            await queue.put(error)
        else:
            await queue.put(None)

    task = asyncio.create_task(produce())
    try:
        while (item := await queue.get()) is not None:
            if isinstance(item, Exception):
                raise item
            sequence += 1
            yield {
                **item,
                "protocol_version": 1,
                "run_id": run["id"],
                "stream_id": stream_id,
                "sequence": sequence,
            }
        await task
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task
