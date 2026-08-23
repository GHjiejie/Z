"""Model tools for creating durable, frontend-renderable artifacts."""

from __future__ import annotations

from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.config import get_stream_writer
from pydantic import Field

from checkpoint_project.artifact_store import ArtifactStore


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
        """创建可运行的 HTML 页面预览。

        只有用户明确要求生成、修改或预览网页时才使用。讲解 HTML 或提供代码
        示例时不要调用。html 应是完整且尽量自包含的 HTML、CSS 和 JavaScript。
        修改已有页面时，把原页面的 artifact ID 传给 parent_artifact_id。
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
            get_stream_writer()(
                {"type": "artifact_ready", "artifact": reference}
            )
        except (KeyError, RuntimeError):
            pass
        return f"已创建可预览页面：{artifact.title}", reference

    return [render_html]
