"""Model tools for creating durable, frontend-renderable artifacts."""

from __future__ import annotations

from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.config import get_stream_writer
from pydantic import Field

from checkpoint_project.artifact_store import ArtifactStore


def html_preview_approval(args: dict[str, object]) -> dict[str, object]:
    """Build the small, serializable HITL payload shown before rendering HTML."""
    raw_title = args.get("title")
    raw_html = args.get("html")
    title = raw_title.strip() if isinstance(raw_title, str) else ""
    html = raw_html if isinstance(raw_html, str) else ""
    return {
        "kind": "html_preview_approval",
        "tool": "render_html",
        "title": title[:120] or "未命名页面",
        "characters": len(html),
        "byte_size": len(html.encode("utf-8")),
        "preview": html[:320],
    }


def build_artifact_tools(store: ArtifactStore) -> list[BaseTool]:
    """Build tools bound to the application's artifact store."""

    @tool(response_format="content_and_artifact")
    def render_html(
        title: Annotated[str, Field(min_length=1, max_length=120)],
        html: Annotated[str, Field(min_length=1, max_length=262_144)],
        tool_call_id: Annotated[str, InjectedToolCallId],
        config: RunnableConfig,
        parent_artifact_id: str | None = None,
    ) -> tuple[str, dict[str, object]]:
        """为适合可视化演示的前端需求创建可运行 HTML 页面预览。

        根据用户意图使用：用户请求页面、组件、视觉效果、动画、交互或其代码示例，
        且完整可运行演示能让答案更直观时，即使用户没有说“预览”也应调用。
        仅解释语法、概念，或只需非可运行片段时不调用。html 应是完整且尽量
        自包含的 HTML、CSS 和 JavaScript。修改已有页面时，把原页面的 artifact ID
        传给 parent_artifact_id。工具执行前系统会自动请求用户确认，不要先在对话中
        询问是否预览。
        """
        thread_id = str(config.get("configurable", {}).get("thread_id", ""))
        if not thread_id:
            raise ValueError("render_html 缺少 thread_id")
        artifact = store.create_or_get_html(
            thread_id=thread_id,
            tool_call_id=tool_call_id,
            title=title,
            html=html,
            parent_artifact_id=parent_artifact_id,
        )
        reference = artifact.public_ref()
        # The final ToolMessage/checkpoint remains authoritative.  Custom streaming
        # only improves latency and may be unavailable during a direct tool test.
        try:
            get_stream_writer()({"type": "artifact_ready", "artifact": reference})
        except (KeyError, RuntimeError):
            pass
        return f"已创建可预览页面：{artifact.title}", reference

    return [render_html]
