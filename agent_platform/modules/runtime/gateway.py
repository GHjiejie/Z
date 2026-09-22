"""Stream LiteLLM Chat Completions with explicit metering evidence."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from langchain_core.messages import AIMessageChunk

from chat_models.factory import create_gateway_chat_model


@dataclass(frozen=True)
class ModelReply:
    content: str
    tool_calls: list[dict]
    input_tokens: int
    output_tokens: int
    raw_usage: dict


@dataclass(frozen=True)
class GatewayEvent:
    type: str
    data: Any


class GatewayError(Exception):
    def __init__(self, code: str, message: str, *, request_sent: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_sent = request_sent


class UsageUnavailable(GatewayError):
    def __init__(self, message: str = "模型网关未返回完整用量，费用等待核实。") -> None:
        super().__init__("usage_unavailable", message)


def _count(usage: dict, key: str) -> int:
    value = usage.get(key)
    if type(value) is not int or value < 0:
        raise UsageUnavailable()
    return value


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") in {"text", "output_text"}
        )
    return ""


class Gateway:
    """Only server configuration selects the upstream endpoint and credential."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        client_factory: Callable[..., Any] = create_gateway_chat_model,
        timeout: float = 90,
    ) -> None:
        self.base_url = (
            base_url if base_url is not None else os.getenv("PLATFORM_LITELLM_URL", "")
        ).strip()
        self._api_key = (
            api_key if api_key is not None else os.getenv("PLATFORM_LITELLM_KEY", "")
        ).strip()
        self._client_factory = client_factory
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        try:
            parsed = urlsplit(self.base_url)
            return bool(
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and self._api_key
            )
        except ValueError:
            return False

    async def stream(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int,
        temperature: float,
        tools: list[dict],
    ) -> AsyncIterator[GatewayEvent]:
        if not self.configured:
            raise GatewayError(
                "gateway_not_configured",
                "请在服务端配置 PLATFORM_LITELLM_URL 和 PLATFORM_LITELLM_KEY。",
                request_sent=False,
            )
        if not model or type(max_tokens) is not int or max_tokens < 1:
            raise GatewayError(
                "invalid_model_config", "模型或输出上限无效。", request_sent=False
            )
        client = self._client_factory(
            model=model,
            base_url=self.base_url,
            api_key=self._api_key,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=self.timeout,
            use_responses_api=False,
        )
        runnable = client.bind_tools(tools) if tools else client
        aggregate: AIMessageChunk | None = None
        usage: dict | None = None
        finish_reason: str | None = None
        try:
            async for chunk in runnable.astream(messages):
                if not isinstance(chunk, AIMessageChunk):
                    raise GatewayError("invalid_gateway_response", "模型返回格式无效。")
                raw_usage = chunk.response_metadata.get("platform_raw_usage")
                if isinstance(raw_usage, dict):
                    # Streaming usage is a cumulative snapshot, never a delta.
                    usage = dict(raw_usage)
                if reason := chunk.response_metadata.get("finish_reason"):
                    finish_reason = reason
                aggregate = chunk if aggregate is None else aggregate + chunk
                if text := _text(chunk.content):
                    yield GatewayEvent("text", {"text": text})
        except asyncio.CancelledError:
            raise
        except GatewayError:
            raise
        except Exception as exc:
            # Provider exceptions can include credentials, URLs or prompt text.
            # Only a stable safe message crosses the platform boundary.
            raise GatewayError(
                "gateway_failed", "模型网关调用失败或流中断，费用等待核实。"
            ) from exc
        if usage is None or aggregate is None or finish_reason is None:
            raise UsageUnavailable()
        input_tokens = _count(usage, "prompt_tokens")
        output_tokens = _count(usage, "completion_tokens")
        if (
            "total_tokens" in usage
            and _count(usage, "total_tokens") != input_tokens + output_tokens
        ):
            raise UsageUnavailable("模型网关用量不一致，费用等待核实。")
        tool_calls = []
        for call in aggregate.tool_call_chunks:
            arguments = call.get("args", "")
            try:
                arguments = json.loads(arguments)
            except (ValueError, TypeError):
                # LangChain's partial-JSON parser can accept truncated JSON.
                # Preserve invalid text for runtime rejection after settlement.
                pass
            tool_calls.append(
                {"id": call.get("id"), "name": call.get("name"), "args": arguments}
            )
        yield GatewayEvent(
            "result",
            ModelReply(
                content=_text(aggregate.content),
                tool_calls=tool_calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                raw_usage={
                    **usage,
                    "finish_reason": finish_reason,
                    "usage_source": "litellm_gateway",
                },
            ),
        )
