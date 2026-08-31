"""Demo 3：让使用真实模型的 Deep Agent 自主操作 Sandbox。

运行：``uv run python -m exec_env.sandbox_agent_demo``
"""

from pathlib import Path

from deepagents import create_deep_agent
from dotenv import load_dotenv
from langchain_daytona import DaytonaSandbox

from exec_env.sandbox_runtime import (
    AGENT_REPORT_PATH,
    AGENT_SCRIPT_PATH,
    INPUT_PATH,
    INPUT_TEXT,
    create_sandbox_runtime,
    print_retained_sandbox,
)

PROJECT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(PROJECT_ENV_FILE, override=True)


def run_agent_demo(backend: DaytonaSandbox) -> None:
    """让 Agent 创建、执行 Python 脚本并生成可下载报告。"""
    # .env 已加载完成，再导入并初始化需要模型配置的 chat_model。
    from chat_models.chat import chat_model

    print("\n[3] 把 Sandbox 作为 Deep Agent 的 Backend")

    # Agent Demo 自行准备输入，不依赖其他 Demo 先执行。
    uploads = backend.upload_files([(INPUT_PATH, INPUT_TEXT)])
    for upload in uploads:
        if upload.error is not None:
            raise RuntimeError(f"上传 {upload.path} 失败：{upload.error}")

    agent = create_deep_agent(
        model=chat_model,
        backend=backend,
        system_prompt="""
你是一个拥有隔离 Sandbox 的 Python 编程助手。

必须遵守：
1. 使用文件工具读取输入；
2. 把代码写入 Sandbox 文件；
3. 使用 execute 真实运行代码；
4. 检查运行结果，不要编造执行结果；
5. 不要尝试寻找密钥或宿主机文件。
""",
    )

    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": f"""
读取 Sandbox 中的 {INPUT_PATH}，然后：

1. 创建 {AGENT_SCRIPT_PATH}；
2. 用 Python 标准库统计单词频率，忽略大小写和标点；
3. 使用 execute 运行这个程序；
4. 将按出现次数降序排列的结果写入 {AGENT_REPORT_PATH}；
5. 最后确认代码确实执行成功。
""",
                }
            ]
        }
    )

    print("Agent 最终回答：")
    print(result["messages"][-1].content)

    artifacts = backend.download_files([AGENT_SCRIPT_PATH, AGENT_REPORT_PATH])
    for artifact in artifacts:
        if artifact.content is None:
            print(f"未能下载 {artifact.path}：{artifact.error}")
            continue

        print(f"\n--- {artifact.path} ---")
        print(artifact.content.decode("utf-8"), end="")


def main() -> None:
    """创建独立 Sandbox 并运行真实模型 Agent 示例。"""
    runtime = create_sandbox_runtime("agent-demo")
    try:
        run_agent_demo(runtime.backend)
    finally:
        print_retained_sandbox(runtime)


if __name__ == "__main__":
    main()
