"""Configuration shared by the HTTP service and administrator bootstrap."""

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
            if isinstance(value, int) and value <= 0:
                raise ValueError(f"{name} must be positive")

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
            else:
                values[name] = int(raw)
        return cls(**values)
