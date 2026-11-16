"""Project-local LangSmith tracing for one authenticated Chat request at a time."""

import json
import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

from dotenv import dotenv_values
from langsmith import Client, tracing_context
from langsmith.anonymizer import create_secret_anonymizer
from pydantic import SecretStr

logger = logging.getLogger(__name__)
DEFAULT_ENV_FILE = Path(__file__).resolve().parent / ".env"
_secret_anonymizer = create_secret_anonymizer(
    extra_rules=[{"pattern": re.compile(r"sk-[A-Za-z0-9_-]{16,}")}], max_depth=64
)
_private_fields = {
    "authorization",
    "cookie",
    "set_cookie",
    "password",
    "password_hash",
    "current_password",
    "new_password",
    "confirm_password",
    "session_token",
    "token_digest",
    "api_key",
    "openai_api_key",
    "access_token",
    "refresh_token",
}


def _redact_fields(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if str(key).lower().replace("-", "_") in _private_fields
            else _redact_fields(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_fields(item) for item in value]
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            return value
        return json.dumps(_redact_fields(parsed), ensure_ascii=False)
    return value


def redact_trace(data: dict) -> dict:
    """Mask structured credentials, including JSON stored inside ToolMessage text."""
    return _secret_anonymizer(_redact_fields(data))


@dataclass(frozen=True)
class TracingSettings:
    enabled: bool = False
    api_key: SecretStr | None = field(default=None, repr=False)
    endpoint: str = "https://api.smith.langchain.com"
    project: str = "agent-permissions"
    workspace_id: str | None = None
    hide_inputs: bool = False
    hide_outputs: bool = False

    def __post_init__(self) -> None:
        if not self.enabled:
            return
        if not self.api_key or not self.api_key.get_secret_value().strip():
            raise ValueError("启用 LangSmith 需要 LANGSMITH_API_KEY")
        url = urlsplit(self.endpoint)
        if url.scheme not in {"http", "https"} or not url.netloc or url.username:
            raise ValueError("LANGSMITH_ENDPOINT 须为有效的 HTTP 服务地址")
        if not self.project:
            raise ValueError("LANGSMITH_PROJECT 不能为空")

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "TracingSettings":
        path = env_file if env_file is not None else DEFAULT_ENV_FILE
        if env_file is not None and not path.is_file():
            raise ValueError("指定的 LangSmith 配置文件不存在")
        values = dotenv_values(path, interpolate=False) if path.is_file() else {}

        def read(name: str, default: str = "") -> str:
            return (os.environ.get(name, values.get(name)) or default).strip()

        def flag(name: str) -> bool:
            raw = read(name, "false").lower()
            if raw in {"true", "1", "yes", "on"}:
                return True
            if raw in {"false", "0", "no", "off"}:
                return False
            raise ValueError(f"{name} 须为布尔值")

        key = read("LANGSMITH_API_KEY")
        return cls(
            enabled=flag("LANGSMITH_TRACING"),
            api_key=SecretStr(key) if key else None,
            endpoint=read("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"),
            project=read("LANGSMITH_PROJECT", "agent-permissions"),
            workspace_id=read("LANGSMITH_WORKSPACE_ID") or None,
            hide_inputs=flag("LANGSMITH_HIDE_INPUTS"),
            hide_outputs=flag("LANGSMITH_HIDE_OUTPUTS"),
        )


def _upload_error(error: Exception) -> None:
    logger.warning("LangSmith upload failed error_type=%s", type(error).__name__)


class ChatTracing:
    """Reuse the HTTP client; scope tracing context to a single Agent execution."""

    def __init__(self, settings: TracingSettings, client: Client | None = None) -> None:
        self.settings = settings
        self._client = client
        self._lock = Lock()
        self._failed = False

    @classmethod
    def from_env(cls) -> "ChatTracing":
        try:
            return cls(TracingSettings.from_env())
        except (ValueError, OSError):
            logger.warning("LangSmith configuration invalid; tracing disabled")
            return cls(TracingSettings())

    def _get_client(self) -> Client | None:
        if not self.settings.enabled or self._failed:
            return None
        with self._lock:
            if self._failed:
                return None
            if self._client is None:
                try:
                    self._client = Client(
                        api_url=self.settings.endpoint,
                        api_key=self.settings.api_key.get_secret_value(),
                        workspace_id=self.settings.workspace_id,
                        timeout_ms=2000,
                        anonymizer=redact_trace,
                        hide_inputs=self.settings.hide_inputs,
                        hide_outputs=self.settings.hide_outputs,
                        omit_traced_runtime_info=True,
                        disable_prompt_cache=True,
                        tracing_sampling_rate=1.0,
                        tracing_error_callback=_upload_error,
                    )
                except Exception as error:  # noqa: BLE001 -- optional telemetry must not break business requests
                    self._failed = True
                    _upload_error(error)
            return self._client

    @contextmanager
    def context(
        self, *, request_id: str, model: str, stream: bool, tools: list[str]
    ) -> Iterator[None]:
        client = self._get_client()
        with tracing_context(
            enabled=client is not None,
            client=client,
            project_name=self.settings.project,
            parent=False,
            tags=["agent-permissions", "chat", "sse" if stream else "completion"],
            metadata={
                "request_id": request_id,
                "model": model,
                "stream": stream,
                "tool_count": len(tools),
                "tool_names": tools,
            },
        ):
            yield

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.close(timeout=3)
            except Exception as error:  # noqa: BLE001 -- telemetry shutdown must not prevent database cleanup
                _upload_error(error)
