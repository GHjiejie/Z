"""Offline tests for the frontend SSE event service."""

from __future__ import annotations

import json
import unittest

from fastapi.testclient import TestClient
from langchain_core.language_models.chat_model_stream import ChatModelStream
from langchain_core.messages import AIMessage

from checkpoint_project.test_checkpoint_project import ScriptedChatModel
from interleave.api import create_api, message_event_consumer, tool_event_consumer
from interleave.main import build_graph


class InterleaveApiTests(unittest.TestCase):
    def test_sse_stream_emits_tokens_state_and_terminal_event(self) -> None:
        graph = build_graph(
            ScriptedChatModel(responses=[AIMessage(content="实时输出成功")])
        )

        with (
            TestClient(create_api(graph=graph)) as client,
            client.stream(
                "POST",
                "/api/chat/stream",
                json={"message": "测试事件流"},
            ) as response,
        ):
            self.assertEqual(response.status_code, 200)
            self.assertTrue(
                response.headers["content-type"].startswith("text/event-stream")
            )
            events = _read_sse(response)

        self.assertEqual(events[0]["type"], "run.started")
        self.assertEqual(
            "".join(event["text"] for event in events if event["type"] == "text.delta"),
            "实时输出成功",
        )
        self.assertGreaterEqual(
            len([event for event in events if event["type"] == "state.updated"]),
            2,
        )
        self.assertEqual(events[-1]["type"], "run.completed")
        self.assertEqual(
            events[-1]["output"]["messages"][-1]["content"],
            "实时输出成功",
        )
        self.assertEqual(
            [event["sequence"] for event in events],
            list(range(1, len(events) + 1)),
        )

    def test_stream_errors_are_forwarded_as_terminal_sse_events(self) -> None:
        graph = build_graph(ScriptedChatModel(responses=[RuntimeError("模型不可用")]))

        with (
            TestClient(create_api(graph=graph)) as client,
            client.stream(
                "POST",
                "/api/chat/stream",
                json={"message": "触发错误"},
            ) as response,
        ):
            events = _read_sse(response)

        self.assertEqual(events[-1]["type"], "run.failed")
        self.assertEqual(events[-1]["error"]["name"], "RuntimeError")
        self.assertEqual(events[-1]["error"]["message"], "模型不可用")

    def test_message_consumer_preserves_reasoning_and_text_order(self) -> None:
        stream = ChatModelStream(node="llm", message_id="message-1")
        for event in (
            {"event": "message-start", "role": "ai", "id": "message-1"},
            {
                "event": "content-block-start",
                "index": 0,
                "content": {"type": "reasoning", "reasoning": ""},
            },
            {
                "event": "content-block-delta",
                "index": 0,
                "delta": {"type": "reasoning-delta", "reasoning": "先思考"},
            },
            {
                "event": "content-block-finish",
                "index": 0,
                "content": {"type": "reasoning", "reasoning": "先思考"},
            },
            {
                "event": "content-block-start",
                "index": 1,
                "content": {"type": "text", "text": ""},
            },
            {
                "event": "content-block-delta",
                "index": 1,
                "delta": {"type": "text-delta", "text": "再回答"},
            },
            {
                "event": "content-block-finish",
                "index": 1,
                "content": {"type": "text", "text": "再回答"},
            },
            {"event": "message-finish"},
        ):
            stream.dispatch(event)

        events = list(message_event_consumer(stream))
        ordered_types = [event["type"] for event in events]
        self.assertLess(
            ordered_types.index("reasoning.delta"),
            ordered_types.index("text.delta"),
        )

    def test_tool_consumer_normalizes_tool_phases(self) -> None:
        events = list(
            tool_event_consumer(
                {"event": "tool-started", "tool_name": "read", "input": "main.py"}
            )
        )
        self.assertEqual(events[0]["type"], "tool.started")


def _read_sse(response) -> list[dict[str, object]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.iter_lines()
        if line.startswith("data: ")
    ]


if __name__ == "__main__":
    unittest.main()
