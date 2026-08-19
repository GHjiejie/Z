"""Offline integration tests for checkpoint_project.

Run with: uv run python -m unittest checkpoint_project.test_checkpoint_project
"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from checkpoint_project.graph import (
    CheckpointChatApp,
    checkpoint_id,
    iter_interrupts,
)


class ScriptedChatModel(BaseChatModel):
    """Minimal deterministic model supporting bind_tools for graph tests."""

    responses: list[Any]
    seen_messages: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-checkpoint-test-model"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ScriptedChatModel:
        del tools, kwargs
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        self.seen_messages.append(messages)
        if not self.responses:
            raise AssertionError("脚本模型没有剩余响应")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return ChatResult(generations=[ChatGeneration(message=response)])


class CheckpointProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "checkpoints.sqlite"
        self.workspace = self.root / "workspace"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def app(self, responses: list[Any]) -> CheckpointChatApp:
        model = ScriptedChatModel(responses=responses)
        return CheckpointChatApp(
            model,
            db_path=self.db_path,
            workspace=self.workspace,
        )

    def test_multi_turn_memory_survives_reopen(self) -> None:
        first_model = ScriptedChatModel(
            responses=[
                AIMessage(content="记住了，你喜欢蓝色。"),
                AIMessage(content="你喜欢蓝色。"),
            ]
        )
        with CheckpointChatApp(
            first_model,
            db_path=self.db_path,
            workspace=self.workspace,
        ) as app:
            app.invoke("memory", {"messages": [HumanMessage(content="我喜欢蓝色")]})
            app.invoke(
                "memory", {"messages": [HumanMessage(content="我喜欢什么颜色？")]}
            )
            second_call = first_model.seen_messages[1]
            contents = [str(message.content) for message in second_call]
            self.assertIn("我喜欢蓝色", contents)
            self.assertIn("记住了，你喜欢蓝色。", contents)

        with self.app([]) as reopened:
            messages = reopened.state("memory").values["messages"]
            self.assertEqual(messages[-1].content, "你喜欢蓝色。")
            self.assertEqual(reopened.state("memory").values["turn_count"], 2)

    def test_write_and_delete_require_approval(self) -> None:
        write_call = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"path": "note.txt", "content": "checkpoint"},
                    "id": "write-1",
                    "type": "tool_call",
                }
            ],
        )
        delete_call = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "delete_file",
                    "args": {"path": "note.txt"},
                    "id": "delete-1",
                    "type": "tool_call",
                }
            ],
        )
        responses = [
            write_call,
            AIMessage(content="文件已写入。"),
            delete_call,
            AIMessage(content="删除被拒绝，文件仍然保留。"),
        ]
        with self.app(responses) as app:
            app.invoke("approval", {"messages": [HumanMessage(content="写 note.txt")]})
            pending = list(iter_interrupts(app.state("approval")))
            self.assertEqual(len(pending), 1)
            self.assertFalse((self.workspace / "note.txt").exists())

            app.resume("approval", True)
            self.assertEqual(
                (self.workspace / "note.txt").read_text(encoding="utf-8"),
                "checkpoint",
            )

            app.invoke(
                "approval", {"messages": [HumanMessage(content="删除 note.txt")]}
            )
            self.assertTrue(list(iter_interrupts(app.state("approval"))))
            app.resume("approval", False)
            self.assertTrue((self.workspace / "note.txt").exists())

    def test_fork_from_checkpoint_copies_only_that_memory(self) -> None:
        responses = [
            AIMessage(content="第一轮回答"),
            AIMessage(content="第二轮回答"),
            AIMessage(content="分支回答"),
        ]
        with self.app(responses) as app:
            app.invoke("source", {"messages": [HumanMessage(content="第一轮")]})
            first_checkpoint = checkpoint_id(app.state("source"))
            app.invoke("source", {"messages": [HumanMessage(content="第二轮")]})

            branch = app.fork("source", first_checkpoint, "branch")
            branch_contents = [
                str(message.content) for message in branch.values["messages"]
            ]
            self.assertIn("第一轮", branch_contents)
            self.assertNotIn("第二轮", branch_contents)
            self.assertEqual(branch.next, ())

            app.invoke("branch", {"messages": [HumanMessage(content="走另一条路")]})
            source_contents = [
                str(message.content)
                for message in app.state("source").values["messages"]
            ]
            branch_contents = [
                str(message.content)
                for message in app.state("branch").values["messages"]
            ]
            self.assertNotIn("走另一条路", source_contents)
            self.assertIn("走另一条路", branch_contents)

            initial_checkpoint = checkpoint_id(app.history("source")[-1])
            empty_branch = app.fork("source", initial_checkpoint, "empty-branch")
            self.assertEqual(empty_branch.values.get("messages", []), [])

    def test_fork_from_pending_approval_cancels_side_effect(self) -> None:
        write_call = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"path": "pending.txt", "content": "never written"},
                    "id": "pending-write",
                    "type": "tool_call",
                }
            ],
        )
        with self.app([write_call, AIMessage(content="分支可以继续")]) as app:
            app.invoke("pending-source", {"messages": [HumanMessage(content="写文件")]})
            pending_checkpoint = checkpoint_id(app.state("pending-source"))
            self.assertTrue(list(iter_interrupts(app.state("pending-source"))))

            branch = app.fork("pending-source", pending_checkpoint, "safe-branch")
            self.assertFalse(list(iter_interrupts(branch)))
            self.assertIn("已取消", str(branch.values["messages"][-1].content))
            self.assertFalse((self.workspace / "pending.txt").exists())

            app.invoke(
                "safe-branch",
                {"messages": [HumanMessage(content="不写了，继续聊天")]},
            )
            self.assertEqual(
                app.state("safe-branch").values["messages"][-1].content,
                "分支可以继续",
            )

    def test_failure_can_retry_from_last_successful_checkpoint(self) -> None:
        with self.app(
            [RuntimeError("temporary failure"), AIMessage(content="恢复成功")]
        ) as app:
            with self.assertRaisesRegex(RuntimeError, "temporary failure"):
                app.invoke("retry", {"messages": [HumanMessage(content="请回答")]})

            failed_state = app.state("retry")
            self.assertEqual(failed_state.next, ("assistant",))
            app.invoke("retry", None)
            self.assertEqual(
                app.state("retry").values["messages"][-1].content, "恢复成功"
            )


if __name__ == "__main__":
    unittest.main()
