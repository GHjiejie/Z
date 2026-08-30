import os
from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain.messages import HumanMessage

from chat_models.chat import chat_model

workspace = Path(__file__).parent.parent / "deepagent" / "localShell_data"
workspace.mkdir(parents=True, exist_ok=True)


backend = LocalShellBackend(
    root_dir=workspace,
    virtual_mode=True,
    env={
        "PATH": os.environ.get(
            "PATH",
            "/usr/local/bin:/usr/bin:/bin",
        ),
    },
)

system_prompt = """你是本地开发助手。

使用文件工具时：
- 使用虚拟绝对路径，例如 /hello.py。

使用 execute 工具时：
- 当前工作目录已经是 workspace；
- 使用相对路径，例如 python hello.py；
- 不要把文件工具的 /hello.py 直接用于 Shell；
- 未经用户明确要求，不执行删除或覆盖操作。"""

agent = create_deep_agent(
    model=chat_model, backend=backend, system_prompt=system_prompt
)


def main() -> None:
    while True:
        question = input("请输入问题(输入 exit 退出): ")
        if question.lower() == "exit":
            break
        result = agent.invoke({"messages": [HumanMessage(content=question)]})

        print(result)


if __name__ == "__main__":
    main()
