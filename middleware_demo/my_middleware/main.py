"""三个简洁直观的 LangChain ``wrap_model_call`` middleware。"""

from collections.abc import Callable
from pprint import pprint
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.messages import HumanMessage, ToolMessage
from langchain.tools import tool
from langchain.tools.tool_node import ToolCallRequest
from langgraph.types import Command

from chat_models.chat import chat_model

ModelHandler = Callable[[ModelRequest], ModelResponse]


@tool
def count_characters(text: str) -> int:
    """计算一段文本包含多少个字符。"""
    return len(text)


def show_request(name: str, request_to_next: ModelRequest) -> None:
    """只展示最关心的两个字段：messages 和 tools。"""
    request_view = {
        "messages": [message.content for message in request_to_next.messages],
        "tools": [
            tool.name if hasattr(tool, "name") else str(tool)
            for tool in request_to_next.tools
        ],
    }
    print(f"\n{name} 传给下一个 middleware 的 request：")
    pprint(request_view, sort_dicts=False)
    print()


class AddMessageMiddleware(AgentMiddleware):
    """Middleware 1：动态追加一条 message。"""

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: ModelHandler,
    ) -> ModelResponse:
        request_to_next = request.override(
            messages=[
                *request.messages,
                HumanMessage(content="补充要求：回答时请说明你是否使用了工具。"),
            ]
        )
        show_request(type(self).__name__, request_to_next)
        return handler(request_to_next)


class AddToolMiddleware(AgentMiddleware):
    """Middleware 2：问题涉及字符数量时，动态添加工具。"""

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: ModelHandler,
    ) -> ModelResponse:
        user_text = " ".join(str(message.content) for message in request.messages)
        tools_to_next = list(request.tools)

        if "字符" in user_text and count_characters.name not in {
            getattr(tool, "name", None) for tool in tools_to_next
        }:
            tools_to_next.append(count_characters)

        request_to_next = request.override(tools=tools_to_next)
        show_request(type(self).__name__, request_to_next)
        return handler(request_to_next)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """让运行时能够执行由 wrap_model_call 动态添加的工具。"""
        request_to_next = request
        if request.tool_call["name"] == count_characters.name:
            request_to_next = request.override(tool=count_characters)
        return handler(request_to_next)


class ShowFinalRequestMiddleware(AgentMiddleware):
    """Middleware 3：展示最终交给模型的 request，不做修改。"""

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: ModelHandler,
    ) -> ModelResponse:
        request_to_next = request
        show_request(type(self).__name__, request_to_next)
        return handler(request_to_next)


def build_agent():
    return create_agent(
        model=chat_model,
        tools=[],
        middleware=[
            AddMessageMiddleware(),
            AddToolMiddleware(),
            ShowFinalRequestMiddleware(),
        ],
    )


def main() -> None:
    result = build_agent().invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "请计算 'LangChain middleware' 包含多少个字符。",
                }
            ]
        }
    )
    print(f"\n最终回答：\n{result['messages'][-1].content}")


if __name__ == "__main__":
    main()
