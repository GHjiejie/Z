from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Mapping


class SecretConfigurationError(RuntimeError):
    pass


def read_secret(
    name: str,
    *,
    values: Mapping[str, str] | None = None,
    required: bool = False,
    production: bool | None = None,
    allow_inline_in_production: bool = False,
    max_bytes: int = 64 * 1024,
) -> str:
    """Read a secret from NAME_FILE, falling back to NAME outside production.

    Mounted secret files are deliberately preferred over environment variables.
    This keeps credentials out of process manifests and diagnostic environment
    dumps while still preserving a convenient development mode.
    """

    environment = (
        os.environ.get("DEEPAGENT_ENVIRONMENT")
        or (values or {}).get("DEEPAGENT_ENVIRONMENT")
        or "development"
    ).strip().lower()
    is_production = production if production is not None else environment in {
        "production",
        "prod",
    }
    file_name = f"{name}_FILE"
    configured_file = os.environ.get(file_name, (values or {}).get(file_name, "")).strip()
    inline = os.environ.get(name, (values or {}).get(name, "")).strip()

    if configured_file:
        path = Path(configured_file)
        try:
            info = path.stat()
        except OSError as exc:
            raise SecretConfigurationError(f"{file_name} cannot be read") from exc
        if not stat.S_ISREG(info.st_mode):
            raise SecretConfigurationError(f"{file_name} must reference a regular file")
        if info.st_size > max_bytes:
            raise SecretConfigurationError(f"{file_name} exceeds {max_bytes} bytes")
        if is_production and info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SecretConfigurationError(
                f"{file_name} must not be accessible by group or other users"
            )
        try:
            value = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise SecretConfigurationError(f"{file_name} cannot be read as UTF-8") from exc
    else:
        if inline and is_production and not allow_inline_in_production:
            raise SecretConfigurationError(
                f"{name} must be supplied through {file_name} in production"
            )
        value = inline

    if required and not value:
        raise SecretConfigurationError(f"Missing required secret: {name}")
    return value
