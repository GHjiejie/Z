from __future__ import annotations

import inspect
import json
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Literal, Optional, Protocol, Union, cast
from urllib.parse import urlparse

import httpx

from packages.secrets import SecretConfigurationError, read_secret
from packages.http_security import EndpointSecurityError, async_provider_client, validate_provider_url


ApiStyle = Literal["chat_completions", "responses", "anthropic_messages"]
ReasoningKind = Literal["reasoning", "summary", "thinking"]


@dataclass(frozen=True)
class ModelStreamEvent:
    """A provider-independent content or reasoning stream event."""

    kind: Literal["content", "reasoning"]
    delta: str
    source: str
    reasoning_kind: Optional[ReasoningKind] = None


StreamHandler = Callable[[ModelStreamEvent], Union[None, Awaitable[None]]]


class ModelGatewayError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int = 0
    estimated: bool = False


@dataclass(frozen=True)
class ModelResponse:
    output: str
    finish_reason: str
    model: str
    usage: ModelUsage
    reasoning: str = ""
    reasoning_kind: Optional[ReasoningKind] = None


class ModelGateway(Protocol):
    def identity(self) -> Dict[str, Any]: ...

    async def complete(
        self, messages: List[Dict[str, str]], on_event: Optional[StreamHandler] = None
    ) -> ModelResponse: ...


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    api_style: ApiStyle = "chat_completions"
    auth_style: Literal["auto", "bearer", "anthropic"] = "auto"
    timeout_seconds: float = 180.0
    max_completion_tokens: int = 4096
    temperature: float = 0.7
    reasoning_split: bool = True
    reasoning_effort: Optional[str] = None
    reasoning_summary: Optional[str] = "auto"
    anthropic_version: str = "2023-06-01"
    anthropic_thinking_mode: Literal["enabled", "adaptive", "disabled", "provider_default"] = "enabled"
    anthropic_thinking_budget_tokens: int = 2048
    anthropic_thinking_display: Optional[str] = None

    def __post_init__(self) -> None:
        try:
            validated = validate_provider_url(
                self.base_url, allowlist_variable="DEEPAGENT_MODEL_ALLOWED_ORIGINS"
            )
        except EndpointSecurityError as exc:
            raise ModelGatewayError(str(exc)) from exc
        object.__setattr__(self, "base_url", validated)

    @classmethod
    def from_environment(cls) -> "OpenAICompatibleConfig":
        base_url = os.getenv("OPENAI_BASE_URL", "").strip().rstrip("/")
        try:
            api_key = read_secret("OPENAI_API_KEY")
        except SecretConfigurationError as exc:
            raise ModelGatewayError(str(exc)) from exc
        model = os.getenv("MODEL", "").strip()
        missing = [
            name for name, value in (
                ("OPENAI_BASE_URL", base_url),
                ("OPENAI_API_KEY", api_key),
                ("MODEL", model),
            ) if not value
        ]
        if missing:
            raise ModelGatewayError(
                "Real model runtime is not configured; missing " + ", ".join(missing)
            )
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ModelGatewayError("OPENAI_BASE_URL must be an absolute HTTP(S) URL")

        api_style = cls._parse_api_style(os.getenv("MODEL_API_STYLE", "chat_completions"))
        auth_style = os.getenv("MODEL_AUTH_STYLE", "auto").strip().lower()
        if auth_style not in {"auto", "bearer", "anthropic"}:
            raise ModelGatewayError("MODEL_AUTH_STYLE must be auto, bearer, or anthropic")
        thinking_mode = os.getenv("MODEL_ANTHROPIC_THINKING_MODE", "enabled").strip().lower()
        if thinking_mode not in {"enabled", "adaptive", "disabled", "provider_default"}:
            raise ModelGatewayError(
                "MODEL_ANTHROPIC_THINKING_MODE must be enabled, adaptive, disabled, or provider_default"
            )
        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            api_style=api_style,
            auth_style=cast(Any, auth_style),
            timeout_seconds=float(os.getenv("MODEL_TIMEOUT_SECONDS", "180")),
            max_completion_tokens=int(os.getenv("MODEL_MAX_COMPLETION_TOKENS", "4096")),
            temperature=float(os.getenv("MODEL_TEMPERATURE", "0.7")),
            reasoning_split=os.getenv("MODEL_REASONING_SPLIT", "true").lower()
            not in {"0", "false", "no"},
            reasoning_effort=os.getenv("MODEL_REASONING_EFFORT", "").strip() or None,
            reasoning_summary=os.getenv("MODEL_REASONING_SUMMARY", "auto").strip() or None,
            anthropic_version=os.getenv("MODEL_ANTHROPIC_VERSION", "2023-06-01").strip(),
            anthropic_thinking_mode=cast(Any, thinking_mode),
            anthropic_thinking_budget_tokens=int(
                os.getenv("MODEL_ANTHROPIC_THINKING_BUDGET_TOKENS", "2048")
            ),
            anthropic_thinking_display=os.getenv(
                "MODEL_ANTHROPIC_THINKING_DISPLAY", ""
            ).strip() or None,
        )

    @staticmethod
    def _parse_api_style(value: str) -> ApiStyle:
        aliases: Dict[str, ApiStyle] = {
            "chat": "chat_completions",
            "chat_completion": "chat_completions",
            "chat_completions": "chat_completions",
            "response": "responses",
            "responses": "responses",
            "anthropic": "anthropic_messages",
            "anthropic_message": "anthropic_messages",
            "anthropic_messages": "anthropic_messages",
            "messages": "anthropic_messages",
        }
        normalized = value.strip().lower().replace("-", "_")
        if normalized not in aliases:
            raise ModelGatewayError(
                "MODEL_API_STYLE must be chat_completions, responses, or anthropic_messages"
            )
        return aliases[normalized]


