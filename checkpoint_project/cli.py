"""Interactive terminal for the SQLite checkpointing demo."""

from __future__ import annotations

import argparse
import shlex
import sys
import uuid
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from checkpoint_project.graph import (
    CheckpointChatApp,
    checkpoint_id,
    iter_interrupts,
)
from checkpoint_project.model import build_model

HELP = """
普通文本                     发送一轮消息（同一 session 自动保留 memory）
/history                     显示当前会话的 checkpoint 历史
/fork <序号|checkpoint_id> [新会话ID]  从任意 checkpoint 派生新会话并切换
/new [会话ID]                创建并切换到空白会话
/switch <会话ID>             切换到已有/指定会话
/sessions                    显示本地会话列表
/state                       显示当前状态摘要
/graph                       显示 LangGraph 图结构
/retry                       从失败节点/最后 checkpoint 恢复执行
/help                        显示帮助
/quit                        退出
""".strip()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="LangGraph SQLite checkpoint 综合终端",
    )
    project_dir = Path(__file__).resolve().parent
    result.add_argument(
        "--db",
        type=Path,
        default=project_dir / "data" / "checkpoints.sqlite",
        help="SQLite 数据库路径",
    )
    result.add_argument(
        "--workspace",
        type=Path,
        default=project_dir / "workspace",
        help="允许文件工具访问的工作区",
    )
    result.add_argument(
        "--thread",
        default="main",
        help="启动时使用的会话 ID",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        model = build_model()
    except RuntimeError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    with CheckpointChatApp(
        model,
        db_path=args.db,
        workspace=args.workspace,
    ) as app:
        terminal = Terminal(app, args.thread)
        terminal.run()
    return 0


class Terminal:
    def __init__(self, app: CheckpointChatApp, thread_id: str) -> None:
        self.app = app
        self.thread_id = thread_id
        self.app.sessions.ensure(thread_id)

    def run(self) -> None:
        print("LangGraph checkpoint 终端已启动。输入 /help 查看命令。")
        print(f"session={self.thread_id}  db={self.app.db_path}")
        while True:
            try:
                if self._handle_pending_approval():
                    continue
                line = input(f"\n[{self.thread_id}] 你> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n已退出。")
                return

            if not line:
                continue
            if line.startswith("/"):
                if not self._command(line):
                    return
                continue

            try:
                self.app.invoke(
                    self.thread_id,
                    {"messages": [HumanMessage(content=line)]},
                )
                if not self._has_interrupt():
                    self._print_latest_ai()
            except Exception as exc:  # noqa: BLE001 - terminal must survive graph/provider errors
                print(f"执行失败: {exc}")
                print("状态已由 SQLite checkpoint 保留；排除原因后输入 /retry。")

    def _command(self, raw: str) -> bool:
        try:
            parts = shlex.split(raw)
        except ValueError as exc:
            print(f"命令格式错误: {exc}")
            return True
        command, *arguments = parts

        if command in {"/quit", "/exit"}:
            print("已退出。")
            return False
        if command == "/help":
            print(HELP)
        elif command == "/history":
            self._print_history()
        elif command == "/sessions":
            self._print_sessions()
        elif command == "/state":
            self._print_state()
        elif command == "/graph":
            print(self.app.graph.get_graph().draw_ascii())
        elif command == "/new":
            new_id = arguments[0] if arguments else _new_thread_id()
            existing = bool(self.app.state(new_id).values)
            self.app.sessions.ensure(new_id)
            self.thread_id = new_id
            kind = "已有" if existing else "空白"
            print(f"已切换到{kind}会话: {new_id}")
        elif command == "/switch":
            if not arguments:
                print("用法: /switch <会话ID>")
            else:
                self.app.sessions.ensure(arguments[0])
                self.thread_id = arguments[0]
                print(f"已切换到会话: {self.thread_id}")
        elif command == "/fork":
            self._fork(arguments)
        elif command == "/retry":
            try:
                self.app.invoke(self.thread_id, None)
                if not self._has_interrupt():
                    self._print_latest_ai()
            except Exception as exc:  # noqa: BLE001 - retry reports provider/tool errors
                print(f"恢复执行仍然失败: {exc}")
        else:
            print(f"未知命令: {command}；输入 /help 查看帮助。")
        return True

    def _has_interrupt(self) -> bool:
        return bool(list(iter_interrupts(self.app.state(self.thread_id))))

    def _handle_pending_approval(self) -> bool:
        interrupts = list(iter_interrupts(self.app.state(self.thread_id)))
        if not interrupts:
            return False
        current = interrupts[0]
        payload = getattr(current, "value", current)
        self._print_approval(payload)
        try:
            answer = input("批准此操作？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n审批保持 pending；下次启动仍可继续。")
            raise
        approved = answer in {"y", "yes", "是", "批准"}
        try:
            self.app.resume(self.thread_id, approved)
            if not self._has_interrupt():
                self._print_latest_ai()
        except Exception as exc:  # noqa: BLE001 - preserve pending approval on any failure
            print(f"恢复审批失败: {exc}")
        return True

    @staticmethod
    def _print_approval(payload: object) -> None:
        print("\n--- 需要人工确认 ---")
        if not isinstance(payload, dict):
            print(payload)
            return
        print(f"操作: {payload.get('tool')}")
        print(f"路径: {payload.get('path')}")
        if payload.get("tool") == "write_file":
            print(
                f"字符数: {payload.get('characters')}  覆盖: {payload.get('overwrite')}"
            )
            preview = str(payload.get("preview", ""))
            print(f"内容预览:\n{preview}")

    def _print_history(self) -> None:
        history = self.app.history(self.thread_id)
        if not history:
            print("当前会话还没有 checkpoint。")
            return
        print(
            "序号  checkpoint_id                         next              最后一条消息"
        )
        for index, snapshot in enumerate(history):
            cp_id = checkpoint_id(snapshot)
            next_nodes = ",".join(snapshot.next) or "END"
            summary = _last_message_summary(snapshot.values.get("messages", []))
            print(f"{index:<5} {cp_id:<37} {next_nodes:<17} {summary}")

    def _fork(self, arguments: list[str]) -> None:
        if not arguments:
            print("用法: /fork <历史序号|checkpoint_id> [新会话ID]")
            return
        source = arguments[0]
        if source.isdigit():
            history = self.app.history(self.thread_id)
            index = int(source)
            if index >= len(history):
                print(f"历史序号越界: {source}")
                return
            source = checkpoint_id(history[index])
        new_id = arguments[1] if len(arguments) > 1 else _new_thread_id("branch")
        try:
            self.app.fork(self.thread_id, source, new_id)
        except Exception as exc:  # noqa: BLE001 - user-facing command boundary
            print(f"创建分支失败: {exc}")
            return
        old_id = self.thread_id
        self.thread_id = new_id
        print(f"已从 {old_id}@{source} 派生并切换到: {new_id}")

    def _print_sessions(self) -> None:
        for session in self.app.sessions.list():
            marker = "*" if session.thread_id == self.thread_id else " "
            source = ""
            if session.source_thread_id:
                source = (
                    f" <- {session.source_thread_id}@{session.source_checkpoint_id}"
                )
            print(f"{marker} {session.thread_id}  {session.created_at}{source}")

    def _print_state(self) -> None:
        snapshot = self.app.state(self.thread_id)
        if not snapshot.values:
            print("当前为空白会话。")
            return
        print(f"checkpoint: {checkpoint_id(snapshot)}")
        print(f"next: {', '.join(snapshot.next) or 'END'}")
        print(f"turn_count: {snapshot.values.get('turn_count', 0)}")
        print(f"messages: {len(snapshot.values.get('messages', []))}")
        print(f"pending_interrupts: {len(list(iter_interrupts(snapshot)))}")

    def _print_latest_ai(self) -> None:
        messages = self.app.state(self.thread_id).values.get("messages", [])
        for message in reversed(messages):
            if isinstance(message, AIMessage) and message.content:
                print(f"助手> {_message_content(message)}")
                return


def _message_content(message: BaseMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return str(message.content)


def _last_message_summary(messages: list[BaseMessage]) -> str:
    if not messages:
        return "-"
    message = messages[-1]
    content = _message_content(message).replace("\n", " ")
    return f"{message.type}: {content[:48]}"


def _new_thread_id(prefix: str = "session") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


if __name__ == "__main__":
    raise SystemExit(main())
