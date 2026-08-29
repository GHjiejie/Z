from __future__ import annotations

from typing import List, Optional

from fastapi import Header, HTTPException, Request

from packages.domain.models import TenantContext


def tenant_context(
    request: Request,
    x_tenant_id: Optional[str] = Header(default=None),
    x_project_id: Optional[str] = Header(default=None),
    x_environment_id: Optional[str] = Header(default=None),
    x_user_id: Optional[str] = Header(default=None),
    x_roles: Optional[str] = Header(default=None),
) -> TenantContext:
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
