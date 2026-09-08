"""Deep Agents Skills 渐进式加载 demo。

运行交互模式：

    uv run python -m skills_demo.main

也可以直接提供一条问题：

    uv run python -m skills_demo.main "用三句话欢迎新同事小林"
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from dotenv import load_dotenv
from langchain.agents import create_agent

DEMO_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = DEMO_ROOT.parent
TEXT_STATS_SCRIPT = DEMO_ROOT / "skills" / "text-stats" / "scripts" / "text_stats.py"

# chat_models.chat 在导入时读取模型环境变量，所以先加载项目根目录的 .env。
load_dotenv(PROJECT_ROOT / ".env", override=True)


def build_agent():
    """创建从 ``skills_demo/skills`` 自动发现 skills 的 agent。"""
    # 延迟导入，让 .env 在 ChatOpenAI 初始化前已就绪。
    from chat_models.chat import chat_model

    return create_deep_agent(
        model=chat_model,
        backend=FilesystemBackend(root_dir=DEMO_ROOT, virtual_mode=True),
        # skills 路径指向“包含多个 skill 目录”的顶层目录。
        skills=["/skills/"],
    )
    return create_agent(
        model=chat_model,
    )


def answer_text(result: dict[str, Any]) -> str:
    """从 agent 返回值中提取最后一条文本消息。"""
    messages = result.get("messages", [])
    if not messages:
        return str(result)

    content = messages[-1].content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if parts:
            return "\n".join(parts)
    return str(content)


def invoke(agent: Any, question: str) -> None:
    result = agent.invoke({"messages": [{"role": "user", "content": question}]})
    print(f"\nAgent: {answer_text(result)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deep Agents Skills demo")
    parser.add_argument("question", nargs="?", help="单次运行时要发给 agent 的问题")
    args = parser.parse_args()

    agent = build_agent()
    if args.question:
        invoke(agent, args.question)
        return

    print("已加载 skills_demo/skills，输入 exit 退出。")
    while True:
        try:
            question = input("\nuser: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if question.lower() in {"exit", "quit"}:
            break
        if question:
            invoke(agent, question)


if __name__ == "__main__":
    main()
