"""Configuration shared by the HTTP service and administrator bootstrap."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent / "data/users.sqlite3"
    )
    session_seconds: int = 28_800
    login_account_limit: int = 10
    login_source_limit: int = 30
    password_limit: int = 10
    account_window_seconds: int = 300
    source_window_seconds: int = 60
    password_window_seconds: int = 300

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if isinstance(value, int) and value <= 0:
                raise ValueError(f"{name} must be positive")

    @classmethod
    def from_env(cls) -> "Settings":
        defaults = cls()
        values = {}
        for name in cls.__dataclass_fields__:
            raw = os.getenv(f"AGENT_USERS_{name.upper()}")
            if raw is None:
                values[name] = getattr(defaults, name)
            elif name == "database_path":
                values[name] = Path(raw).expanduser().resolve()
            else:
                values[name] = int(raw)
        return cls(**values)
