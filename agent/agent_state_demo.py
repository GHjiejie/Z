"""核心概念 5：Agent state（Agent 状态）。"""

import asyncio
from typing import Any

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import ModelRequest, dynamic_prompt

try:
    from .concept import get_chat_model, print_sse_stream
except ImportError:
    from concept import get_chat_model, print_sse_stream


class LearningState(AgentState):
    """在内置 messages 之外增加当前学习者的信息。"""

    learner_level: str
    learning_topic: str


@dynamic_prompt
def prompt_from_state(request: ModelRequest) -> str:
    state: dict[str, Any] = request.state or {}
    level = state.get("learner_level", "未知")
    topic = state.get("learning_topic", "当前主题")
    return (
        f"你正在辅导一位{level}学习者，主题是{topic}。"
        "回答时明确提及学习者级别，并将回答控制在两句话内。"
    )


async def run_demo() -> None:
    agent = create_agent(
        model=get_chat_model(),
        tools=[],
        state_schema=LearningState,
        middleware=[prompt_from_state],
    )
    await print_sse_stream(
        agent,
        {
            "messages": [{"role": "user", "content": "请给我一条学习建议。"}],
            "learner_level": "初学者",
            "learning_topic": "LangChain create_agent",
        },
    )


if __name__ == "__main__":
    asyncio.run(run_demo())
