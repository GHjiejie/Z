"""Origin-bound HTTP clients for secret-bearing provider requests."""
from __future__ import annotations

import os

import httpx


class EndpointSecurityError(ValueError):
    pass


def _url(value: str) -> httpx.URL:
    try:
        url = httpx.URL(value)
        if (url.scheme not in {"http", "https"} or not url.host
                or url.userinfo or url.fragment or url.query):
            raise ValueError
        return url
    except (ValueError, httpx.InvalidURL) as exc:
        raise EndpointSecurityError(
            "Provider URL must be an absolute HTTP(S) URL without credentials, query or fragment"
        ) from exc


def origin(url: httpx.URL) -> tuple[str, str, int]:
    return url.scheme, url.host, url.port or (443 if url.scheme == "https" else 80)


def validate_provider_url(value: str, *, allowlist_variable: str) -> str:
    url = _url(value)
    production = os.getenv("DEEPAGENT_ENVIRONMENT", "development").strip().lower() in {"prod", "production"}
    configured = os.getenv(allowlist_variable, "").strip()
    if production and url.scheme != "https":
        raise EndpointSecurityError("Production provider connections require HTTPS")
    if production and not configured:
        raise EndpointSecurityError(f"{allowlist_variable} is required in production")
    allowed = set()
    for item in configured.split(","):
        if not item.strip():
            continue
        entry = _url(item.strip())
        if entry.path not in {"", "/"} or (production and entry.scheme != "https"):
            raise EndpointSecurityError(f"{allowlist_variable} must contain explicit HTTPS origins")
        allowed.add(origin(entry))
    if configured and origin(url) not in allowed:
        raise EndpointSecurityError(f"Provider origin is not in {allowlist_variable}")
    return str(url).rstrip("/")


class OriginBoundTransport(httpx.BaseTransport):
    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None):
        self.allowed = origin(_url(base_url))
        self.inner = transport if transport is not None else httpx.HTTPTransport(retries=0, trust_env=False)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if origin(request.url) != self.allowed:
            raise httpx.RequestError("Provider request attempted an unapproved origin", request=request)
        response = self.inner.handle_request(request)
        if 300 <= response.status_code < 400:
            response.close()
            raise httpx.RequestError("Provider redirects are disabled", request=request)
        return response

    def close(self) -> None:
        self.inner.close()


class AsyncOriginBoundTransport(httpx.AsyncBaseTransport):
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self.allowed = origin(_url(base_url))
        self.inner = transport if transport is not None else httpx.AsyncHTTPTransport(retries=0, trust_env=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if origin(request.url) != self.allowed:
            raise httpx.RequestError("Provider request attempted an unapproved origin", request=request)
        response = await self.inner.handle_async_request(request)
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise httpx.RequestError("Provider redirects are disabled", request=request)
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


def provider_client(base_url: str, *, timeout: float = 60, transport=None) -> httpx.Client:
    return httpx.Client(
        transport=OriginBoundTransport(base_url, transport),
        timeout=timeout, follow_redirects=False, trust_env=False,
    )


def async_provider_client(base_url: str, *, timeout=60, transport=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=AsyncOriginBoundTransport(base_url, transport),
        timeout=timeout, follow_redirects=False, trust_env=False,
    )


def provider_event_hooks(base_url: str, *, asynchronous: bool = False) -> dict:
    """Apply the same policy to SDK-owned clients, including Anthropic's HTTPX2 client."""
    allowed = origin(_url(base_url))

    def request_hook(request):
        if origin(request.url) != allowed:
            raise EndpointSecurityError("Provider request attempted an unapproved origin")

    def response_hook(response):
        if 300 <= response.status_code < 400:
            raise EndpointSecurityError("Provider redirects are disabled")

    async def async_request_hook(request):
        request_hook(request)

    async def async_response_hook(response):
        response_hook(response)

    return {
        "request": [async_request_hook if asynchronous else request_hook],
        "response": [async_response_hook if asynchronous else response_hook],
    }
