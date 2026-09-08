"""一个可复用的 Middleware 类，配合 ``create_deep_agent`` 使用。

运行：

    uv run python -m middleware_demo.create_deep_agent_middleware
"""

from __future__ import annotations

import operator
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, NotRequired

from deepagents import create_deep_agent
from dotenv import load_dotenv
from langchain.agents import AgentState
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    hook_config,
)
from langchain.messages import AIMessage, SystemMessage
from langgraph.runtime import Runtime

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=True)


class DeepAuditState(AgentState):
    """Deep Agent 的 lifecycle hook 共享状态。"""

    run_id: NotRequired[str]
    started_at: NotRequired[float]
    model_calls: NotRequired[int]
    audit_events: Annotated[list[str], operator.add]


class DeepAuditMiddleware(AgentMiddleware[DeepAuditState, None, Any]):
    """可复用的 Deep Agent 生命周期 middleware。

    运行数据始终写入 graph state，而不是实例属性，因而能安全用于并发调用。
    """

    state_schema = DeepAuditState

    @staticmethod
    def _record(stage: str, detail: str) -> str:
        event = f"{stage}: {detail}"
        print(f"[deep-middleware] {event}")
        return event

    def before_agent(self, state: DeepAuditState, runtime: Runtime) -> dict[str, Any]:
        run_id = uuid.uuid4().hex[:8]
        return {
            "run_id": run_id,
            "started_at": time.perf_counter(),
            "model_calls": 0,
            "audit_events": [self._record("before_agent", f"run_id={run_id}")],
        }

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: DeepAuditState, runtime: Runtime) -> dict[str, Any]:
        last_content = str(state["messages"][-1].content) if state["messages"] else ""
        if "/stop" in last_content:
            return {
                "messages": [AIMessage("请求已由 Deep Agent middleware 提前终止。")],
                "audit_events": [
                    self._record("before_model", "matched /stop; jump_to=end")
                ],
                "jump_to": "end",
            }
        count = state.get("model_calls", 0) + 1
        return {
            "model_calls": count,
            "audit_events": [
                self._record("before_model", f"planning/model call={count}")
            ],
        }

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """保留 Deep Agent 原始提示词并附加每轮的动态审计上下文。"""
        prior_blocks = (
            list(request.system_message.content_blocks)
            if request.system_message
            else []
        )
        dynamic_context = (
            f"Runtime audit: run={request.state.get('run_id', 'unknown')}; "
            f"model-call={request.state.get('model_calls', 0)}."
        )
        request = request.override(
            system_message=SystemMessage(
                content=[*prior_blocks, {"type": "text", "text": dynamic_context}]
            )
        )
        started = time.perf_counter()
        self._record("wrap_model_call", "calling model with dynamic audit context")
        response = handler(request)
        self._record(
            "wrap_model_call",
            f"model returned in {(time.perf_counter() - started) * 1000:.1f}ms",
        )
        return response

    def after_model(self, state: DeepAuditState, runtime: Runtime) -> dict[str, Any]:
        tool_calls = getattr(state["messages"][-1], "tool_calls", [])
        return {
            "audit_events": [
                self._record("after_model", f"tool_calls={len(tool_calls)}")
            ]
        }

    def after_agent(self, state: DeepAuditState, runtime: Runtime) -> dict[str, Any]:
        elapsed_ms = (
            time.perf_counter() - state.get("started_at", time.perf_counter())
        ) * 1000
        detail = (
            f"model_calls={state.get('model_calls', 0)}, elapsed={elapsed_ms:.1f}ms"
        )
        return {"audit_events": [self._record("after_agent", detail)]}


def build_agent():
    """传入一个类实例；其所有生命周期方法由框架按时机调用。"""
    from chat_models.chat import chat_model

    return create_deep_agent(
        model=chat_model,
        tools=[],
        middleware=[DeepAuditMiddleware()],
    )


def main() -> None:
    result = build_agent().invoke(
        {"messages": [{"role": "user", "content": "用一句话说明深度 Agent 的价值"}]}
    )
    print("\n最终回答:", result["messages"][-1].content)
    print("审计事件:")
    for event in result.get("audit_events", []):
        print(f"- {event}")


if __name__ == "__main__":
    main()
