"""核心概念 3：System prompt（系统提示词）。"""

import asyncio

from langchain.agents import create_agent

try:
    from .concept import get_chat_model, print_sse_stream
except ImportError:
    from concept import get_chat_model, print_sse_stream


async def run_demo() -> None:
    agent = create_agent(
        model=get_chat_model(),
        tools=[],
        system_prompt=(
            "你是一名耐心的 LangChain 入门老师。"
            "使用简体中文，只回答两句话，并用生活化的比喻解释概念。"
        ),
    )
    await print_sse_stream(
        agent,
        {"messages": [{"role": "user", "content": "什么是 Agent？"}]},
    )


if __name__ == "__main__":
    asyncio.run(run_demo())
