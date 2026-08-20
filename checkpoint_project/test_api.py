"""Offline HTTP integration tests for the FastAPI adapter."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from checkpoint_project.api import create_api
from checkpoint_project.test_checkpoint_project import ScriptedChatModel


class CheckpointApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_chat_approval_history_and_fork_flow(self) -> None:
        write_call = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"path": "web.txt", "content": "from api"},
                    "id": "api-write",
                    "type": "tool_call",
                }
            ],
        )
        model = ScriptedChatModel(
            responses=[
                AIMessage(content="第一轮回答"),
                write_call,
                AIMessage(content="文件已经写入"),
            ]
        )
        api = create_api(
            model=model,
            db_path=self.root / "api.sqlite",
            workspace=self.root / "workspace",
        )

        with TestClient(api) as client:
            health = client.get("/api/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["status"], "ok")

            created = client.post(
                "/api/sessions",
                json={"thread_id": "web-test"},
            )
            self.assertEqual(created.status_code, 201)
            self.assertEqual(created.json()["messages"], [])

            first = client.post(
                "/api/sessions/web-test/messages",
                json={"content": "第一轮"},
            )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json()["messages"][-1]["content"], "第一轮回答")

            write = client.post(
                "/api/sessions/web-test/messages",
                json={"content": "写入 web.txt"},
            )
            self.assertEqual(write.status_code, 200)
            self.assertEqual(write.json()["status"], "waiting_approval")
            self.assertEqual(
                write.json()["pending_approvals"][0]["payload"]["tool"],
                "write_file",
            )
            self.assertFalse((self.root / "workspace" / "web.txt").exists())

            blocked = client.post(
                "/api/sessions/web-test/messages",
                json={"content": "审批前不能继续"},
            )
            self.assertEqual(blocked.status_code, 409)

            approval = client.post(
                "/api/sessions/web-test/approval",
                json={"approved": True},
            )
            self.assertEqual(approval.status_code, 200)
            self.assertEqual(approval.json()["status"], "idle")
            self.assertEqual(
                (self.root / "workspace" / "web.txt").read_text(encoding="utf-8"),
                "from api",
            )

            checkpoints = client.get("/api/sessions/web-test/checkpoints").json()
            first_turn_checkpoint = next(
                item
                for item in checkpoints
                if item["last_message"]
                and item["last_message"]["content"] == "第一轮回答"
            )
            forked = client.post(
                "/api/sessions/web-test/fork",
                json={
                    "checkpoint_id": first_turn_checkpoint["checkpoint_id"],
                    "new_thread_id": "web-branch",
                },
            )
            self.assertEqual(forked.status_code, 201)
            branch_contents = [
                message["content"] for message in forked.json()["messages"]
            ]
            self.assertIn("第一轮回答", branch_contents)
            self.assertNotIn("写入 web.txt", branch_contents)

            sessions = client.get("/api/sessions").json()
            self.assertEqual(
                {session["thread_id"] for session in sessions},
                {"web-test", "web-branch"},
            )

    def test_message_sse_stream_returns_tokens_and_final_state(self) -> None:
        api = create_api(
            model=ScriptedChatModel(responses=[AIMessage(content="逐字输出成功")]),
            db_path=self.root / "stream.sqlite",
            workspace=self.root / "stream-workspace",
        )
        with TestClient(api) as client:
            client.post("/api/sessions", json={"thread_id": "stream-test"})
            with client.stream(
                "POST",
                "/api/sessions/stream-test/messages/stream",
                json={"content": "测试流式输出"},
            ) as response:
                self.assertEqual(response.status_code, 200)
                self.assertTrue(
                    response.headers["content-type"].startswith("text/event-stream")
                )
                events = [
                    json.loads(line.removeprefix("data: "))
                    for line in response.iter_lines()
                    if line.startswith("data: ")
                ]

            self.assertEqual(events[0]["type"], "start")
            self.assertEqual(
                "".join(
                    event.get("content", "")
                    for event in events
                    if event["type"] == "token"
                ),
                "逐字输出成功",
            )
            self.assertEqual(events[-1]["type"], "state")
            self.assertEqual(
                events[-1]["state"]["messages"][-1]["content"],
                "逐字输出成功",
            )


if __name__ == "__main__":
    unittest.main()
