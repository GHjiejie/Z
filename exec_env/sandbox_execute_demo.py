"""Demo 1：通过 Sandbox Backend 执行 Shell 命令。

运行：``uv run python -m exec_env.sandbox_execute_demo``
"""

from pathlib import Path

from dotenv import load_dotenv
from langchain_daytona import DaytonaSandbox

from exec_env.sandbox_runtime import create_sandbox_runtime, print_retained_sandbox

PROJECT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(PROJECT_ENV_FILE, override=True)


def run_execute_demo(backend: DaytonaSandbox) -> None:
    """证明命令运行在 Daytona，而不是本机。"""
    print("\n[1] 在 Sandbox 中执行命令")
    result = backend.execute("pwd && python --version")
    # 在sandbox里面创建一个文件
    print("\n[2] 在 Sandbox 中创建一个文件")
    result2 = backend.execute("echo 'Hello, Sandbox!' > hello.txt")
    print(result2.output)

    print(result.output)
    print(f"exit_code={result.exit_code}, truncated={result.truncated}")

    if result.exit_code != 0:
        raise RuntimeError("Sandbox 命令执行失败")


def main() -> None:
    """创建独立 Sandbox 并运行 Shell 命令示例。"""
    runtime = create_sandbox_runtime("execute-demo")
    try:
        run_execute_demo(runtime.backend)
    finally:
        print_retained_sandbox(runtime)


if __name__ == "__main__":
    main()
