"""从 Snapshot 创建并复用 Daytona Sandbox。

运行：``uv run python -m sandbox.sync_file_by_download --local-workspace ./output``
"""

import argparse
import os
import shlex
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from daytona import (
    CreateSandboxFromSnapshotParams,
    Daytona,
    DaytonaNotFoundError,
    Sandbox,
    SandboxState,
)
from deepagents import create_deep_agent
from dotenv import load_dotenv
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.output_parsers import PydanticOutputParser
from langchain_daytona import DaytonaSandbox
from pydantic import BaseModel, Field

PROJECT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(PROJECT_ENV_FILE, override=True)


class SyncPlan(BaseModel):
    """模型生成的本地同步计划。"""

    should_sync: bool = Field(description="本次任务是否产生了需要同步到本地的文件")
    relative_directory: str | None = Field(
        description="相对于项目根目录的目标目录，例如 sandbox、examples/demo 或 ."
    )
    source: Literal["user", "inferred", "none"] = Field(
        description="目标目录来自用户明确指定、模型推断，或无法确定"
    )
    reason: str = Field(description="选择该目录的简短理由")


def parse_args() -> argparse.Namespace:
    """读取显式指定的本地同步目录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--local-workspace",
        type=Path,
        help="测试通过后同步文件的本地目录；不指定则由模型根据用户任务推断。",
    )
    return parser.parse_args()


def list_local_directories(root: Path, max_depth: int = 3) -> list[str]:
    """列出可供模型选择的项目目录，不读取文件内容。"""
    ignored = {".git", ".idea", ".pytest_cache", ".venv", "__pycache__", "node_modules"}
    directories = ["."]

    for current, names, _ in os.walk(root):
        current_path = Path(current)
        relative = current_path.relative_to(root)
        depth = len(relative.parts)
        names[:] = sorted(
            name for name in names if name not in ignored and not name.startswith(".")
        )
        if depth >= max_depth:
            names.clear()
            continue
        directories.extend((relative / name).as_posix() for name in names)

    return directories[:200]


def infer_local_workspace(model: BaseChatModel, user_request: str) -> Path | None:
    """让模型规划同步目录，并在宿主机上验证其安全范围。"""
    candidates = "\n".join(
        f"- {path}" for path in list_local_directories(PROJECT_ENV_FILE.parent)
    )
    parser = PydanticOutputParser(pydantic_object=SyncPlan)
    try:
        response = model.invoke(
            f"""
你只负责决定 Sandbox 产物应该同步到本地项目的哪个目录，不执行任务。

规则：
1. 用户明确指定项目内目录时必须优先使用，并将 source 设为 user；
2. 用户没有指定时，可以根据任务语义和已有目录推断，并将 source 设为 inferred；
3. 推断时只能选择下面已有的目录；
4. 用户明确指定的新目录可以返回，但必须是项目内相对路径；
5. 禁止返回绝对路径、.. 或项目外路径；
6. 不产生文件或无法可靠判断时，should_sync=false。

项目内现有目录：
{candidates}

用户任务：
{user_request}

