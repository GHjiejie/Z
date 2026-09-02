"""一个可复用的 Middleware 类，配合 ``create_agent`` 使用。

运行前在项目根目录的 .env 中配置 OPENAI_API_KEY、OPENAI_BASE_URL、MODEL：

    uv run python -m middleware_demo.create_agent_middleware
"""

from __future__ import annotations

import operator
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, NotRequired

from dotenv import load_dotenv
from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    hook_config,
)
from langchain.messages import AIMessage, SystemMessage, ToolMessage
from langchain.tools import tool
from langchain.tools.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=True)


class AuditState(AgentState):
    """由同一个 middleware 的所有 hook 共享的、单次调用范围状态。"""

    run_id: NotRequired[str]
    started_at: NotRequired[float]
    model_calls: NotRequired[int]
    audit_events: Annotated[list[str], operator.add]


class AuditMiddleware(AgentMiddleware[AuditState, None, Any]):
    """可复用的生命周期审计 middleware。

    不在 ``self`` 上累加运行时数据：所有状态保存在 LangGraph state 中，因此并发
    调用彼此隔离。把这个类实例放一次进 ``middleware`` 列表即可覆盖所有 hook。
    """

    state_schema = AuditState

    @staticmethod
    def _record(stage: str, detail: str) -> str:
        event = f"{stage}: {detail}"
        print(f"[middleware] {event}")
        return event

    def before_agent(self, state: AuditState, runtime: Runtime) -> dict[str, Any]:
        """执行一次：初始化本次 agent invocation 的审计状态。"""
        run_id = uuid.uuid4().hex[:8]
        return {
            "run_id": run_id,
            "started_at": time.perf_counter(),
            "model_calls": 0,
            "audit_events": [self._record("before_agent", f"run_id={run_id}")],
        }

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: AuditState, runtime: Runtime) -> dict[str, Any]:
        """每轮模型调用前：输入守卫与调用计数。"""
        last_content = str(state["messages"][-1].content) if state["messages"] else ""
        if "/stop" in last_content:
            return {
                "messages": [AIMessage("请求已被 before_model middleware 提前终止。")],
                "audit_events": [
                    self._record("before_model", "matched /stop; jump_to=end")
                ],
                "jump_to": "end",
            }

        count = state.get("model_calls", 0) + 1
        return {
            "model_calls": count,
            "audit_events": [self._record("before_model", f"model_call={count}")],
        }

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """包裹模型调用：注入动态 prompt 并记录调用耗时。

        ``SystemMessage.content_blocks`` 的复制确保已有 system prompt 不会被覆盖。
        这里也可以扩展为模型路由、重试、缓存或降级。
        """

        print(f"ModelRequest: {request}")
        dynamic_context = (
            f"Audit run={request.state.get('run_id', 'unknown')}; "
            f"model-call={request.state.get('model_calls', 0)}."
        )
        prior_blocks = (
            list(request.system_message.content_blocks)
            if request.system_message
            else []
        )
        request = request.override(
            system_message=SystemMessage(
                content=[*prior_blocks, {"type": "text", "text": dynamic_context}]
            )
        )

        started = time.perf_counter()
        self._record("wrap_model_call", "calling model with dynamic audit context")
        response = handler(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        self._record("wrap_model_call", f"model returned in {elapsed_ms:.1f}ms")
        return response

    def after_model(self, state: AuditState, runtime: Runtime) -> dict[str, Any]:
        """每轮模型响应后：记录模型是否选择了工具。"""
        tool_calls = getattr(state["messages"][-1], "tool_calls", [])
        return {
            "audit_events": [
                self._record("after_model", f"tool_calls={len(tool_calls)}")
            ]
        }

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """包裹每个工具调用；将异常变成 agent 可处理的 tool error。"""
        name = request.tool_call["name"]
        self._record(
            "wrap_tool_call", f"calling {name} with {request.tool_call['args']}"
        )
        try:
            result = handler(request)
        except Exception as exc:  # noqa: BLE001 - 所有工具错误都应反馈给 agent。
            self._record("wrap_tool_call", f"{name} failed: {exc}")
            return ToolMessage(
                content=f"Tool {name} failed: {exc}",
                tool_call_id=request.tool_call["id"],
                status="error",
            )
        self._record("wrap_tool_call", f"{name} completed")
        return result

    def after_agent(self, state: AuditState, runtime: Runtime) -> dict[str, Any]:
        """执行一次：输出本次运行的最终汇总。"""
        elapsed_ms = (
            time.perf_counter() - state.get("started_at", time.perf_counter())
        ) * 1000
        detail = (
            f"model_calls={state.get('model_calls', 0)}, elapsed={elapsed_ms:.1f}ms"
        )
        return {"audit_events": [self._record("after_agent", detail)]}


@tool
def multiply(left: int, right: int) -> str:
    """Return the product of two integers."""
    return str(left * right)


def build_agent():
    """一个 Middleware 类实例，即可挂接全部生命周期。"""
    from chat_models.chat import chat_model

    return create_agent(
        model=chat_model,
        tools=[],
        system_prompt="You are a concise arithmetic assistant. Use multiply for multiplication.",
        middleware=[AuditMiddleware()],
    )


def main() -> None:
    result = build_agent().invoke(
        {"messages": [{"role": "user", "content": "计算 6 × 7"}]}
    )
    print("\n最终回答:", result["messages"][-1].content)
    print("审计事件:")
    for event in result.get("audit_events", []):
        print(f"- {event}")


if __name__ == "__main__":
    main()
