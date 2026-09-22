"""Model configuration for the standalone CrewAI project."""

import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

PROJECT_DIR = Path(__file__).resolve().parent


def load_llm_config() -> dict[str, str]:
    """Read only this project's .env, with process environment taking priority."""
    values = {**dotenv_values(PROJECT_DIR / ".env", interpolate=False), **os.environ}
    missing = [
        key
        for key in ("OPENAI_API_KEY", "MODEL")
        if not (values.get(key) or "").strip()
    ]
    if missing:
        raise ValueError(
            f"缺少配置：{', '.join(missing)}。"
            "请先执行 make setup，再填写 crewai_agent/.env。"
        )

    base_url = (values.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip()
    address = urlsplit(base_url)
    if address.scheme not in {"http", "https"} or not address.netloc:
        raise ValueError("OPENAI_BASE_URL 必须是有效的 HTTP 或 HTTPS 地址。")

    api = (values.get("OPENAI_API") or "completions").strip()
    if api not in {"completions", "responses"}:
        raise ValueError("OPENAI_API 只能填写 completions 或 responses。")

    # HTTPX also discovers system proxies on macOS. Apply the local bypass list,
    # while allowing either casing of an explicit process setting to take priority.
    bypass = values.get("no_proxy", values.get("NO_PROXY"))
    if bypass is not None and not any(
        key in os.environ for key in ("no_proxy", "NO_PROXY")
    ):
        os.environ["no_proxy"] = bypass

    return {
        "model": values["MODEL"].strip(),
        "api_key": values["OPENAI_API_KEY"].strip(),
        "base_url": base_url,
        "api": api,
    }
