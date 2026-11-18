"""Project-local Responses API client with isolated credential configuration."""

import os
from pathlib import Path

import httpx
import httpx2
import openai
from dotenv import dotenv_values
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

DEFAULT_ENV_FILE = Path(__file__).resolve().parent / ".env"
# The SDK wraps timeouts before headers. Once SSE starts, its transport raises directly.
MODEL_TIMEOUT_ERRORS = (
    openai.APITimeoutError,
    httpx2.TimeoutException,
    httpx.TimeoutException,
)


def create_chat_model(*, env_file: Path | None = None) -> ChatOpenAI:
    """Read only this project's namespaced variables and explicit local env file.

    Creating the HTTP API or importing this module does not require model
    credentials. Call this factory when constructing an Agent.
    """
    path = env_file if env_file is not None else DEFAULT_ENV_FILE
    if env_file is not None and not path.is_file():
        raise ValueError("指定的模型配置文件不存在")
    values = dotenv_values(path, interpolate=False) if path.is_file() else {}

    def required(name: str) -> str:
        key = f"AGENT_USERS_LLM_{name}"
        value = os.environ.get(key, values.get(key))
        if not value or not value.strip():
            raise ValueError(f"请在 agent_permissions/.env 或进程环境中设置 {key}")
        return value.strip()

    return ChatOpenAI(
        model=required("MODEL"),
        api_key=SecretStr(required("API_KEY")),
        base_url=required("BASE_URL"),
        use_responses_api=True,
        reasoning={"effort": "medium", "summary": "auto"},
        temperature=0,
        streaming=True,
        timeout=60,
        max_retries=0,
    )
