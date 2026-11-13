"""Configuration shared by the HTTP service and administrator bootstrap."""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


@dataclass(frozen=True)
class Settings:
    database_path: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent / "data/users.sqlite3"
    )
    database_url: str | None = field(default=None, repr=False)
    db_pool_min_size: int = 2
    db_pool_max_size: int = 10
    db_pool_timeout_seconds: float = 3.0
    db_pool_max_waiting: int = 20
    db_pool_startup_timeout_seconds: float = 30.0
    db_pool_max_idle_seconds: float = 300.0
    db_pool_max_lifetime_seconds: float = 1800.0
    db_pool_reconnect_timeout_seconds: float = 60.0
    db_pool_close_timeout_seconds: float = 5.0
    session_seconds: int = 28_800
    login_account_limit: int = 10
    login_source_limit: int = 30
    password_limit: int = 10
    account_window_seconds: int = 300
    source_window_seconds: int = 60
    password_window_seconds: int = 300

    def __post_init__(self) -> None:
        if self.database_url is not None and not self.database_url.startswith(
            ("postgresql://", "postgres://")
        ):
            raise ValueError("AGENT_USERS_DATABASE_URL must be a PostgreSQL URL")
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if name.startswith("db_pool_"):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"{name} must be a positive number")
                if name.endswith("_seconds"):
                    if not math.isfinite(value) or value <= 0:
                        raise ValueError(f"{name} must be finite and positive")
                elif not isinstance(value, int) or value <= 0:
                    raise ValueError(f"{name} must be a positive integer")
            if isinstance(value, int) and value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.db_pool_min_size > self.db_pool_max_size:
            raise ValueError("db_pool_min_size must not exceed db_pool_max_size")

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        if env_file is not None and not env_file.is_file():
            raise ValueError("配置文件不存在")
        file_values = dotenv_values(env_file) if env_file else {}
        defaults = cls()
        values = {}
        for name in cls.__dataclass_fields__:
            key = f"AGENT_USERS_{name.upper()}"
            raw = os.getenv(key, file_values.get(key))
            if raw is None:
                values[name] = getattr(defaults, name)
            elif name == "database_path":
                values[name] = Path(raw).expanduser().resolve()
            elif name == "database_url":
                values[name] = raw
            elif isinstance(getattr(defaults, name), float):
                values[name] = float(raw)
            else:
                values[name] = int(raw)
        return cls(**values)
