"""Deep Agents CompositeBackend demo.

路由规则：

- 普通路径（例如 ``/notes.txt``）由 ``StateBackend`` 保存，只属于当前线程。
- ``/memories/`` 下的路径由 ``FilesystemBackend`` 保存到本地磁盘，可跨线程、
  跨进程重启访问。

运行：

    uv run python -m exec_env.composite
"""

from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from langchain.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from chat_models.chat import chat_model

# 使用相对于当前 Python 文件的固定路径，避免从不同工作目录启动时写到不同位置。
MEMORIES_DIR = Path(__file__).resolve().parent / "composite_data" / "memories"

backend = CompositeBackend(
    default=StateBackend(),
    routes={
        "/memories/": FilesystemBackend(
            root_dir=MEMORIES_DIR,
            # 将 root_dir 作为虚拟根目录，并阻止通过 .. 或 ~ 越界访问。
            virtual_mode=True,
        ),
    },
)

agent = create_deep_agent(
    model=chat_model,
    backend=backend,
    # StateBackend 的文件位于 Agent state 中；checkpointer 让同一线程的
    # 多次 invoke 能继续访问自己的普通文件和对话历史。
    checkpointer=InMemorySaver(),
)


def get_answer(result: dict[str, Any]) -> str:
    """从 Agent 返回值中提取最后一条消息的文本。"""
    messages = result.get("messages", [])
    if not messages:
        return str(result)

    content = messages[-1].content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if text_parts:
            return "\n".join(text_parts)
    return str(content)


def main() -> None:
    thread_id = "thread-a"
    while True:
        question = input(f"\n[{thread_id}] 你: ").strip()

        result = agent.invoke(
            {"messages": [HumanMessage(content=question)]},
            config={"configurable": {"thread_id": thread_id}},
        )
        print(f"\nAgent: {get_answer(result)}")


if __name__ == "__main__":
    main()
