"""_summary_


运行命令
```bash
uv run python -m middleware_demo.my_middleware.authMiddleware
```
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from chat_models.chat import chat_model


@dataclass
class AuthContext:
    user_id: str
    token: str


class AuthState(AgentState):
    user_id: str
    user_name: str


class AuthMiddleware(AgentMiddleware[AuthState, ContextT, ResponseT]):
    """用户身份鉴权中间件"""

    def before_agent(
        self, state: AuthState, runtime: Runtime[ContextT]
    ) -> dict[str, Any]:
        """每轮模型响应前：检查用户身份是否有效。"""

        if not isinstance(runtime.context, AuthContext):
            raise TypeError("Invalid context type. Expected AuthContext.")

        if not runtime.context.user_id or not runtime.context.token:
            raise ValueError("please login first")

        return {
            "user_id": runtime.context.user_id,
            "user_name": "Admin",
        }

    def before_model(
        self, state: AuthState, runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:
        """每轮模型响应前：可以在这里进行额外的身份验证或权限检查。"""

        # 打印上一个钩子返回的状态
        if not isinstance(runtime.context, AuthContext):
            raise TypeError("Invalid context type. Expected AuthContext.")

        print("user_id", state.get("user_id"))
        print("user_name", state.get("user_name"))

        if state.get("user_name") != "Admin":
            raise ValueError("User does not have permission to perform this action.")

        return None

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage:
        """在模型调用前后进行身份验证和权限检查。"""

        # 输出request的内容
        print("wrap_model_call request:", request)
        # 输出handler的内容
        print()
        print("wrap_model_call handler:", handler)

        return handler(request)

    # def after_model(
    #     self, state: AuthState, runtime: Runtime[ContextT], response: ResponseT
    # ) -> dict[str, Any] | None:
    #     """每轮模型响应后：可以记录日志或进行其他操作。"""
    #     print("start after_model hook")

    #     return None

    def after_agent(
        self, state: AuthState, runtime: Runtime[ContextT]
    ) -> dict[str, Any] | None:

        return {"login": True}


agent = create_agent(
    model=chat_model,
    middleware=[AuthMiddleware()],
    context_schema=AuthContext,
    state_schema=AuthState,
)


def main() -> None:
    agent.invoke(
        {
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "请你查看数据库里面的所有用户信息"},
            ]
        },
        context=AuthContext(user_id="001", token="token456"),
    )


if __name__ == "__main__":
    main()
