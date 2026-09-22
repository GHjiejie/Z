"""Stable types shared by HTTP, persistence and model adapters."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AuthContext:
    tenant_id: str
    project_id: str
    user_id: str
    roles: tuple[str, ...] = ()


class ServiceError(Exception):
    def __init__(
        self, code: str, message: str, status_code: int = 400, details: Any = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
