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
            progress_events = [event for event in events if event["type"] == "progress"]
            self.assertGreaterEqual(len(progress_events), 2)
            self.assertEqual(progress_events[0]["phase"], "accepted")
            self.assertEqual(progress_events[-1]["phase"], "finalizing")
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

    def test_stream_sends_heartbeat_while_model_is_silent(self) -> None:
        api = create_api(
            model=ScriptedChatModel(
                responses=[AIMessage(content="延迟后完成")],
                delay_seconds=1.2,
            ),
            db_path=self.root / "heartbeat.sqlite",
            workspace=self.root / "heartbeat-workspace",
        )
        with TestClient(api) as client:
            client.post("/api/sessions", json={"thread_id": "heartbeat-test"})
            with client.stream(
                "POST",
                "/api/sessions/heartbeat-test/messages/stream",
                json={"content": "执行一个较慢的请求"},
            ) as response:
                events = [
                    json.loads(line.removeprefix("data: "))
                    for line in response.iter_lines()
                    if line.startswith("data: ")
                ]

            heartbeats = [
                event
                for event in events
                if event["type"] == "progress" and event["heartbeat"]
            ]
            self.assertTrue(heartbeats)
            self.assertGreaterEqual(heartbeats[0]["elapsed_ms"], 900)
            self.assertEqual(events[-1]["type"], "state")
            self.assertEqual(
                events[-1]["state"]["messages"][-1]["content"],
                "延迟后完成",
            )

    def test_html_artifact_stream_query_and_session_access(self) -> None:
        html = (
            "<!doctype html><html><body>"
            "<button onclick=\"document.body.dataset.clicked='yes'\">运行</button>"
            "</body></html>"
        )
        render_call = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "render_html",
                    "args": {"title": "交互页面", "html": html},
                    "id": "api-render-html",
                    "type": "tool_call",
                }
            ],
        )
        api = create_api(
            model=ScriptedChatModel(
                responses=[render_call, AIMessage(content="页面已经生成。")]
            ),
            db_path=self.root / "artifact-api.sqlite",
            workspace=self.root / "artifact-workspace",
        )

        with TestClient(api) as client:
            client.post("/api/sessions", json={"thread_id": "artifact-api"})
            client.post("/api/sessions", json={"thread_id": "other-session"})
            with client.stream(
                "POST",
                "/api/sessions/artifact-api/messages/stream",
                json={"content": "生成一个可以运行的按钮页面"},
            ) as response:
                events = [
                    json.loads(line.removeprefix("data: "))
                    for line in response.iter_lines()
                    if line.startswith("data: ")
                ]

            self.assertEqual(events[0]["protocol_version"], 2)
            self.assertTrue(events[0]["run_id"].startswith("run_"))
            self.assertTrue(
                all(event.get("run_id") == events[0]["run_id"] for event in events)
            )
            self.assertFalse(
                [event for event in events if event["type"] == "artifact_ready"]
            )
            pending_state = events[-1]["state"]
            self.assertEqual(pending_state["status"], "waiting_approval")
            approval_payload = pending_state["pending_approvals"][0]["payload"]
            self.assertEqual(approval_payload["kind"], "html_preview_approval")
            self.assertEqual(approval_payload["tool"], "render_html")
            self.assertEqual(approval_payload["title"], "交互页面")
            self.assertIn(
                "preparing_preview",
                [event["phase"] for event in events if event["type"] == "progress"],
            )
            self.assertEqual(
                client.get("/api/sessions/artifact-api/artifacts").json(),
                [],
            )

            with client.stream(
                "POST",
                "/api/sessions/artifact-api/approval/stream",
                json={"approved": True},
            ) as response:
                approval_events = [
                    json.loads(line.removeprefix("data: "))
                    for line in response.iter_lines()
                    if line.startswith("data: ")
                ]

            artifact_events = [
                event for event in approval_events if event["type"] == "artifact_ready"
            ]
            self.assertEqual(len(artifact_events), 1)
            reference = artifact_events[0]["artifact"]
            artifact_id = reference["artifact_id"]
            self.assertEqual(reference["kind"], "html")
            self.assertEqual(
                reference["content_url"],
                f"/api/sessions/artifact-api/artifacts/{artifact_id}",
            )

            final_state = approval_events[-1]["state"]
            self.assertEqual(final_state["status"], "idle")
            tool_message = next(
                message
                for message in final_state["messages"]
                if message["type"] == "tool" and message["name"] == "render_html"
            )
            self.assertEqual(tool_message["artifact"]["artifact_id"], artifact_id)
            self.assertNotIn("content", tool_message["artifact"])

            reloaded_state = client.get("/api/sessions/artifact-api").json()
            reloaded_tool = next(
                message
                for message in reloaded_state["messages"]
                if message["type"] == "tool" and message["name"] == "render_html"
            )
            self.assertEqual(reloaded_tool["artifact"]["artifact_id"], artifact_id)

            artifact = client.get(reference["content_url"])
            self.assertEqual(artifact.status_code, 200)
            self.assertEqual(artifact.json()["content"], html)
            listed = client.get("/api/sessions/artifact-api/artifacts").json()
            self.assertEqual([item["artifact_id"] for item in listed], [artifact_id])

            denied = client.get(f"/api/sessions/other-session/artifacts/{artifact_id}")
            self.assertEqual(denied.status_code, 404)

    def test_reject_html_preview_keeps_markdown_answer(self) -> None:
        render_call = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "render_html",
                    "args": {
                        "title": "鼠标跟随动画",
                        "html": "<!doctype html><html><body>demo</body></html>",
                    },
                    "id": "api-reject-render",
                    "type": "tool_call",
                }
            ],
        )
        api = create_api(
            model=ScriptedChatModel(
                responses=[
                    render_call,
                    AIMessage(content="```html\n<div>仅查看代码</div>\n```"),
                ]
            ),
            db_path=self.root / "reject-artifact.sqlite",
            workspace=self.root / "reject-artifact-workspace",
        )

        with TestClient(api) as client:
            client.post("/api/sessions", json={"thread_id": "reject-preview"})
            pending = client.post(
                "/api/sessions/reject-preview/messages",
                json={"content": "介绍鼠标跟随动画，最好给出代码示例"},
            )
            self.assertEqual(pending.json()["status"], "waiting_approval")

            rejected = client.post(
                "/api/sessions/reject-preview/approval",
                json={"approved": False},
            )
            self.assertEqual(rejected.json()["status"], "idle")
            self.assertIn("```html", rejected.json()["messages"][-1]["content"])
            self.assertEqual(
                client.get("/api/sessions/reject-preview/artifacts").json(),
                [],
            )


if __name__ == "__main__":
    unittest.main()
