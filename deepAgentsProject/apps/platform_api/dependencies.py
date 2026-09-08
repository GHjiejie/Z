from __future__ import annotations

import hashlib
import hmac
import time
from typing import Callable, Optional

from fastapi import Depends, Header, HTTPException, Request

from packages.auth import (
    AuthenticatedPrincipal,
    AuthenticationError,
    Permission,
    is_allowed,
    permissions_for_roles,
)
from packages.domain.models import TenantContext
from packages.auth.resource_access import refresh_context


def _session_token(request: Request, authorization: Optional[str]) -> Optional[str]:
    if authorization is not None:
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(
                status_code=401,
                detail="Authorization must use a Bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return token.strip()
    auth = request.app.state.services.auth
    return request.cookies.get(auth.cookie_name)


def authenticated_principal(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> AuthenticatedPrincipal:
    token = _session_token(request, authorization)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Authentication is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return request.app.state.services.auth.authenticate(token)
    except AuthenticationError as error:
        raise HTTPException(
            status_code=401,
            detail=str(error),
            headers={"WWW-Authenticate": "Bearer"},
        ) from error


def require_super_admin(
    principal: AuthenticatedPrincipal = Depends(authenticated_principal),
) -> AuthenticatedPrincipal:
    if not principal.is_super_admin:
        raise HTTPException(
            status_code=403,
            detail="Super administrator access is required",
        )
    return principal


def require_user_manager(
    principal: AuthenticatedPrincipal = Depends(authenticated_principal),
) -> AuthenticatedPrincipal:
    if principal.must_change_password:
        raise HTTPException(
            status_code=403,
            detail="Password change is required before accessing platform administration",
        )
    if Permission.USER_MANAGE not in permissions_for_roles(
        principal.roles, is_super_admin=principal.is_super_admin
    ):
        raise HTTPException(
            status_code=403,
            detail="Platform or tenant administrator access is required",
        )
    return principal


def tenant_context(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_tenant_id: Optional[str] = Header(default=None),
    x_project_id: Optional[str] = Header(default=None),
    x_environment_id: Optional[str] = Header(default=None),
    x_user_id: Optional[str] = Header(default=None),
    x_roles: Optional[str] = Header(default=None),
    x_identity_timestamp: Optional[str] = Header(default=None),
    x_identity_signature: Optional[str] = Header(default=None),
) -> TenantContext:
    token = _session_token(request, authorization)
    if token:
        try:
            principal = request.app.state.services.auth.authenticate(token)
        except AuthenticationError as error:
            raise HTTPException(
                status_code=401,
                detail=str(error),
                headers={"WWW-Authenticate": "Bearer"},
            ) from error
        if principal.must_change_password:
            raise HTTPException(
                status_code=403,
                detail="Password change is required before accessing the platform",
            )
        return TenantContext(
            tenant_id=principal.tenant_id,
            project_id=principal.project_id,
            environment_id=principal.environment_id,
            user_id=principal.user_id,
            roles=principal.roles,
            is_super_admin=principal.is_super_admin,
            session_id=principal.session_id,
        )
    supplied = any(
        value is not None
        for value in (x_tenant_id, x_project_id, x_environment_id, x_user_id, x_roles)
    )
    if supplied:
        if not getattr(request.app.state, "trust_identity_headers", False):
            raise HTTPException(
                status_code=401,
                detail="Identity headers are disabled; configure an authenticated identity adapter",
            )
        required = {
            "X-Tenant-ID": x_tenant_id,
            "X-Project-ID": x_project_id,
            "X-Environment-ID": x_environment_id,
            "X-User-ID": x_user_id,
            "X-Roles": x_roles,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise HTTPException(
                status_code=401,
                detail="Trusted identity headers are incomplete: " + ", ".join(missing),
            )
        identity_secret = getattr(request.app.state, "identity_header_secret", None)
        if identity_secret:
            if not x_identity_timestamp or not x_identity_signature:
                raise HTTPException(
                    status_code=401,
                    detail="Signed identity timestamp and signature are required",
                )
            try:
                timestamp = int(x_identity_timestamp)
            except ValueError as error:
                raise HTTPException(
                    status_code=401, detail="Identity timestamp is invalid"
                ) from error
            if abs(int(time.time()) - timestamp) > 60:
                raise HTTPException(
                    status_code=401, detail="Identity signature has expired"
                )
            canonical = "\n".join(
                (
                    x_tenant_id or "",
                    x_project_id or "",
                    x_environment_id or "",
                    x_user_id or "",
                    x_roles or "",
                    x_identity_timestamp,
                )
            )
            expected = hmac.new(
                identity_secret.encode("utf-8"),
                canonical.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, x_identity_signature):
                raise HTTPException(
                    status_code=401, detail="Identity signature is invalid"
                )
        tenant_id = x_tenant_id
        project_id = x_project_id
        environment_id = x_environment_id
        user_id = x_user_id
        roles = x_roles
    else:
        if not getattr(request.app.state, "allow_demo_identity", False):
            raise HTTPException(
                status_code=401,
                detail="Authentication is required; demo identity is disabled",
            )
        tenant_id = "tenant_demo"
        project_id = "project_atlas"
        environment_id = "env_development"
        user_id = "user_demo"
        roles = "owner"
    return refresh_context(request.app.state.services.db, TenantContext(
        tenant_id=tenant_id,
        project_id=project_id,
        environment_id=environment_id,
        user_id=user_id,
        roles=[role.strip() for role in roles.split(",") if role.strip()],
    ))


def require_permission(
    permission: Permission,
) -> Callable[..., TenantContext]:
    """Create a FastAPI dependency for one centrally defined platform action."""

    def authorized_context(
        context: TenantContext = Depends(tenant_context),
    ) -> TenantContext:
        if not is_allowed(context, permission):
            raise HTTPException(
                status_code=403,
                detail=f"Permission is required: {permission.value}",
            )
        return context

    return authorized_context


def services(request: Request):
    return request.app.state.services