只返回符合以下格式要求的 JSON，不要使用 Markdown，也不要添加解释：
{parser.get_format_instructions()}
"""
        )
        plan = parser.parse(response.text)
    except Exception as exc:
        print(f"模型无法确定本地同步目录，将只保留 Sandbox 文件：{exc}")
        return None

    print(f"同步目录判断：{plan.reason}")
    if not plan.should_sync or not plan.relative_directory:
        return None

    relative_directory = Path(plan.relative_directory)
    if relative_directory.is_absolute() or ".." in relative_directory.parts:
        raise RuntimeError(f"模型返回了不安全的同步目录：{plan.relative_directory}")

    project_root = PROJECT_ENV_FILE.parent.resolve()
    local_workspace = (project_root / relative_directory).resolve()
    if not local_workspace.is_relative_to(project_root):
        raise RuntimeError(f"模型返回了项目外的同步目录：{local_workspace}")

    if not local_workspace.exists():
        if plan.source != "user":
            print(f"推断目录不存在，取消自动同步：{local_workspace}")
            return None
        local_workspace.mkdir(parents=True)
        print(f"已创建用户指定的本地目录：{local_workspace}")
    elif not local_workspace.is_dir():
        raise RuntimeError(f"同步目标不是目录：{local_workspace}")

    print(f"本次任务将同步到：{local_workspace}")
    return local_workspace


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


def sync_generated_files(
    backend: DaytonaSandbox,
    remote_workspace: str,
    local_workspace: Path,
) -> None:
    """把本次任务生成的文件安全地下载到本地工作目录。"""
    result = backend.glob("**/*", path=remote_workspace)
    if result.error:
        raise RuntimeError(f"获取 Sandbox 文件列表失败：{result.error}")

    remote_root = PurePosixPath(remote_workspace)
    ignored_dirs = {".git", ".pytest_cache", ".venv", "__pycache__", "node_modules"}
    remote_paths: list[str] = []
    local_paths: list[Path] = []

    for match in result.matches or []:
        remote_path = PurePosixPath(match["path"])
        relative_path = remote_path.relative_to(remote_root)
        if any(part in ignored_dirs for part in relative_path.parts):
            continue

        local_path = (local_workspace / Path(*relative_path.parts)).resolve()
        if not local_path.is_relative_to(local_workspace):
            raise RuntimeError(f"拒绝同步工作目录之外的路径：{local_path}")
        remote_paths.append(str(remote_path))
        local_paths.append(local_path)

    if not remote_paths:
        print("\n本次任务没有生成需要同步的文件。")
        return

    existing_paths = [path for path in local_paths if path.exists()]
    if existing_paths:
        paths = "\n".join(f"- {path}" for path in existing_paths)
        raise FileExistsError(f"以下本地文件已存在，为避免覆盖已停止同步：\n{paths}")

    downloads = backend.download_files(remote_paths)
    for download, local_path in zip(downloads, local_paths, strict=True):
        if download.content is None:
            raise RuntimeError(f"下载 {download.path} 失败：{download.error}")

    for download, local_path in zip(downloads, local_paths, strict=True):
        assert download.content is not None
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(download.content)
        print(f"已同步：{download.path} -> {local_path}")


def main() -> None:
    """让 Agent 在复用的 Sandbox 中工作并同步生成文件。"""
    args = parse_args()
    if not os.getenv("DAYTONA_API_KEY"):
        raise SystemExit("请在项目根目录的 .env 中配置 DAYTONA_API_KEY")

    # .env 加载后再初始化项目中配置的真实模型。
    from chat_models.chat import chat_model

    user_request = input("请输入任务：").strip()
    if not user_request:
        raise SystemExit("任务不能为空")

    if args.local_workspace is not None:
        local_workspace = args.local_workspace.expanduser().resolve()
        if not local_workspace.is_dir():
            raise SystemExit(f"本地同步目录不存在或不是目录：{local_workspace}")
        print(f"使用命令行指定的同步目录：{local_workspace}")
    else:
        local_workspace = infer_local_workspace(chat_model, user_request)

    daytona = Daytona()
    sandbox = get_or_create_sandbox(daytona)
    backend = DaytonaSandbox(sandbox=sandbox)
    remote_workspace = f"{sandbox.get_work_dir()}/runs/{uuid4().hex[:8]}"
    print(f"本次任务的 Sandbox 工作目录：{remote_workspace}")
    setup = backend.execute(f"mkdir -p {shlex.quote(remote_workspace)}")
    if setup.exit_code != 0:
        raise RuntimeError(f"创建 Sandbox 工作目录失败：{setup.output}")

    try:
        # Deep Agents 会将 backend 的 execute 和文件操作能力自动提供给模型。
        agent = create_deep_agent(
            model=chat_model,
            backend=backend,
            system_prompt=f"""
本次任务的工作目录是 {remote_workspace}。
用户提到“当前目录”时，指的就是这个目录。
所有新建和修改的文件都必须放在该目录中；运行命令时先进入该目录。
涉及代码时，必须在 Sandbox 中实际运行测试，测试通过后再回答。
""",
        )

        print("Agent 开始处理……", flush=True)
        last_execute_exit_code: int | None = None
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
                if message.name == "execute" and isinstance(message.artifact, dict):
                    last_execute_exit_code = message.artifact.get("exit_code")
            elif message.type == "ai" and message.text and not tool_call_chunks:
                print(message.text, end="", flush=True)

        print()
        if last_execute_exit_code not in (None, 0):
            raise RuntimeError(
                f"Sandbox 最后一次代码执行失败（exit_code={last_execute_exit_code}），"
                "不会同步文件到本地。"
            )
        if local_workspace is None:
            print(f"未指定本地同步目录，文件保留在：{remote_workspace}")
        else:
            sync_generated_files(backend, remote_workspace, local_workspace)
    finally:
        print("\nSandbox 已保留，没有自动删除。")
        print(f"Sandbox 名称：{sandbox.name}")
        print(f"Sandbox ID：{sandbox.id}")
        print("查看地址：https://app.daytona.io/dashboard/sandboxes")


if __name__ == "__main__":
    main()
