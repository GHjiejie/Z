"""LangChain ``create_agent`` 的核心概念与 demo 公共代码。

LangChain 当前官方文档将 create_agent 的核心组件划分为五类：
Model、Tools、System prompt、Structured output 和 Agent state。
"""

import json
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage


@dataclass(frozen=True)
class AgentConcept:
    """一个 create_agent 核心概念。"""

    key: str
    name: str
    create_agent_parameter: str
    purpose: str
    demo_module: str


CORE_CONCEPTS: tuple[AgentConcept, ...] = (
    AgentConcept(
        key="model",
        name="Model（模型）",
        create_agent_parameter="model",
        purpose="Agent 的推理引擎，负责决定回复内容以及是否调用工具。",
        demo_module="model_demo",
    ),
    AgentConcept(
        key="tools",
        name="Tools（工具）",
        create_agent_parameter="tools",
        purpose="让模型能够执行计算、检索或调用外部系统等动作。",
        demo_module="tools_demo",
    ),
    AgentConcept(
        key="system_prompt",
        name="System prompt（系统提示词）",
        create_agent_parameter="system_prompt",
        purpose="规定 Agent 的角色、行为边界与回答方式。",
        demo_module="system_prompt_demo",
    ),
    AgentConcept(
        key="structured_output",
        name="Structured output（结构化输出）",
        create_agent_parameter="response_format",
        purpose="让最终回答通过 schema 校验，并以稳定的数据结构返回。",
        demo_module="structured_output_demo",
    ),
    AgentConcept(
        key="agent_state",
        name="Agent state（Agent 状态）",
        create_agent_parameter="state_schema",
        purpose="保存消息历史以及工具或中间件需要的自定义短期状态。",
        demo_module="agent_state_demo",
    ),
)


def get_chat_model() -> BaseChatModel:
    """复用项目 chat_models/chat.py 中配置的真实 ChatOpenAI 模型。"""

    project_root = Path(__file__).resolve().parent.parent
    project_root_string = str(project_root)
    if project_root_string not in sys.path:
        sys.path.insert(0, project_root_string)

    from chat_models.chat import chat_model

    return chat_model


class FrontendEvent(TypedDict):
    """提供给前端的统一流式事件。"""

    type: Literal["token", "tool_call", "tool_result", "structured_output", "done"]
    data: dict[str, Any]


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


async def stream_agent_events(
    agent: Any,
    agent_input: dict[str, Any],
) -> AsyncIterator[FrontendEvent]:
    """将 LangGraph v2 流转换成适合 SSE/WebSocket 的稳定前端事件。"""

    async for part in agent.astream(
        agent_input,
        stream_mode=["messages", "updates"],
        version="v2",
    ):
        if part["type"] == "messages":
            message_chunk, metadata = part["data"]
            if isinstance(message_chunk, AIMessageChunk) and message_chunk.text:
                yield {
                    "type": "token",
                    "data": {
                        "text": message_chunk.text,
                        "node": metadata.get("langgraph_node", "model"),
                    },
                }
            continue

        if part["type"] != "updates":
            continue

        for node_name, update in part["data"].items():
            if not isinstance(update, dict):
                continue

            for message in update.get("messages", []):
                if isinstance(message, AIMessage):
                    for tool_call in message.tool_calls:
                        yield {
                            "type": "tool_call",
                            "data": {
                                "id": tool_call.get("id"),
                                "name": tool_call["name"],
                                "args": tool_call["args"],
                            },
                        }
                elif isinstance(message, ToolMessage):
                    yield {
                        "type": "tool_result",
                        "data": {
                            "id": message.tool_call_id,
                            "name": message.name,
                            "content": _json_value(message.content),
                            "node": node_name,
                        },
                    }

            structured_response = update.get("structured_response")
            if structured_response is not None:
                yield {
                    "type": "structured_output",
                    "data": {"value": _json_value(structured_response)},
                }

    yield {"type": "done", "data": {}}


def to_sse(event: FrontendEvent) -> str:
    """把统一事件编码成浏览器 EventSource 可消费的 SSE 文本。"""

    data = json.dumps(event["data"], ensure_ascii=False)
    return f"event: {event['type']}\ndata: {data}\n\n"


async def print_sse_stream(agent: Any, agent_input: dict[str, Any]) -> None:
    """CLI 演示；Web 服务中可直接迭代 stream_agent_events。"""

    async for event in stream_agent_events(agent, agent_input):
        print(to_sse(event), end="", flush=True)


def print_concepts() -> None:
    print(f"create_agent 共有 {len(CORE_CONCEPTS)} 个核心概念：")
    for index, concept in enumerate(CORE_CONCEPTS, start=1):
        print(
            f"{index}. {concept.name} -> {concept.create_agent_parameter}: "
            f"{concept.purpose}（demo: {concept.demo_module}.py）"
        )


if __name__ == "__main__":
    print_concepts()
