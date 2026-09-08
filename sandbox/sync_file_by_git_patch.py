"""通过 Git diff/patch 将 Sandbox 中的改动同步回本地。

运行：``uv run python -m sandbox.sync_file_by_git_patch --local-workspace ./path``
/Users/zhengjie/Github/Z/sandbox
"""

import argparse
import os
import shlex
import subprocess
from pathlib import Path
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
from langchain_daytona import DaytonaSandbox

PROJECT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(PROJECT_ENV_FILE, override=True)


def parse_args() -> argparse.Namespace:
    """读取本地 Git 工作区和补丁确认选项。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--local-workspace",
        type=Path,
        required=True,
        help="需要交给 Agent 修改的本地 Git 仓库或仓库子目录。",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="通过 git apply --check 后直接应用补丁，不再询问确认。",
    )
    return parser.parse_args()


def get_or_create_sandbox(daytona: Daytona) -> Sandbox:
    """按名称复用 Sandbox，仅在不存在时创建。"""
    name = os.getenv("DAYTONA_SANDBOX_NAME", "deepagents-git-patch-demo")

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


def resolve_git_workspace(local_workspace: Path) -> tuple[Path, Path]:
    """返回 Git 仓库根目录及用户选择的仓库内相对目录。"""
    workspace = local_workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise SystemExit(f"本地工作区不存在或不是目录：{workspace}")

    result = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"本地工作区不是 Git 仓库：{workspace}")

    repo_root = Path(result.stdout.strip()).resolve()
    return repo_root, workspace.relative_to(repo_root)


def collect_workspace_files(repo_root: Path, scope: Path) -> list[tuple[str, bytes]]:
    """读取 Git 已跟踪和未忽略的未跟踪文件，作为 Sandbox 基线。"""
    scope_arg = "." if scope == Path(".") else scope.as_posix()
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            scope_arg,
        ],
        check=True,
        capture_output=True,
    )

    files: list[tuple[str, bytes]] = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative_path = Path(os.fsdecode(raw_path))
        local_path = repo_root / relative_path
        if local_path.is_symlink():
            raise RuntimeError(f"Demo 暂不支持同步符号链接：{local_path}")
        if local_path.is_file():
            files.append((relative_path.as_posix(), local_path.read_bytes()))
    return files


def prepare_remote_repository(
    backend: DaytonaSandbox,
    sandbox: Sandbox,
    repo_root: Path,
    scope: Path,
) -> tuple[str, str, str]:
    """上传本地工作区，并在 Sandbox 中建立 Git 基线提交。"""
    remote_repo = f"{sandbox.get_work_dir()}/git-runs/{uuid4().hex[:8]}"
    remote_scope = (
        remote_repo if scope == Path(".") else f"{remote_repo}/{scope.as_posix()}"
    )

    setup = backend.execute(
        f"mkdir -p {shlex.quote(remote_scope)} && "
        f"cd {shlex.quote(remote_repo)} && git init -q"
    )
    if setup.exit_code != 0:
        raise RuntimeError(f"初始化远程 Git 仓库失败：{setup.output}")

    local_files = collect_workspace_files(repo_root, scope)
    uploads = backend.upload_files(
        [
            (f"{remote_repo}/{relative_path}", content)
            for relative_path, content in local_files
        ]
    )
    for upload in uploads:
        if upload.error is not None:
            raise RuntimeError(f"上传 {upload.path} 失败：{upload.error}")

    baseline = backend.execute(
        f"cd {shlex.quote(remote_repo)} && "
        "git config user.name 'Deep Agents Sandbox' && "
        "git config user.email 'sandbox@example.invalid' && "
        "git add -A && git commit --allow-empty -qm baseline && git rev-parse HEAD"
    )
    if baseline.exit_code != 0:
        raise RuntimeError(f"创建远程 Git 基线失败：{baseline.output}")

    baseline_commit = baseline.output.strip().splitlines()[-1]
    print(f"Sandbox Git 工作区：{remote_scope}")
    print(f"已上传 {len(local_files)} 个文件，基线：{baseline_commit[:12]}")
    return remote_repo, remote_scope, baseline_commit


def run_agent(
    backend: DaytonaSandbox,
    remote_scope: str,
    user_request: str,
) -> int | None:
    """流式运行 Agent，并返回最后一次 execute 的退出码。"""
    from chat_models.chat import chat_model

    agent = create_deep_agent(
        model=chat_model,
        backend=backend,
        system_prompt=f"""
