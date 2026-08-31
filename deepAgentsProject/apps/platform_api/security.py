from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Iterable, Mapping
from urllib.parse import urlparse

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class RequestTooLargeError(Exception):
    pass


def _value(name: str, default: str, values: Mapping[str, str] | None = None) -> str:
    if name in os.environ:
        return os.environ[name]
    return (values or {}).get(name, default)


def _flag(
    name: str, default: bool = False, values: Mapping[str, str] | None = None
) -> bool:
    fallback = "true" if default else "false"
    return _value(name, fallback, values).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _origins(values: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        origin = value.strip().rstrip("/")
        if not origin:
            continue
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError(f"Invalid CORS origin: {origin}")
        normalized.append(origin)
    return tuple(dict.fromkeys(normalized))


@dataclass(frozen=True)
class SecuritySettings:
    environment: str
    cors_origins: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    csrf_enabled: bool
    max_request_bytes: int
    hsts_enabled: bool

    @property
    def production(self) -> bool:
        return self.environment in {"production", "prod"}

    @classmethod
    def from_environment(
        cls, values: Mapping[str, str] | None = None
    ) -> "SecuritySettings":
        environment = _value("DEEPAGENT_ENVIRONMENT", "development", values).strip().lower()
        origins = _origins(
            _value(
                "DEEPAGENT_CORS_ORIGINS", "http://localhost:5173", values
            ).split(",")
        )
        if not origins:
            raise RuntimeError("At least one explicit CORS origin is required")
        allowed_hosts = tuple(
            value.strip()
            for value in _value(
                "DEEPAGENT_ALLOWED_HOSTS",
                "localhost,127.0.0.1,testserver",
                values,
            ).split(",")
            if value.strip()
        )
        max_request_bytes = int(
            _value(
                "DEEPAGENT_MAX_REQUEST_BYTES", str(110 * 1024 * 1024), values
            )
        )
        if max_request_bytes < 1024:
            raise RuntimeError("DEEPAGENT_MAX_REQUEST_BYTES must be at least 1024")
        return cls(
            environment=environment,
            cors_origins=origins,
            allowed_hosts=allowed_hosts,
            csrf_enabled=_flag("DEEPAGENT_CSRF_ENABLED", True, values),
            max_request_bytes=max_request_bytes,
            hsts_enabled=_flag(
                "DEEPAGENT_HSTS_ENABLED",
                environment in {"production", "prod"},
                values,
            ),
        )

    def validate_startup(
        self,
        *,
        bootstrap_password: str,
        cookie_secure: bool,
        allow_demo_identity: bool,
        trust_identity_headers: bool,
        identity_header_secret: str | None,
    ) -> None:
        if not self.production:
            return
        errors: list[str] = []
        if bootstrap_password == "Console1@":
            errors.append("DEEPAGENT_BOOTSTRAP_ADMIN_PASSWORD must not use the default")
        if not cookie_secure:
            errors.append("DEEPAGENT_SESSION_COOKIE_SECURE must be true")
        if allow_demo_identity:
            errors.append("DEEPAGENT_ALLOW_DEMO_IDENTITY must be false")
        if trust_identity_headers and not identity_header_secret:
            errors.append(
                "DEEPAGENT_IDENTITY_HEADER_SECRET is required when trusted identity headers are enabled"
            )
        if not self.csrf_enabled:
            errors.append("DEEPAGENT_CSRF_ENABLED must be true")
        if any(origin.startswith("http://") for origin in self.cors_origins):
            errors.append("production CORS origins must use HTTPS")
        if "*" in self.allowed_hosts:
            errors.append("production allowed hosts must be explicit")
        if errors:
            raise RuntimeError("Unsafe production configuration: " + "; ".join(errors))


class EnterpriseSecurityMiddleware:
    """Enforce browser request integrity, request bounds, and security headers."""

    def __init__(self, app: ASGIApp, settings: SecuritySettings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        content_length = headers.get("content-length")
        if content_length:
            try:
                too_large = int(content_length) > self.settings.max_request_bytes
            except ValueError:
                too_large = True
            if too_large:
                await self._error(scope, receive, send, 413, "REQUEST_TOO_LARGE")
                return

        origin = (headers.get("origin") or "").rstrip("/")
        if (
            scope["method"] in UNSAFE_METHODS
            and origin
            and origin not in self.settings.cors_origins
        ):
            await self._error(scope, receive, send, 403, "ORIGIN_NOT_ALLOWED")
            return

        cookie_name = os.getenv("DEEPAGENT_SESSION_COOKIE_NAME", "deepagent_session")
        csrf_cookie_name = os.getenv("DEEPAGENT_CSRF_COOKIE_NAME", "deepagent_csrf")
        cookies = self._cookies(headers.get("cookie", ""))
        bearer = (headers.get("authorization") or "").lower().startswith("bearer ")
        is_login = scope.get("path") == "/api/v1/auth/login"
        if (
            self.settings.csrf_enabled
            and scope["method"] in UNSAFE_METHODS
            and not is_login
            and cookies.get(cookie_name)
            and not bearer
        ):
            cookie_token = cookies.get(csrf_cookie_name, "")
            header_token = headers.get("x-csrf-token", "")
            if not cookie_token or not header_token or not hmac.compare_digest(
                cookie_token, header_token
            ):
                await self._error(scope, receive, send, 403, "CSRF_VALIDATION_FAILED")
                return

        received = 0
        response_started = False

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.settings.max_request_bytes:
                    raise RequestTooLargeError
            return message

        async def secure_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                response_headers = list(message.get("headers", []))
                response_headers.extend(self._security_headers())
                message["headers"] = response_headers
            await send(message)

        try:
            await self.app(scope, bounded_receive, secure_send)
        except RequestTooLargeError:
            if not response_started:
                await self._error(scope, receive, send, 413, "REQUEST_TOO_LARGE")
                return
            raise

    async def _error(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        code: str,
    ) -> None:
        response = JSONResponse(
            status_code=status_code,
            content={"error": {"code": code, "message": code.replace("_", " ").title()}},
            headers=dict(
                (key.decode("latin-1"), value.decode("latin-1"))
                for key, value in self._security_headers()
            ),
        )
        await response(scope, receive, send)

    def _security_headers(self) -> list[tuple[bytes, bytes]]:
        connect_sources = " ".join(self.settings.cors_origins)
        headers = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
            (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
            (
                b"content-security-policy",
                b"default-src 'self'; base-uri 'self'; frame-ancestors 'none'; "
                b"form-action 'self'; img-src 'self' data: https:; "
                + f"style-src 'self' 'unsafe-inline'; connect-src 'self' {connect_sources}".encode(
                    "ascii"
                ),
            ),
        ]
        if self.settings.hsts_enabled:
            headers.append(
                (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
            )
        return headers

    @staticmethod
    def _cookies(raw: str) -> dict[str, str]:
        cookies: dict[str, str] = {}
        for part in raw.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name:
                cookies[name] = value
        return cookies
