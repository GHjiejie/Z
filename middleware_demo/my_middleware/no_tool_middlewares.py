"""三个不使用工具的 LangChain ``wrap_model_call`` middleware 示例。

运行：

    uv run python -m middleware_demo.my_middleware.no_tool_middlewares
"""

from collections.abc import Callable

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.messages import HumanMessage, SystemMessage

from chat_models.chat import chat_model

ModelHandler = Callable[[ModelRequest], ModelResponse]


class AddSystemMessageMiddleware(AgentMiddleware):
    """Middleware 1：动态设置 system message。"""

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: ModelHandler,
    ) -> ModelResponse:
        print(f"\nAddSystemMessageMiddleware 中间件的 request：\n{request}\n")
        request.system_message = SystemMessage(
            content="AddSystemMessageMiddleware中间件设置的系统消息。"
        )
        request_to_next = request

        return handler(request_to_next)


class AddUserMessageMiddleware(AgentMiddleware):
    """Middleware 2：动态追加一条 message。"""

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: ModelHandler,
    ) -> ModelResponse:
        print(f"\nAddUserMessageMiddleware 中间件的 request：\n{request}\n")
        request.messages.append(
            HumanMessage(content="AddUserMessageMiddleware中间件追加的用户消息。")
        )
        request_to_next = request

        return handler(request_to_next)


class ModelCallMiddleware(AgentMiddleware):
    """Middleware 3：展示最终 request 并调用模型。"""

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: ModelHandler,
    ) -> ModelResponse:
        print(f"\nModelCallMiddleware 中间件的 request：\n{request}\n")
        request_to_next = request
        response = handler(request_to_next)
        print(f"\n模型调用结束，输出 response：\n{response}\n")
        return response


def build_agent():
    return create_agent(
        model=chat_model,
        tools=[],
        middleware=[
            AddSystemMessageMiddleware(),
            AddUserMessageMiddleware(),
            ModelCallMiddleware(),
        ],
    )


def main() -> None:
    result = build_agent().invoke(
        {"messages": [{"role": "user", "content": "请介绍 LangChain middleware。"}]}
    )
    print(f"\n最终回答：\n{result['messages'][-1].content}")


if __name__ == "__main__":
    main()