本次任务的 Git 工作目录是 {remote_scope}。
用户提到“当前目录”时，指的就是这个目录。
只能修改该目录内的文件；运行命令时先进入该目录。
涉及代码时必须实际运行测试。不要执行 git commit。
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
    return last_execute_exit_code


def create_patch(
    backend: DaytonaSandbox,
    sandbox: Sandbox,
    remote_repo: str,
    scope: Path,
    baseline_commit: str,
) -> bytes:
    """在 Sandbox 中通过 git diff --binary 生成并下载补丁。"""
    scope_arg = "." if scope == Path(".") else scope.as_posix()
    patch_path = f"{sandbox.get_work_dir()}/patches/{uuid4().hex[:8]}.patch"
    command = (
        f"mkdir -p {shlex.quote(str(Path(patch_path).parent))} && "
        f"cd {shlex.quote(remote_repo)} && "
        "git add -N -- . && "
        f"git diff --binary --full-index --no-ext-diff {shlex.quote(baseline_commit)} "
        f"-- {shlex.quote(scope_arg)} > {shlex.quote(patch_path)}"
    )
    result = backend.execute(command)
    if result.exit_code != 0:
        raise RuntimeError(f"生成 Git patch 失败：{result.output}")

    download = backend.download_files([patch_path])[0]
    if download.content is None:
        raise RuntimeError(f"下载 Git patch 失败：{download.error}")
    return download.content


def apply_patch(repo_root: Path, patch: bytes, *, assume_yes: bool) -> None:
    """检查、展示并应用 Sandbox 生成的 Git patch。"""
    if not patch.strip():
        print("Sandbox 没有产生 Git 文件改动。")
        return

    check = subprocess.run(
        ["git", "apply", "--check", "--binary", "-"],
        cwd=repo_root,
        input=patch,
        check=False,
        capture_output=True,
    )
    if check.returncode != 0:
        raise RuntimeError(
            "Git patch 无法安全应用到当前本地工作区：\n"
            + check.stderr.decode(errors="replace")
        )

    stat = subprocess.run(
        ["git", "apply", "--stat", "-"],
        cwd=repo_root,
        input=patch,
        check=True,
        capture_output=True,
        text=False,
    )
    print("\nGit patch 统计：")
    print(stat.stdout.decode(errors="replace"), end="")

    if not assume_yes:
        confirmed = input("将以上补丁应用到本地工作区？[y/N] ").strip().lower()
        if confirmed not in {"y", "yes"}:
            print("已取消，本地工作区没有被修改。")
            return

    applied = subprocess.run(
        ["git", "apply", "--binary", "-"],
        cwd=repo_root,
        input=patch,
        check=False,
        capture_output=True,
    )
    if applied.returncode != 0:
        raise RuntimeError(
            "应用 Git patch 失败：\n" + applied.stderr.decode(errors="replace")
        )
    print(f"Git patch 已应用到：{repo_root}")


def main() -> None:
    """在 Sandbox 修改并测试代码，再以 Git patch 同步到本地。"""
    args = parse_args()
    print(f"本地工作区：{args}")
    if not os.getenv("DAYTONA_API_KEY"):
        raise SystemExit("请在项目根目录的 .env 中配置 DAYTONA_API_KEY")

    repo_root, scope = resolve_git_workspace(args.local_workspace)
    user_request = input("请输入任务：").strip()
    if not user_request:
        raise SystemExit("任务不能为空")

    daytona = Daytona()
    sandbox = get_or_create_sandbox(daytona)
    backend = DaytonaSandbox(sandbox=sandbox)

    try:
        remote_repo, remote_scope, baseline_commit = prepare_remote_repository(
            backend,
            sandbox,
            repo_root,
            scope,
        )
        last_execute_exit_code = run_agent(backend, remote_scope, user_request)
        if last_execute_exit_code not in (None, 0):
            raise RuntimeError(
                f"Sandbox 最后一次代码执行失败（exit_code={last_execute_exit_code}），"
                "不会生成或应用补丁。"
            )

        patch = create_patch(
            backend,
            sandbox,
            remote_repo,
            scope,
            baseline_commit,
        )
        apply_patch(repo_root, patch, assume_yes=args.yes)
    finally:
        print("\nSandbox 已保留，没有自动删除。")
        print(f"Sandbox 名称：{sandbox.name}")
        print(f"Sandbox ID：{sandbox.id}")
        print("查看地址：https://app.daytona.io/dashboard/sandboxes")


if __name__ == "__main__":
    main()
