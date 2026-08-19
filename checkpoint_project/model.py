"""Chat model configuration shared by the terminal and HTTP API."""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from pydantic import SecretStr


def build_model() -> ChatOpenAI:
    """Create the configured OpenAI-compatible chat model."""
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("缺少 OPENAI_API_KEY，请在 .env 或环境变量中设置")
    model = os.getenv("MODEL", "gpt-4.1-mini")
    base_url = os.getenv("OPENAI_BASE_URL") or None
    kwargs: dict[str, Any] = {
        "model": model,
        "temperature": 0,
        "api_key": SecretStr(api_key),
    }
    if base_url:
        kwargs["base_url"] = base_url
    return ChatOpenAI(**kwargs)
