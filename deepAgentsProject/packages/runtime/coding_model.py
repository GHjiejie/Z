from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import SecretStr

from packages.runtime.model_gateway import (
    ModelGateway,
    ModelGatewayError,
    OpenAICompatibleModelGateway,
)


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
        from langchain_anthropic import ChatAnthropic

        thinking = None
        if config.anthropic_thinking_mode != "provider_default":
            thinking = {"type": config.anthropic_thinking_mode}
            if config.anthropic_thinking_mode == "enabled":
                thinking["budget_tokens"] = config.anthropic_thinking_budget_tokens
        return ChatAnthropic(
            model_name=config.model,
            api_key=SecretStr(config.api_key),
            base_url=config.base_url,
            timeout=config.timeout_seconds,
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
        max_completion_tokens=config.max_completion_tokens,
        temperature=config.temperature,
        reasoning_effort=config.reasoning_effort,
        streaming=True,
        stream_usage=True,
        use_responses_api=config.api_style == "responses",
    )
