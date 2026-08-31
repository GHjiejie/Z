"""Deep Agents 内置文件工具的权限规则示例。

在项目根目录运行：

    uv run python -m exec_env.permission

permissions 只约束内置文件工具，不能约束 LocalShellBackend 的 execute。
因此本示例使用 FilesystemBackend。
"""

from pathlib import Path

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from chat_models.chat import chat_model

DEMO_ROOT = Path(__file__).resolve().parent / "permission_data"
WORKSPACE = DEMO_ROOT / "workspace"
PRIVATE_DIR = DEMO_ROOT / "private"


def prepare_demo_files() -> None:
    """创建演示数据，但不覆盖已经存在的文件。"""
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)

    demo_files = {
        WORKSPACE / "readme.txt": "这是允许 Agent 读取的普通文件。\n",
        WORKSPACE / ".env": "DEMO_SECRET=not-a-real-secret\n",
        PRIVATE_DIR / "internal.txt": "这是 workspace 之外的文件。\n",
    }
    for file_path, content in demo_files.items():
        if not file_path.exists():
            file_path.write_text(content, encoding="utf-8")


backend = FilesystemBackend(root_dir=DEMO_ROOT, virtual_mode=True)

# 权限规则按照声明顺序匹配，第一条匹配规则决定结果。
# 没有规则匹配时默认允许，所以最后使用 /** 拒绝其余所有路径。
permissions = [
    # 具体规则必须放在宽泛规则前面：禁止访问模拟的敏感文件。
    FilesystemPermission(
        operations=["read", "write"],
        paths=["/workspace/.env"],
        mode="deny",
    ),
    # 写入 review 目录前暂停，由用户批准或拒绝。
    # 必须放在下面的 /workspace/** allow 规则之前。
    FilesystemPermission(
        operations=["write"],
        paths=["/workspace/review/**"],
        mode="interrupt",
    ),
    # workspace 中除 .env 外的文件允许读写。
    FilesystemPermission(
        operations=["read", "write"],
        paths=["/workspace/**"],
        mode="allow",
    ),
    # 拒绝 workspace 以外的所有文件访问。
    FilesystemPermission(
        operations=["read", "write"],
        paths=["/**"],
        mode="deny",
    ),
]

agent = create_deep_agent(
    model=chat_model,
    backend=backend,
    permissions=permissions,
    checkpointer=InMemorySaver(),
    system_prompt="""你正在演示 Deep Agents 的文件权限。
必须实际调用文件工具验证用户要求，不要猜测文件内容或权限结果。
文件工具使用虚拟绝对路径，例如 /workspace/readme.txt。
工具返回权限错误时，明确告诉用户该操作被权限规则拒绝。
""",
)

CONFIG = {"configurable": {"thread_id": "permission-demo"}}


def review_pending_actions(result):
    """处理 permission interrupt，并使用同一 thread_id 恢复 Agent。"""
    while result.interrupts:
        interrupt_value = result.interrupts[0].value
        action_requests = interrupt_value["action_requests"]
        review_configs = interrupt_value["review_configs"]
        config_by_action = {item["action_name"]: item for item in review_configs}

        decisions = []
        print("\n检测到需要人工审批的文件操作：")
        for action in action_requests:
            review_config = config_by_action[action["name"]]
            print(f"  工具：{action['name']}")
            print(f"  参数：{action['args']}")
            print(f"  支持的决定：{review_config['allowed_decisions']}")

            while True:
                choice = input("是否批准？approve(a) / reject(r)：").strip().lower()
                if choice in {"a", "approve", "y", "yes"}:
                    decisions.append({"type": "approve"})
                    break
                if choice in {"r", "reject", "n", "no"}:
                    decisions.append(
                        {
                            "type": "reject",
                            "message": (
                                "用户拒绝了这个文件操作。"
                                "不要重试相同操作，并向用户说明没有执行。"
                            ),
                        }
                    )
                    break
                print("请输入 a 或 r。")

        result = agent.invoke(
            Command(resume={"decisions": decisions}),
            config=CONFIG,
            version="v2",
        )

    return result


def main() -> None:
    prepare_demo_files()

    print(f"Demo 文件目录：{DEMO_ROOT}")
    print("可以依次尝试以下指令：")
    print("  读取 /workspace/readme.txt")
    print("  读取 /workspace/.env")
    print("  创建 /workspace/hello.txt，内容为 hello")
    print("  创建 /workspace/review/report.txt，内容为 pending review")
    print("  创建 /outside.txt，内容为 blocked")

    while True:
        question = input("\n请输入问题（输入 exit 退出）：")
        if question.strip().lower() == "exit":
            break
        if not question.strip():
            continue

        result = agent.invoke(
            {"messages": [HumanMessage(content=question)]},
            config=CONFIG,
            version="v2",
        )
        result = review_pending_actions(result)
        print(f"回复：{result.value['messages'][-1].content}")


if __name__ == "__main__":
    main()
