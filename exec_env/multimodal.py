"""Deep Agents 多模态输入与工具结果 Demo。

在项目根目录运行：

    uv run python -m exec_env.multimodal

运行前请确认 chat_models.chat 中配置的模型支持对应的图片、音频、
视频或文档 MIME 类型。Deep Agents 能传递多模态内容，但不能让一个
纯文本模型获得视觉或音频能力。
"""

from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.messages import HumanMessage
from langchain.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from chat_models.chat import chat_model

MEDIA_ROOT = Path(__file__).resolve().parent / "multimodal_data"

SUPPORTED_EXTENSIONS = {
    # Images
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".heic",
    ".heif",
    # Video
    ".mp4",
    ".mpeg",
    ".mov",
    ".avi",
    ".flv",
    ".mpg",
    ".webm",
    ".wmv",
    ".3gpp",
    # Audio
    ".wav",
    ".mp3",
    ".aiff",
    ".aac",
    ".ogg",
    ".flac",
    # Documents
    ".pdf",
    ".ppt",
    ".pptx",
}


@tool
def return_remote_image(image_url: str) -> list[dict[str, str]]:
    """将公开图片 URL 作为标准多模态工具结果返回给模型。"""
    return [
        {
            "type": "text",
            "text": "下面是自定义工具返回的远程图片：",
        },
        {"type": "image", "url": image_url},
    ]


backend = FilesystemBackend(root_dir=MEDIA_ROOT, virtual_mode=True)

agent = create_deep_agent(
    model=chat_model,
    tools=[return_remote_image],
    backend=backend,
    checkpointer=InMemorySaver(),
    system_prompt="""你是多模态内容分析助手。
用户要求分析 Backend 中的媒体文件时，必须先调用 read_file，不能猜测内容。
用户要求测试自定义工具结果时，必须调用 return_remote_image。
如果模型或服务端不支持对应模态，请如实说明，不要编造观察结果。
""",
)

CONFIG = {"configurable": {"thread_id": "multimodal-demo"}}


def ask_analysis_question(default: str) -> str:
    """读取分析问题；用户直接回车时使用默认问题。"""
    question = input(f"分析要求（直接回车使用“{default}”）：").strip()
    return question or default


def print_answer(message: HumanMessage) -> None:
    """调用 Agent 并输出最后一条回复。"""
    try:
        result = agent.invoke({"messages": [message]}, config=CONFIG)
    except Exception as exc:  # noqa: BLE001 - CLI demo needs provider error details
        print(f"调用失败：{type(exc).__name__}: {exc}")
        print("请确认当前模型、OpenAI 兼容服务和 MIME 类型支持该模态。")
        return

    print(f"\n回复：{result['messages'][-1].content}")


def analyze_image_url() -> None:
    """演示在 HumanMessage 中直接传递标准图片内容块。"""
    image_url = input("公开图片 URL：").strip()
    if not image_url:
        print("URL 不能为空。")
        return

    question = ask_analysis_question("请详细描述这张图片。")
    message = HumanMessage(
        content=[
            {"type": "text", "text": question},
            {"type": "image", "url": image_url},
        ]
    )
    print_answer(message)


def analyze_backend_file() -> None:
    """演示由内置 read_file 工具读取 Backend 中的媒体文件。"""
    raw_path = input("媒体虚拟路径，例如 /example.png：").strip()
    if not raw_path:
        print("文件路径不能为空。")
        return

    virtual_path = raw_path if raw_path.startswith("/") else f"/{raw_path}"
    real_path = (MEDIA_ROOT / virtual_path.lstrip("/")).resolve()
    try:
        real_path.relative_to(MEDIA_ROOT.resolve())
    except ValueError:
        print("文件路径不能超出 multimodal_data。")
        return

    if not real_path.is_file():
        print(f"文件不存在：{real_path}")
        print(f"请先把媒体文件放入：{MEDIA_ROOT}")
        return

    extension = real_path.suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        print(f"该扩展名不在官方多模态 read_file 列表中：{extension}")
        return

    question = ask_analysis_question("请分析这个文件并概括主要内容。")
    message = HumanMessage(
        content=(
            f"{question}\n请先调用 read_file 读取 {virtual_path}，再根据实际内容回答。"
        )
    )
    print_answer(message)


def analyze_custom_tool_output() -> None:
    """演示自定义工具返回 text + image 标准内容块。"""
    image_url = input("公开图片 URL：").strip()
    if not image_url:
        print("URL 不能为空。")
        return

    question = ask_analysis_question("请描述工具返回的图片。")
    message = HumanMessage(
        content=(
            "请调用 return_remote_image 工具，参数 image_url 为 "
            f"{image_url!r}。工具返回图片后，{question}"
        )
    )
    print_answer(message)


def main() -> None:
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)

    print(f"当前模型：{chat_model.model_name}")
    print(f"本地媒体目录：{MEDIA_ROOT}")
    print("1. 在用户消息中直接发送图片 URL")
    print("2. 使用内置 read_file 读取本地媒体")
    print("3. 使用自定义工具返回图片内容块")

    while True:
        choice = input("\n请选择 1/2/3（输入 exit 退出）：").strip().lower()
        if choice == "exit":
            break
        if choice == "1":
            analyze_image_url()
        elif choice == "2":
            analyze_backend_file()
        elif choice == "3":
            analyze_custom_tool_output()
        else:
            print("请输入 1、2、3 或 exit。")


if __name__ == "__main__":
    main()
