"""LangGraph conversation graph with SQLite checkpoints and HITL tools."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, NotRequired

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot, interrupt

from checkpoint_project.artifact_store import ArtifactStore
from checkpoint_project.artifact_tools import (
    build_artifact_tools,
    html_preview_approval,
)
from checkpoint_project.file_tools import WorkspaceFiles, approval_preview, tool_map
from checkpoint_project.session_store import SessionStore

APPROVAL_TOOLS = {"write_file", "delete_file", "render_html"}

SYSTEM_PROMPT = """你是一个具有持久记忆的终端文件助手和页面原型助手。
你只能通过已提供的工具访问工作区。读取和列出文件可以直接执行；写入、覆盖、
删除文件会由系统暂停并请求用户批准。不要声称未经工具确认的文件操作已经完成。
结合消息历史回答，以简洁中文与用户交流。每轮尽量只发起一个文件变更工具调用。
主动识别用户的真实意图，而不是等待用户说出工具名或“预览”：当用户请求前端页面、
组件、视觉效果、动画、交互，并希望获得示例、代码、实现或演示时，完整可运行页面通常
比静态代码更有帮助，应生成自包含 HTML/CSS/JavaScript 并调用 render_html。典型例子
包括“介绍鼠标跟随动画，最好给出代码示例”。仅在用户只问语法/概念、明确不要运行，
或答案不适合组成页面时，才只给 Markdown 代码。render_html 会由系统自动触发
Human-in-the-loop 确认，不要先用普通文字追问用户，也不要要求用户提到 render_html。
确认通过后简短说明页面已经生成，不要再次输出完整 HTML；用户拒绝实时预览后，继续
提供解释和 Markdown 代码，不要再次调用 render_html。HTML 应尽量自包含且不依赖
外部网络资源。
"""


class ChatState(MessagesState):
    """Conversation state persisted by LangGraph after every super-step."""

    turn_count: NotRequired[int]


class CheckpointChatApp(AbstractContextManager["CheckpointChatApp"]):
    """Own the checkpointer connection and the compiled conversation graph."""

    def __init__(
        self,
        model: BaseChatModel,
        *,
        db_path: Path,
        workspace: Path,
    ) -> None:
        self.db_path = db_path.expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.files = WorkspaceFiles(workspace)
        self.artifacts = ArtifactStore(self.db_path)
        self.tools = [
            *self.files.build_tools(),
            *build_artifact_tools(self.artifacts),
        ]
        self.tools_by_name = tool_map(self.tools)
        self.sessions = SessionStore(self.db_path)
        # from_conn_string is a context manager.  Keep it open for as long as
        # the compiled graph may access SQLite.
        self._saver_context = SqliteSaver.from_conn_string(str(self.db_path))
        self.checkpointer = self._saver_context.__enter__()
        self._closed = False
        try:
            self.graph = self._build_graph(model)
        except BaseException:
            self.close()
            raise

    def _build_graph(self, model: BaseChatModel) -> CompiledStateGraph:
        try:
            model_with_tools = model.bind_tools(self.tools, parallel_tool_calls=False)
        except TypeError:
            # Some test/demonstration chat models do not accept provider kwargs.
            model_with_tools = model.bind_tools(self.tools)

        def assistant(state: ChatState) -> dict[str, object]:
            response = model_with_tools.invoke(
                [SystemMessage(content=SYSTEM_PROMPT), *state["messages"]]
            )
            return {
                "messages": [response],
                "turn_count": state.get("turn_count", 0) + 1,
            }

        def route_after_assistant(state: ChatState) -> str:
            latest = state["messages"][-1]
            if isinstance(latest, AIMessage) and latest.tool_calls:
                return "tools"
            return END

        def execute_tools(
            state: ChatState,
            config: RunnableConfig,
        ) -> dict[str, list[ToolMessage]]:
            latest = state["messages"][-1]
            if not isinstance(latest, AIMessage):
                raise TypeError("tools 节点要求最后一条消息是 AIMessage")

            calls = latest.tool_calls
            approvals: dict[str, bool] = {}

            # Collect every approval before producing side effects.  On resume,
            # LangGraph restarts this node and replays prior interrupt answers;
            # therefore no successful file operation is accidentally repeated.
            for call in calls:
                name = call["name"]
                if name not in APPROVAL_TOOLS:
                    continue
                try:
                    if name == "render_html":
                        payload = html_preview_approval(call["args"])
                    else:
                        payload = approval_preview(
                            name,
                            call["args"],
                            self.files.resolve,
                        )
                    payload["tool_call_id"] = call["id"]
                    decision = interrupt(payload)
                    approvals[call["id"]] = _is_approved(decision)
                except ValueError:
                    approvals[call["id"]] = False

            results: list[ToolMessage] = []
            for call in calls:
                name = call["name"]
                if name in APPROVAL_TOOLS and not approvals.get(call["id"], False):
                    if name == "render_html":
                        content = (
                            "用户选择仅查看代码，未运行 HTML 实时预览，也未创建 "
                            "artifact。请继续提供说明和 Markdown HTML 代码。"
                        )
                    else:
                        content = f"用户拒绝执行 {name}，未产生文件变更。"
                    results.append(
                        ToolMessage(
                            content=content,
                            tool_call_id=call["id"],
                            name=name,
                            status="error",
                        )
                    )
                    continue

                selected = self.tools_by_name.get(name)
                if selected is None:
                    results.append(
                        ToolMessage(
                            content=f"未知工具: {name}",
                            tool_call_id=call["id"],
                            name=name,
                            status="error",
                        )
                    )
                    continue
                try:
                    output = selected.invoke(call, config=config)
                    if isinstance(output, ToolMessage):
                        results.append(output)
                    else:
                        results.append(
                            ToolMessage(
                                content=str(output),
                                tool_call_id=call["id"],
                                name=name,
                            )
                        )
                except Exception as exc:  # noqa: BLE001 - expose tool error to the model
                    results.append(
                        ToolMessage(
                            content=f"工具执行失败: {exc}",
                            tool_call_id=call["id"],
                            name=name,
                            status="error",
                        )
                    )
            return {"messages": results}

        def session_boundary(_state: ChatState) -> dict[str, object]:
            """Synthetic terminal node used only while cloning a checkpoint."""
            return {}

        builder = StateGraph(ChatState)
        builder.add_node("assistant", assistant)
        builder.add_node("tools", execute_tools)
        builder.add_node("session_boundary", session_boundary)
        builder.add_edge(START, "assistant")
        builder.add_conditional_edges("assistant", route_after_assistant)
        builder.add_edge("tools", "assistant")
        builder.add_edge("session_boundary", END)
        return builder.compile(checkpointer=self.checkpointer)

    @staticmethod
    def config(
        thread_id: str,
        checkpoint_id: str | None = None,
    ) -> RunnableConfig:
        configurable: dict[str, str] = {"thread_id": thread_id}
        if checkpoint_id:
            configurable["checkpoint_id"] = checkpoint_id
        return {"configurable": configurable}

    def invoke(self, thread_id: str, graph_input: Any) -> dict[str, Any]:
        self.sessions.ensure(thread_id)
        return self.graph.invoke(graph_input, config=self.config(thread_id))

    def stream(self, thread_id: str, graph_input: Any) -> Iterator[tuple[str, Any]]:
        """Stream LangGraph message events while retaining SQLite checkpoints."""
        self.sessions.ensure(thread_id)
        return self.graph.stream(
            graph_input,
            config=self.config(thread_id),
            stream_mode=["messages", "custom"],
        )

    def resume(self, thread_id: str, decision: bool) -> dict[str, Any]:
        return self.invoke(thread_id, Command(resume={"approved": decision}))

    def state(self, thread_id: str, checkpoint_id: str | None = None) -> StateSnapshot:
        return self.graph.get_state(self.config(thread_id, checkpoint_id))

    def history(self, thread_id: str) -> list[StateSnapshot]:
        return list(self.graph.get_state_history(self.config(thread_id)))

    def fork(
        self,
        source_thread_id: str,
        checkpoint_id: str,
        new_thread_id: str,
    ) -> StateSnapshot:
        if source_thread_id == new_thread_id:
            raise ValueError("新会话 ID 必须与源会话不同")
        source = self.state(source_thread_id, checkpoint_id)
        if source.metadata is None:
            raise ValueError(f"checkpoint 不存在: {checkpoint_id}")
        if self.state(new_thread_id).metadata is not None:
            raise ValueError(f"新会话 ID 已有 checkpoint: {new_thread_id}")
        self.sessions.ensure(
            new_thread_id,
            source_thread_id=source_thread_id,
            source_checkpoint_id=checkpoint_id,
        )
        branch_values = _branch_values(source.values)
        self.artifacts.grant_to_session(
            new_thread_id,
            _artifact_ids(branch_values.get("messages", [])),
        )
        # Mark the copied state as a completed synthetic node.  This deliberately
        # copies memory but not an old checkpoint's pending task/interrupt, so the
        # branch is immediately ready for a new user turn.
        result_config = self.graph.update_state(
            self.config(new_thread_id),
            values=branch_values,
            as_node="session_boundary",
        )
        return self.graph.get_state(result_config)

    def close(self) -> None:
        if not self._closed:
            self._saver_context.__exit__(None, None, None)
            self._closed = True

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _is_approved(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return bool(value.get("approved", False))
    return False


def _branch_values(values: dict[str, Any]) -> dict[str, Any]:
    """Copy state and safely close a tool call pending at the branch point."""
    copied = dict(values)
    messages = list(values.get("messages", []))
    if messages and isinstance(messages[-1], AIMessage) and messages[-1].tool_calls:
        for call in messages[-1].tool_calls:
            messages.append(
                ToolMessage(
                    content=(
                        "从此 checkpoint 创建新会话时已取消原待执行工具调用；"
                        "没有执行该操作。"
                    ),
                    tool_call_id=call["id"],
                    name=call["name"],
                    status="error",
                )
            )
    copied["messages"] = messages
    return copied


def _artifact_ids(messages: object) -> list[str]:
    if not isinstance(messages, list):
        return []
    result: list[str] = []
    for message in messages:
        if not isinstance(message, ToolMessage) or not isinstance(
            message.artifact, dict
        ):
            continue
        artifact_id = message.artifact.get("artifact_id")
        if isinstance(artifact_id, str):
            result.append(artifact_id)
    return result


def checkpoint_id(snapshot: StateSnapshot) -> str:
    return str(snapshot.config["configurable"]["checkpoint_id"])


def iter_interrupts(snapshot: StateSnapshot) -> Iterator[object]:
    for task in snapshot.tasks:
        yield from task.interrupts
