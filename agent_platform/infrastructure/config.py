"""Explicit platform configuration, separate from existing demos."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///agent_platform/.data/platform.db"
    redis_url: str | None = None
    litellm_url: str = ""
    litellm_key: str = ""
    gateway_local_address: str | None = None
    default_model: str = ""
    default_model_input_price: str = ""
    default_model_output_price: str = ""
    admin_email: str = "admin@example.com"
    admin_password: str = ""
    secure_cookies: bool = False
    embedded_worker: bool = True
    session_hours: int = 24
    run_timeout: int = 300
    model_timeout: int = 90
    max_run_cost: str = "1"
    worker_poll_seconds: float = 0.5
    web_directory: Path = Path(__file__).resolve().parents[1] / "apps/web/dist"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.getenv("PLATFORM_DATABASE_URL", cls.database_url),
            redis_url=os.getenv("PLATFORM_REDIS_URL") or None,
            litellm_url=os.getenv("PLATFORM_LITELLM_URL", ""),
            litellm_key=os.getenv("PLATFORM_LITELLM_KEY", ""),
            gateway_local_address=os.getenv("PLATFORM_GATEWAY_LOCAL_ADDRESS") or None,
            default_model=os.getenv("PLATFORM_DEFAULT_MODEL", "").strip(),
            default_model_input_price=os.getenv(
                "PLATFORM_DEFAULT_MODEL_INPUT_PRICE", ""
            ).strip(),
            default_model_output_price=os.getenv(
                "PLATFORM_DEFAULT_MODEL_OUTPUT_PRICE", ""
            ).strip(),
            admin_email=os.getenv("PLATFORM_ADMIN_EMAIL", "admin@example.com"),
            admin_password=os.getenv("PLATFORM_ADMIN_PASSWORD", ""),
            secure_cookies=os.getenv("PLATFORM_SECURE_COOKIES", "false").lower()
            == "true",
            embedded_worker=os.getenv("PLATFORM_EMBEDDED_WORKER", "true").lower()
            == "true",
            run_timeout=int(os.getenv("PLATFORM_RUN_TIMEOUT", "300")),
            model_timeout=int(os.getenv("PLATFORM_MODEL_TIMEOUT", "90")),
            max_run_cost=os.getenv("PLATFORM_MAX_RUN_COST", "1"),
        )
