"""核心概念 1：Model（模型）。"""

import asyncio

from langchain.agents import create_agent

try:
    from .concept import get_chat_model, print_sse_stream
except ImportError:
    from concept import get_chat_model, print_sse_stream


async def run_demo() -> None:
    # 传入模型实例可以完整保留 chat_models/chat.py 中的模型配置。
    agent = create_agent(model=get_chat_model(), tools=[])
    await print_sse_stream(
        agent,
        {"messages": [{"role": "user", "content": "用一句话说明 Agent 中模型的职责。"}]}
    )


if __name__ == "__main__":
    asyncio.run(run_demo())
