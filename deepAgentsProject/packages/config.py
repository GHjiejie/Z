from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, Optional


def load_environment(project_root: Path, explicit_path: Optional[str] = None) -> list[Path]:
    """Load workspace and project dotenv files without overriding process variables.

    The desktop workspace keeps shared provider credentials one directory above this
    project. Project-local values take precedence over workspace values, while an
    explicitly exported process variable remains authoritative.
    """

    configured_path = explicit_path or os.getenv("DEEPAGENT_ENV_FILE")
    candidates: list[Path] = [project_root.parent / ".env", project_root / ".env"]
    if configured_path:
        configured = Path(configured_path).expanduser()
        candidates.append(configured if configured.is_absolute() else project_root / configured)

    merged: Dict[str, str] = {}
    loaded: list[Path] = []
    for path in _unique_paths(candidates):
        if not path.is_file():
            continue
        merged.update(_read_dotenv(path))
        loaded.append(path)
    for name, value in merged.items():
        os.environ.setdefault(name, value)
    return loaded


def _unique_paths(paths: Iterable[Path]) -> Iterable[Path]:
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            yield resolved


def _read_dotenv(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, value = line.split("=", 1)
        name = name.strip()
        if not name or not name.replace("_", "a").isalnum() or name[0].isdigit():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    return values
