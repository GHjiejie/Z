"""使用标准 Docker 镜像创建 Daytona Sandbox。

运行：``uv run python -m sandbox.docker_image_demo``
"""

import os
from pathlib import Path

from daytona import (
    CreateSandboxFromSnapshotParams,
    Daytona,
    DaytonaNotFoundError,
    Sandbox,
    SandboxState,
)
from deepagents import create_deep_agent
from dotenv import load_dotenv
from langchain_daytona import DaytonaSandbox

PROJECT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(PROJECT_ENV_FILE, override=True)


def get_or_create_sandbox(daytona: Daytona) -> Sandbox:
    """按名称复用 Sandbox，仅在不存在时创建。"""
    name = os.getenv("DAYTONA_SANDBOX_NAME", "deepagents-docker-demo")

    try:
        sandbox = daytona.get(name)
        sandbox.refresh_data()
        if sandbox.state != SandboxState.STARTED:
            sandbox.start()
        print(f"复用 Sandbox：{sandbox.name} ({sandbox.id})")
        return sandbox
    except DaytonaNotFoundError:
        sandbox = daytona.create(
            CreateSandboxFromSnapshotParams(
                name=name,
                auto_delete_interval=0,
            )
        )
        print(f"创建 Sandbox：{sandbox.name} ({sandbox.id})")
        return sandbox


def main() -> None:
    """从标准 Python 镜像创建 Sandbox，并让 Agent 使用它。"""
    if not os.getenv("DAYTONA_API_KEY"):
        raise SystemExit("请在项目根目录的 .env 中配置 DAYTONA_API_KEY")

    # .env 加载后再初始化项目中配置的真实模型。
    from chat_models.chat import chat_model

    user_request = input("请输入任务：").strip()
    if not user_request:
        raise SystemExit("任务不能为空")

    daytona = Daytona()
    sandbox = get_or_create_sandbox(daytona)
    backend = DaytonaSandbox(sandbox=sandbox)

    try:
        # Deep Agents 会将 backend 的 execute 和文件操作能力自动提供给模型。
        agent = create_deep_agent(model=chat_model, backend=backend)

        print("Agent 开始处理……", flush=True)
        for chunk in agent.stream(
            {"messages": [{"role": "user", "content": user_request}]},
            stream_mode="messages",
            subgraphs=True,
            version="v2",
        ):
            if chunk["type"] != "messages":
                continue

            message, _ = chunk["data"]
            tool_call_chunks = getattr(message, "tool_call_chunks", [])

            for tool_call in tool_call_chunks:
                if tool_call.get("name"):
                    print(f"\n[调用工具] {tool_call['name']}")
                if tool_call.get("args"):
                    print(tool_call["args"], end="", flush=True)

            if message.type == "tool":
                print(f"\n[工具结果] {message.name}")
                print(message.text, flush=True)
            elif message.type == "ai" and message.text and not tool_call_chunks:
                print(message.text, end="", flush=True)

        print()
    finally:
        print("\nSandbox 已保留，没有自动删除。")
        print(f"Sandbox 名称：{sandbox.name}")
        print(f"Sandbox ID：{sandbox.id}")
        print("查看地址：https://app.daytona.io/dashboard/sandboxes")


if __name__ == "__main__":
    main()
