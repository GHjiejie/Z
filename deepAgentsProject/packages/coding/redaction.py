from __future__ import annotations

import re


_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*([^\s'\"]+)"),
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]{12,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"LTAI[0-9A-Za-z]{12,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{12,}"),
]


def redact_text(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        def replace(match: re.Match[str]) -> str:
            if match.lastindex and match.lastindex >= 2:
                return f"{match.group(1)}=[REDACTED]"
            return "[REDACTED]"

        redacted = pattern.sub(replace, redacted)
    return redacted
