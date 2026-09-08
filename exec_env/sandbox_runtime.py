"""创建 Daytona Sandbox，并集中定义 Demo 使用的公共路径。"""

import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from daytona import CreateSandboxFromSnapshotParams, Daytona, Sandbox
from dotenv import load_dotenv
from langchain_daytona import DaytonaSandbox

PROJECT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(PROJECT_ENV_FILE, override=True)

SANDBOX_HOME = "/home/daytona"
INPUT_PATH = f"{SANDBOX_HOME}/input.txt"
HOST_REPORT_PATH = f"{SANDBOX_HOME}/host-created-report.txt"
AGENT_SCRIPT_PATH = f"{SANDBOX_HOME}/word_frequency.py"
AGENT_REPORT_PATH = f"{SANDBOX_HOME}/agent-report.txt"

INPUT_TEXT = b"""Deep Agents can write files.
Deep Agents can run code in a sandbox.
A sandbox protects the host environment.
"""


@dataclass(frozen=True)
class SandboxRuntime:
    """一次 Demo 运行所需的 Daytona 对象。"""

    client: Daytona
    sandbox: Sandbox
    backend: DaytonaSandbox


def require_daytona_api_key() -> None:
    """在创建远程资源之前检查 Daytona 凭据。"""
    if not os.getenv("DAYTONA_API_KEY"):
        raise SystemExit(
            "缺少环境变量：DAYTONA_API_KEY。"
            "请在 https://app.daytona.io/dashboard/keys 创建。"
        )


def create_sandbox_runtime(demo_name: str) -> SandboxRuntime:
    """创建一个默认保留、不会自动删除的 Python Sandbox。"""
    require_daytona_api_key()

    client = Daytona()
    sandbox = client.create(
        CreateSandboxFromSnapshotParams(
            language="python",
            name=f"deepagents-{demo_name}-{uuid4().hex[:8]}",
            # 0 表示不自动删除，便于运行后进入控制台检查文件。
            auto_delete_interval=0,
        )
    )
    backend = DaytonaSandbox(sandbox=sandbox)

    print(f"已创建 Daytona Sandbox：{sandbox.name} ({sandbox.id})")
    return SandboxRuntime(client=client, sandbox=sandbox, backend=backend)


def print_retained_sandbox(runtime: SandboxRuntime) -> None:
    """打印保留的 Sandbox 信息和手动清理提醒。"""
    print("\nSandbox 已保留，没有自动删除。")
    print(f"Sandbox 名称：{runtime.sandbox.name}")
    print(f"Sandbox ID：{runtime.sandbox.id}")
    print("查看地址：https://app.daytona.io/dashboard/sandboxes")
    print("查看完成后，请在 Daytona 控制台手动删除，避免持续占用资源。")