class OpenAICompatibleModelGateway:
    """Streaming adapter for Chat Completions, Responses, and Anthropic Messages."""

    def __init__(
        self,
        config: OpenAICompatibleConfig,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.config = config
        self.transport = transport

    @classmethod
    def from_environment(cls) -> "OpenAICompatibleModelGateway":
        return cls(OpenAICompatibleConfig.from_environment())

    def identity(self) -> Dict[str, Any]:
        return {
            "provider": self.config.api_style,
            "model": self.config.model,
            "route": self.config.base_url,
            "streaming": True,
            "api_style": self.config.api_style,
            "reasoning_stream": True,
        }

    async def complete(
        self, messages: List[Dict[str, str]], on_event: Optional[StreamHandler] = None
    ) -> ModelResponse:
        from packages.operations.telemetry import operation
        with operation('model.call'):
            return await self._complete_traced(messages, on_event)

    async def _complete_traced(self, messages, on_event):
        if self.config.api_style == "responses":
            return await self._complete_responses(messages, on_event)
        if self.config.api_style == "anthropic_messages":
            return await self._complete_anthropic(messages, on_event)
        return await self._complete_chat(messages, on_event)

    async def _complete_chat(
        self, messages: List[Dict[str, str]], on_event: Optional[StreamHandler]
    ) -> ModelResponse:
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_completion_tokens": self.config.max_completion_tokens,
            "temperature": self.config.temperature,
        }
        if self.config.reasoning_split:
            payload["reasoning_split"] = True
        if self.config.reasoning_effort:
            payload["reasoning_effort"] = self.config.reasoning_effort

        output = reasoning = ""
        reasoning_kind: Optional[ReasoningKind] = None
        finish_reason, response_model = "stop", self.config.model
        usage: Optional[ModelUsage] = None
        async for chunk in self._event_stream(
            self._endpoint("chat/completions"), payload, self._headers()
        ):
            self._validate_provider_chunk(chunk)
            response_model = str(chunk.get("model") or response_model)
            chunk_usage = chunk.get("usage")
            if isinstance(chunk_usage, dict):
                details = chunk_usage.get("completion_tokens_details") or {}
                usage = ModelUsage(
                    int(chunk_usage.get("prompt_tokens") or 0),
                    int(chunk_usage.get("completion_tokens") or 0),
                    int(details.get("reasoning_tokens") or 0),
                )
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"])
            delta_object = choice.get("delta") or {}
            reasoning_text, source = self._chat_reasoning(delta_object)
            if reasoning_text:
                delta, reasoning = self._normalize_content(reasoning, reasoning_text)
                if delta:
                    reasoning_kind = "reasoning"
                    await self._emit(
                        on_event, ModelStreamEvent("reasoning", delta, source, "reasoning")
                    )
            content = delta_object.get("content")
            if isinstance(content, str) and content:
                delta, output = self._normalize_content(output, content)
                if delta:
                    await self._emit(
                        on_event, ModelStreamEvent("content", delta, "delta.content")
                    )
        return self._response(
            messages, output, reasoning, reasoning_kind, finish_reason, response_model, usage
        )

    async def _complete_responses(
        self, messages: List[Dict[str, str]], on_event: Optional[StreamHandler]
    ) -> ModelResponse:
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "input": messages,
            "stream": True,
            "max_output_tokens": self.config.max_completion_tokens,
        }
        reasoning_options: Dict[str, str] = {}
        if self.config.reasoning_effort:
            reasoning_options["effort"] = self.config.reasoning_effort
        if self.config.reasoning_summary:
            reasoning_options["summary"] = self.config.reasoning_summary
        if reasoning_options:
            payload["reasoning"] = reasoning_options

        output = reasoning = ""
        reasoning_kind: Optional[ReasoningKind] = None
        finish_reason, response_model = "stop", self.config.model
        usage: Optional[ModelUsage] = None
        async for event in self._event_stream(
            self._endpoint("responses"), payload, self._headers()
        ):
            self._validate_provider_chunk(event)
            event_type = str(event.get("type") or "")
            if event_type in {"response.output_text.delta", "response.text.delta"}:
                text = event.get("delta")
                if isinstance(text, str) and text:
                    delta, output = self._normalize_content(output, text)
                    if delta:
                        await self._emit(
                            on_event, ModelStreamEvent("content", delta, event_type)
                        )
            elif event_type in {
                "response.reasoning_summary_text.delta",
                "response.reasoning_text.delta",
            }:
                text = event.get("delta")
                if isinstance(text, str) and text:
                    kind: ReasoningKind = "summary" if "summary" in event_type else "reasoning"
                    delta, reasoning = self._normalize_content(reasoning, text)
                    if delta:
                        reasoning_kind = kind
                        await self._emit(
                            on_event, ModelStreamEvent("reasoning", delta, event_type, kind)
                        )

            response_object = event.get("response")
            if isinstance(response_object, dict):
                response_model = str(response_object.get("model") or response_model)
                if response_object.get("status"):
                    finish_reason = str(response_object["status"])
                usage = self._responses_usage(response_object.get("usage")) or usage
                if event_type in {"response.completed", "response.done"}:
                    final_output = self._responses_output(response_object)
                    if final_output:
                        delta, output = self._normalize_content(output, final_output)
                        if delta:
                            await self._emit(
                                on_event, ModelStreamEvent("content", delta, "response.output")
                            )
                    final_reasoning, final_kind = self._responses_reasoning(response_object)
                    if final_reasoning:
                        delta, reasoning = self._normalize_content(reasoning, final_reasoning)
                        if delta:
                            reasoning_kind = final_kind
                            await self._emit(
                                on_event,
                                ModelStreamEvent(
                                    "reasoning", delta, "response.output.reasoning", final_kind
                                ),
                            )
            if event_type in {"response.failed", "error"}:
                error = event.get("error") or (
                    response_object.get("error") if isinstance(response_object, dict) else None
                )
                message = error.get("message") if isinstance(error, dict) else "provider error"
                raise ModelGatewayError(
                    f"Model gateway rejected the request: {str(message)[:300]}"
                )
        return self._response(
            messages, output, reasoning, reasoning_kind, finish_reason, response_model, usage
        )

    async def _complete_anthropic(
        self, messages: List[Dict[str, str]], on_event: Optional[StreamHandler]
    ) -> ModelResponse:
        system, anthropic_messages = self._anthropic_messages(messages)
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": anthropic_messages,
            "stream": True,
            "max_tokens": self.config.max_completion_tokens,
        }
        if system:
            payload["system"] = system
        thinking = self._anthropic_thinking()
        if thinking:
            payload["thinking"] = thinking
        if not thinking or thinking.get("type") == "disabled":
            payload["temperature"] = self.config.temperature

        output = reasoning = ""
        reasoning_kind: Optional[ReasoningKind] = None
        finish_reason, response_model = "stop", self.config.model
        usage: Optional[ModelUsage] = None
        async for event in self._event_stream(
            self._endpoint("messages"), payload, self._headers(anthropic=True)
        ):
            self._validate_provider_chunk(event)
            event_type = str(event.get("type") or "")
            if event_type == "message_start":
                message = event.get("message") or {}
                response_model = str(message.get("model") or response_model)
                usage = self._anthropic_usage(message.get("usage"), usage)
            elif event_type == "content_block_start":
                block = event.get("content_block") or {}
                if block.get("type") == "text":
                    delta, output = self._normalize_content(output, str(block.get("text") or ""))
                    if delta:
                        await self._emit(
                            on_event, ModelStreamEvent("content", delta, "content_block.text")
                        )
                elif block.get("type") == "thinking":
                    delta, reasoning = self._normalize_content(
                        reasoning, str(block.get("thinking") or "")
                    )
                    if delta:
                        reasoning_kind = "thinking"
                        await self._emit(
                            on_event,
                            ModelStreamEvent(
                                "reasoning", delta, "content_block.thinking", "thinking"
                            ),
                        )
            elif event_type == "content_block_delta":
                delta_object = event.get("delta") or {}
                if delta_object.get("type") == "text_delta":
                    text = delta_object.get("text")
                    if isinstance(text, str) and text:
                        delta, output = self._normalize_content(output, text)
                        if delta:
                            await self._emit(
                                on_event, ModelStreamEvent("content", delta, "text_delta")
                            )
                elif delta_object.get("type") == "thinking_delta":
                    text = delta_object.get("thinking")
                    if isinstance(text, str) and text:
                        delta, reasoning = self._normalize_content(reasoning, text)
                        if delta:
                            reasoning_kind = "thinking"
                            await self._emit(
                                on_event,
                                ModelStreamEvent("reasoning", delta, "thinking_delta", "thinking"),
                            )
                # signature_delta and redacted_thinking are opaque by design.
            elif event_type == "message_delta":
                delta_object = event.get("delta") or {}
                if delta_object.get("stop_reason"):
                    finish_reason = str(delta_object["stop_reason"])
                usage = self._anthropic_usage(event.get("usage"), usage)
            elif event_type == "error":
                error = event.get("error") or {}
                message = error.get("message") if isinstance(error, dict) else "provider error"
                raise ModelGatewayError(
                    f"Model gateway rejected the request: {str(message)[:300]}"
                )
        return self._response(
            messages, output, reasoning, reasoning_kind, finish_reason, response_model, usage
        )

    async def _event_stream(
        self, endpoint: str, payload: Dict[str, Any], headers: Dict[str, str]
    ) -> AsyncIterator[Dict[str, Any]]:
        timeout = httpx.Timeout(
            self.config.timeout_seconds,
            connect=min(20.0, self.config.timeout_seconds),
        )
        try:
            async with async_provider_client(
                self.config.base_url, timeout=timeout, transport=self.transport
            ) as client:
                async with client.stream(
                    "POST", endpoint, headers=headers, json=payload
                ) as response:
                    if not response.is_success:
                        body = await response.aread()
                        raise ModelGatewayError(self._safe_error(response.status_code, body))
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            parsed = json.loads(data)
                        except json.JSONDecodeError as exc:
                            raise ModelGatewayError(
                                "Model gateway returned malformed stream data"
                            ) from exc
                        if isinstance(parsed, dict):
                            yield parsed
        except ModelGatewayError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise ModelGatewayError(
                f"Model gateway request failed ({exc.__class__.__name__})"
            ) from exc

    def _endpoint(self, suffix: str) -> str:
        base, suffix = self.config.base_url.rstrip("/"), suffix.strip("/")
        return base if base.endswith(f"/{suffix}") else f"{base}/{suffix}"

    def _headers(self, *, anthropic: bool = False) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        use_anthropic_auth = self.config.auth_style == "anthropic" or (
            self.config.auth_style == "auto"
            and anthropic
            and urlparse(self.config.base_url).hostname == "api.anthropic.com"
        )
        if use_anthropic_auth:
            headers["x-api-key"] = self.config.api_key
        else:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        if anthropic:
            headers["anthropic-version"] = self.config.anthropic_version
        return headers

    def _anthropic_thinking(self) -> Dict[str, Any]:
        mode = self.config.anthropic_thinking_mode
        if mode == "provider_default":
            return {}
        thinking: Dict[str, Any] = {"type": mode}
        if mode == "enabled":
            if self.config.max_completion_tokens <= 1024:
                raise ModelGatewayError(
                    "Anthropic extended thinking requires MODEL_MAX_COMPLETION_TOKENS greater than 1024"
                )
            thinking["budget_tokens"] = max(
                1024,
                min(
                    self.config.anthropic_thinking_budget_tokens,
                    self.config.max_completion_tokens - 1,
                ),
            )
        if self.config.anthropic_thinking_display:
            thinking["display"] = self.config.anthropic_thinking_display
        return thinking

    @staticmethod
    def _anthropic_messages(
        messages: List[Dict[str, str]],
    ) -> tuple[str, List[Dict[str, str]]]:
        system = "\n\n".join(
            item.get("content", "") for item in messages if item.get("role") == "system"
        )
        converted = [
            {"role": item.get("role", "user"), "content": item.get("content", "")}
            for item in messages if item.get("role") in {"user", "assistant"}
        ]
        return system, converted

    @staticmethod
    def _chat_reasoning(delta: Dict[str, Any]) -> tuple[str, str]:
        details = delta.get("reasoning_details")
        if isinstance(details, list):
            text = "".join(
                str(item.get("text") or item.get("content") or "")
                for item in details if isinstance(item, dict)
            )
            if text:
                return text, "delta.reasoning_details"
        for key in ("reasoning_content", "reasoning"):
            value = delta.get(key)
            if isinstance(value, str) and value:
                return value, f"delta.{key}"
        return "", ""

    @staticmethod
    def _responses_usage(value: Any) -> Optional[ModelUsage]:
        if not isinstance(value, dict):
            return None
        details = value.get("output_tokens_details") or {}
        return ModelUsage(
            int(value.get("input_tokens") or 0),
            int(value.get("output_tokens") or 0),
            int(details.get("reasoning_tokens") or 0),
        )

    @staticmethod
    def _responses_output(response: Dict[str, Any]) -> str:
        parts: List[str] = []
        for item in response.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                    parts.append(str(content.get("text") or ""))
        return "".join(parts)

    @staticmethod
    def _responses_reasoning(response: Dict[str, Any]) -> tuple[str, ReasoningKind]:
        reasoning_parts: List[str] = []
        summary_parts: List[str] = []
        for item in response.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "reasoning":
                continue
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("type") in {"reasoning_text", "text"}:
                    reasoning_parts.append(str(content.get("text") or ""))
            for summary in item.get("summary") or []:
                if isinstance(summary, dict):
                    summary_parts.append(str(summary.get("text") or ""))
        if reasoning_parts:
            return "".join(reasoning_parts), "reasoning"
        return "".join(summary_parts), "summary"

    @staticmethod
    def _anthropic_usage(value: Any, previous: Optional[ModelUsage]) -> Optional[ModelUsage]:
        if not isinstance(value, dict):
            return previous
        details = value.get("output_tokens_details") or {}
        return ModelUsage(
            int(value.get("input_tokens") if value.get("input_tokens") is not None else (previous.input_tokens if previous else 0)),
            int(value.get("output_tokens") if value.get("output_tokens") is not None else (previous.output_tokens if previous else 0)),
            int(details.get("thinking_tokens") or details.get("reasoning_tokens") or (previous.reasoning_tokens if previous else 0)),
        )

    @staticmethod
    async def _emit(handler: Optional[StreamHandler], event: ModelStreamEvent) -> None:
        if not handler:
            return
        result = handler(event)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _response(
        messages: List[Dict[str, str]], output: str, reasoning: str,
        reasoning_kind: Optional[ReasoningKind], finish_reason: str,
        model: str, usage: Optional[ModelUsage],
    ) -> ModelResponse:
        output, reasoning = output.strip(), reasoning.strip()
        if not output:
            raise ModelGatewayError("Model gateway completed without response content")
        if usage is None:
            usage = ModelUsage(
                max(1, sum(len(item.get("content", "")) for item in messages) // 3),
                max(1, (len(output) + len(reasoning)) // 3),
                estimated=True,
            )
        return ModelResponse(
            output, finish_reason, model, usage, reasoning, reasoning_kind
        )

    @staticmethod
    def _normalize_content(previous: str, content: str) -> tuple[str, str]:
        if previous and content.startswith(previous):
            return content[len(previous):], content
        return content, previous + content

    @staticmethod
    def _validate_provider_chunk(chunk: Dict[str, Any]) -> None:
        base_response = chunk.get("base_resp")
        if isinstance(base_response, dict) and int(base_response.get("status_code") or 0) != 0:
            message = str(base_response.get("status_msg") or "provider error")[:300]
            raise ModelGatewayError(f"Model gateway rejected the request: {message}")
        error = chunk.get("error")
        if isinstance(error, dict) and chunk.get("type") != "response.failed":
            message = str(error.get("message") or "provider error")[:300]
            raise ModelGatewayError(f"Model gateway rejected the request: {message}")

    @staticmethod
    def _safe_error(status_code: int, body: bytes) -> str:
        message = "request failed"
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
            if isinstance(parsed, dict):
                error, base_response = parsed.get("error"), parsed.get("base_resp")
                if isinstance(error, dict):
                    message = str(error.get("message") or message)
                elif isinstance(base_response, dict):
                    message = str(base_response.get("status_msg") or message)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        return f"Model gateway returned HTTP {status_code}: {message[:300]}"


class DeterministicModelGateway:
    """Explicit test double. Product startup never selects this gateway implicitly."""

    def identity(self) -> Dict[str, Any]:
        return {
            "provider": "test_double",
            "model": "deterministic-test-model",
            "route": "in-process",
            "streaming": True,
            "api_style": "test_double",
            "reasoning_stream": True,
        }

    async def complete(
        self, messages: List[Dict[str, str]], on_event: Optional[StreamHandler] = None
    ) -> ModelResponse:
        reasoning = (
            "I will inspect the request, verify the execution context, and prepare "
            "a concise, auditable response."
        )
        output = (
            "Analysis complete. I verified the execution plan, gathered the relevant "
            "context, and produced an auditable release recommendation."
        )
        for event in (
            ModelStreamEvent("reasoning", reasoning[:48], "test.reasoning", "reasoning"),
            ModelStreamEvent("reasoning", reasoning[48:], "test.reasoning", "reasoning"),
            ModelStreamEvent("content", output[:48], "test.content"),
            ModelStreamEvent("content", output[48:], "test.content"),
        ):
            await OpenAICompatibleModelGateway._emit(on_event, event)
        return ModelResponse(
            output=output,
            finish_reason="stop",
            model="deterministic-test-model",
            usage=ModelUsage(
                max(1, sum(len(item.get("content", "")) for item in messages) // 3),
                max(1, len(output) // 3),
                max(1, len(reasoning) // 3),
            ),
            reasoning=reasoning,
            reasoning_kind="reasoning",
        )
