from __future__ import annotations

from functools import cached_property

import anthropic
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import SecretStr

from packages.http_security import async_provider_client, provider_client, provider_event_hooks
from packages.runtime.model_gateway import (
    ModelGateway,
    ModelGatewayError,
    OpenAICompatibleModelGateway,
)


class OriginBoundChatAnthropic(ChatAnthropic):
    """Keep native tool-calling requests inside the configured provider origin."""

    @cached_property
    def _client(self):
        params = self._client_params
        return anthropic.Client(
            **params, http_client=anthropic.DefaultHttpxClient(
                timeout=params.get("timeout", 180), trust_env=False, follow_redirects=False,
                event_hooks=provider_event_hooks(params["base_url"]),
            )
        )

    @cached_property
    def _async_client(self):
        params = self._client_params
        return anthropic.AsyncClient(
            **params, http_client=anthropic.DefaultAsyncHttpxClient(
                timeout=params.get("timeout", 180), trust_env=False, follow_redirects=False,
                event_hooks=provider_event_hooks(params["base_url"], asynchronous=True),
            )
        )


async def close_coding_chat_model(model: BaseChatModel) -> None:
    if isinstance(model, OriginBoundChatAnthropic):
        if client := model.__dict__.get("_client"):
            client.close()
        if client := model.__dict__.get("_async_client"):
            await client.close()
    else:
        if client := getattr(model, "http_client", None):
            client.close()
        if client := getattr(model, "http_async_client", None):
            await client.aclose()


def create_coding_chat_model(
    gateway: ModelGateway,
    override: BaseChatModel | None = None,
) -> BaseChatModel:
    """Build the native tool-calling model used by the Deep Agents graph."""

    if override is not None:
        return override
    if not isinstance(gateway, OpenAICompatibleModelGateway):
        raise ModelGatewayError(
            "Coding Agent requires a native tool-calling chat model; pass "
            "coding_model when using a custom model gateway"
        )

    config = gateway.config
    if config.api_style == "anthropic_messages":
        thinking = None
        if config.anthropic_thinking_mode != "provider_default":
            thinking = {"type": config.anthropic_thinking_mode}
            if config.anthropic_thinking_mode == "enabled":
                thinking["budget_tokens"] = config.anthropic_thinking_budget_tokens
        return OriginBoundChatAnthropic(
            model_name=config.model,
            api_key=SecretStr(config.api_key),
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=0,
            cache=False,
            max_tokens_to_sample=config.max_completion_tokens,
            streaming=True,
            stream_usage=True,
            thinking=thinking,
            temperature=(
                config.temperature
                if config.anthropic_thinking_mode in {"disabled", "provider_default"}
                else None
            ),
        )

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=config.model,
        api_key=SecretStr(config.api_key),
        base_url=config.base_url,
        timeout=config.timeout_seconds,
        max_retries=0,
        cache=False,
        http_client=provider_client(config.base_url, timeout=config.timeout_seconds),
        http_async_client=async_provider_client(config.base_url, timeout=config.timeout_seconds),
        max_completion_tokens=config.max_completion_tokens,
        temperature=config.temperature,
        reasoning_effort=config.reasoning_effort,
        streaming=True,
        stream_usage=True,
        use_responses_api=config.api_style == "responses",
    )
