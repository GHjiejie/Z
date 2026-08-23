from __future__ import annotations

from typing import List, Optional

from fastapi import Header, Request

from packages.domain.models import TenantContext


def tenant_context(
    x_tenant_id: str = Header(default="tenant_demo"),
    x_project_id: str = Header(default="project_atlas"),
    x_environment_id: str = Header(default="env_development"),
    x_user_id: str = Header(default="user_demo"),
    x_roles: str = Header(default="owner"),
) -> TenantContext:
    return TenantContext(
        tenant_id=x_tenant_id,
        project_id=x_project_id,
        environment_id=x_environment_id,
        user_id=x_user_id,
        roles=[role.strip() for role in x_roles.split(",") if role.strip()],
    )


def services(request: Request):
    return request.app.state.services

