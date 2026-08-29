from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from packages.config import load_environment
from packages.runtime.model_gateway import (
    ModelGatewayError,
    OpenAICompatibleConfig,
    OpenAICompatibleModelGateway,
)


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        ({"reasoning_content": "Think"}, ("Think", "delta.reasoning_content")),
        ({"reasoning": "Think"}, ("Think", "delta.reasoning")),
        (
            {"reasoning_details": [{"type": "reasoning.text", "text": "Think"}]},
            ("Think", "delta.reasoning_details"),
        ),
    ],
)
def test_chat_completion_reasoning_field_variants(delta, expected):
    assert OpenAICompatibleModelGateway._chat_reasoning(delta) == expected


def test_environment_loader_prefers_process_then_project_then_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    project = workspace / "project"
    project.mkdir(parents=True)
    (workspace / ".env").write_text(
        "OPENAI_BASE_URL=https://workspace.test/v1\n"
        "OPENAI_API_KEY=workspace-key\n"
        "MODEL=workspace-model\n",
        encoding="utf-8",
    )
    (project / ".env").write_text(
        "MODEL=project-model\nOPENAI_API_KEY=project-key\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "process-key")

    loaded = load_environment(project)

    assert loaded == [(workspace / ".env").resolve(), (project / ".env").resolve()]
    assert os.environ["OPENAI_BASE_URL"] == "https://workspace.test/v1"
    assert os.environ["MODEL"] == "project-model"
    assert os.environ["OPENAI_API_KEY"] == "process-key"


@pytest.mark.asyncio
async def test_openai_compatible_gateway_streams_and_normalizes_cumulative_chunks():
    chunks = [
        {
            "model": "MiniMax-M3",
            "choices": [{"delta": {
                "reasoning_details": [{"type": "reasoning.text", "text": "Check facts"}],
                "content": "Hello",
            }, "finish_reason": None}],
        },
        {
            "model": "MiniMax-M3",
            "choices": [{"delta": {
                "reasoning_details": [{"type": "reasoning.text", "text": "Check facts first"}],
                "content": "Hello world",
            }, "finish_reason": "stop"}],
        },
        {
            "model": "MiniMax-M3",
            "choices": [],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 7,
                "completion_tokens_details": {"reasoning_tokens": 5},
            },
        },
    ]
    stream = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer secret"
        body = json.loads(request.content)
        assert body["model"] == "MiniMax-M3"
        assert body["reasoning_split"] is True
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    gateway = OpenAICompatibleModelGateway(
        OpenAICompatibleConfig(
            base_url="https://example.test/v1",
            api_key="secret",
            model="MiniMax-M3",
        ),
        transport=httpx.MockTransport(handler),
    )
    events = []
    response = await gateway.complete(
        [{"role": "user", "content": "Say hello"}], events.append
    )

    assert [(event.kind, event.delta) for event in events] == [
        ("reasoning", "Check facts"),
        ("content", "Hello"),
        ("reasoning", " first"),
        ("content", " world"),
    ]
    assert response.output == "Hello world"
    assert response.reasoning == "Check facts first"
    assert response.reasoning_kind == "reasoning"
    assert response.model == "MiniMax-M3"
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 7
    assert response.usage.reasoning_tokens == 5


@pytest.mark.asyncio
async def test_responses_gateway_streams_reasoning_summary_and_output():
    events = [
        {"type": "response.reasoning_summary_text.delta", "delta": "Inspect"},
        {"type": "response.reasoning_summary_text.delta", "delta": " inputs"},
        {"type": "response.output_text.delta", "delta": "Result"},
        {"type": "response.output_text.delta", "delta": " ready"},
        {
            "type": "response.completed",
            "response": {
                "model": "gpt-reasoning",
                "status": "completed",
                "output": [
                    {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Inspect inputs"}]},
                    {"type": "message", "content": [{"type": "output_text", "text": "Result ready"}]},
                ],
                "usage": {
                    "input_tokens": 9,
                    "output_tokens": 11,
                    "output_tokens_details": {"reasoning_tokens": 6},
                },
            },
        },
    ]
    stream = "".join(f"data: {json.dumps(event)}\n\n" for event in events)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        body = json.loads(request.content)
        assert body["input"] == [{"role": "user", "content": "Analyze"}]
        assert body["reasoning"] == {"summary": "auto"}
        assert body["max_output_tokens"] == 4096
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    gateway = OpenAICompatibleModelGateway(
        OpenAICompatibleConfig(
            base_url="https://example.test/v1",
            api_key="secret",
            model="gpt-reasoning",
            api_style="responses",
        ),
        transport=httpx.MockTransport(handler),
    )
    streamed = []
    response = await gateway.complete(
        [{"role": "user", "content": "Analyze"}], streamed.append
    )

    assert [(event.kind, event.delta) for event in streamed] == [
        ("reasoning", "Inspect"),
        ("reasoning", " inputs"),
        ("content", "Result"),
        ("content", " ready"),
    ]
    assert response.reasoning == "Inspect inputs"
    assert response.reasoning_kind == "summary"
    assert response.output == "Result ready"
    assert response.finish_reason == "completed"
    assert response.usage.reasoning_tokens == 6


@pytest.mark.asyncio
async def test_anthropic_messages_gateway_streams_thinking_and_text():
    events = [
        {
            "type": "message_start",
            "message": {"model": "claude-test", "usage": {"input_tokens": 10, "output_tokens": 0}},
        },
        {"type": "content_block_start", "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "Compare"}},
        {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": " options"}},
        {"type": "content_block_delta", "delta": {"type": "signature_delta", "signature": "opaque"}},
        {"type": "content_block_start", "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Use A"}},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 8},
        },
        {"type": "message_stop"},
    ]
    stream = "".join(f"data: {json.dumps(event)}\n\n" for event in events)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "secret"
        assert request.headers["anthropic-version"] == "2023-06-01"
        body = json.loads(request.content)
        assert body["system"] == "Be accurate"
        assert body["messages"] == [{"role": "user", "content": "Choose"}]
        assert body["thinking"] == {"type": "enabled", "budget_tokens": 2048}
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    gateway = OpenAICompatibleModelGateway(
        OpenAICompatibleConfig(
            base_url="https://api.anthropic.com/v1",
            api_key="secret",
            model="claude-test",
            api_style="anthropic_messages",
        ),
        transport=httpx.MockTransport(handler),
    )
    streamed = []
    response = await gateway.complete(
        [
            {"role": "system", "content": "Be accurate"},
            {"role": "user", "content": "Choose"},
        ],
        streamed.append,
    )

    assert [(event.kind, event.delta) for event in streamed] == [
        ("reasoning", "Compare"),
        ("reasoning", " options"),
        ("content", "Use A"),
    ]
    assert response.reasoning == "Compare options"
    assert response.reasoning_kind == "thinking"
    assert response.output == "Use A"
    assert response.finish_reason == "end_turn"
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 8


@pytest.mark.asyncio
async def test_openai_compatible_gateway_returns_safe_provider_errors():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"message": "invalid credential"}},
        )

    gateway = OpenAICompatibleModelGateway(
        OpenAICompatibleConfig(
            base_url="https://example.test/v1",
            api_key="must-not-leak",
            model="MiniMax-M3",
        ),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ModelGatewayError) as error:
        await gateway.complete([{"role": "user", "content": "Hello"}])

    assert "HTTP 401" in str(error.value)
    assert "invalid credential" in str(error.value)
    assert "must-not-leak" not in str(error.value)
