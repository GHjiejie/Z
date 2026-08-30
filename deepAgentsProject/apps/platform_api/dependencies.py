from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException, Request

from packages.auth import AuthenticatedPrincipal, AuthenticationError
from packages.domain.models import TenantContext


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
    if not principal.is_super_admin and "tenant_admin" not in principal.roles:
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
    return TenantContext(
        tenant_id=tenant_id,
        project_id=project_id,
        environment_id=environment_id,
        user_id=user_id,
        roles=[role.strip() for role in roles.split(",") if role.strip()],
    )


def services(request: Request):
    return request.app.state.services
