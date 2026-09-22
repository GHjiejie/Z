"""Verified identity and trusted membership resolution."""

import ipaddress
import re
from pathlib import Path

import jwt
from fastapi import Request
from starlette.concurrency import run_in_threadpool

from llamaindex_service.contracts import AuthContext, ServiceError


async def authenticate(request: Request) -> AuthContext:
    settings = request.app.state.settings
    repository = request.app.state.repository
    if settings.auth_mode == "dev":
        host = request.client.host if request.client else ""
        local = host == "testclient" and settings.environment == "test"
        try:
            local = local or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if not local:
            raise ServiceError("LOCAL_AUTH_ONLY", "开发身份仅允许本机访问", 403)
        context = AuthContext(
            settings.dev_tenant_id,
            settings.dev_project_id,
            settings.dev_user_id,
            ("developer",),
        )
        await run_in_threadpool(repository.ensure_membership, context)
        return context

    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise ServiceError("UNAUTHENTICATED", "需要有效的 Bearer token", 401)
    public_key = settings.jwt_public_key
    if public_key.startswith("@"):
        public_key = Path(public_key[1:]).read_text()
    try:
        claims = jwt.decode(
            authorization[7:],
            public_key,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={
                "require": [
                    "sub",
                    "exp",
                    "iat",
                    "iss",
                    "aud",
                    "tenant_id",
                    "project_id",
                ]
            },
        )
        ids = [claims[k] for k in ("tenant_id", "project_id", "sub")]
        if not all(
            isinstance(v, str) and re.fullmatch(r"[a-zA-Z0-9_.:@-]{1,128}", v)
            for v in ids
        ):
            raise jwt.InvalidTokenError("Invalid identity")
    except jwt.InvalidTokenError as exc:
        raise ServiceError("UNAUTHENTICATED", "身份凭证无效或已过期", 401) from exc
    context = AuthContext(*ids)
    membership = await run_in_threadpool(repository.get_membership, context)
    if not membership:
        raise ServiceError("MEMBERSHIP_REQUIRED", "用户不属于该租户和项目", 403)
    return AuthContext(*ids, roles=tuple(membership["roles"]))
