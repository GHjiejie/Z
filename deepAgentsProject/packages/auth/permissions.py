from __future__ import annotations

from enum import Enum
from typing import Iterable

from packages.domain.models import TenantContext


class Permission(str, Enum):
    """Stable action names used by the platform policy enforcement point."""

    PLATFORM_READ = "platform.read"
    USER_MANAGE = "user.manage"
    AGENT_READ = "agent.read"
    AGENT_AUTHOR = "agent.author"
    AGENT_PUBLISH = "agent.publish"
    EVALUATION_MANAGE = "evaluation.manage"
    DEPLOYMENT_READ = "deployment.read"
    DEPLOYMENT_MANAGE = "deployment.manage"
    RELEASE_APPROVE = "release.approve"
    RELEASE_GRANT_MANAGE = "release.grant.manage"
    MODEL_READ = "model.read"
    MODEL_MANAGE = "model.manage"
    BILLING_MANAGE = "billing.manage"
    PLUGIN_READ = "plugin.read"
    RUNTIME_READ = "runtime.read"
    RUNTIME_USE = "runtime.use"
    RUNTIME_CONTROL = "runtime.control"
    APPROVAL_READ = "approval.read"
    APPROVAL_DECIDE = "approval.decide"
    KNOWLEDGE_READ = "knowledge.read"
    KNOWLEDGE_MANAGE = "knowledge.manage"
    REPOSITORY_READ = "repository.read"
    REPOSITORY_MANAGE = "repository.manage"
    CODING_READ = "coding.read"
    CODING_APPROVE = "coding.approve"
    ROUTING_READ = "routing.read"
    ROUTING_USE = "routing.use"
    ROUTING_MANAGE = "routing.manage"
    ROUTING_REQUEST = "routing.request"
    ROUTING_APPROVE = "routing.approve"


READ_PERMISSIONS = frozenset(
    {
        Permission.PLATFORM_READ,
        Permission.AGENT_READ,
        Permission.DEPLOYMENT_READ,
        Permission.MODEL_READ,
        Permission.PLUGIN_READ,
        Permission.RUNTIME_READ,
        Permission.APPROVAL_READ,
        Permission.KNOWLEDGE_READ,
        Permission.REPOSITORY_READ,
        Permission.CODING_READ,
        Permission.ROUTING_READ,
    }
)

MEMBER_PERMISSIONS = READ_PERMISSIONS | {Permission.RUNTIME_USE, Permission.ROUTING_USE}
DEVELOPER_PERMISSIONS = MEMBER_PERMISSIONS | {
    Permission.AGENT_AUTHOR,
    Permission.KNOWLEDGE_MANAGE,
    Permission.REPOSITORY_MANAGE,
}
OPERATOR_PERMISSIONS = MEMBER_PERMISSIONS | {
    Permission.ROUTING_REQUEST,
    Permission.ROUTING_APPROVE,
    Permission.RELEASE_APPROVE,
    Permission.AGENT_PUBLISH,
    Permission.DEPLOYMENT_MANAGE,
    Permission.RUNTIME_CONTROL,
    Permission.APPROVAL_DECIDE,
    Permission.CODING_APPROVE,
}

ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "viewer": READ_PERMISSIONS,
    "member": MEMBER_PERMISSIONS,
    "developer": DEVELOPER_PERMISSIONS,
    "operator": OPERATOR_PERMISSIONS,
    "release_manager": OPERATOR_PERMISSIONS,
    "tenant_admin": frozenset(Permission),
    "admin": frozenset(Permission),
    "owner": frozenset(Permission),
}


def permissions_for_roles(
    roles: Iterable[str], *, is_super_admin: bool = False
) -> frozenset[Permission]:
    if is_super_admin:
        return frozenset(Permission)
    granted: set[Permission] = set()
    for role in roles:
        granted.update(ROLE_PERMISSIONS.get(role.strip().lower(), ()))
    return frozenset(granted)


def permissions_for_context(context: TenantContext) -> frozenset[Permission]:
    return permissions_for_roles(context.roles, is_super_admin=context.is_super_admin)


def is_allowed(context: TenantContext, permission: Permission) -> bool:
    return permission in permissions_for_context(context)


def authorize(context: TenantContext, *permissions: Permission) -> None:
    """Enforce actions at the application boundary, including non-HTTP callers."""
    from packages.auth.service import AuthAuthorizationError

    for permission in permissions:
        if not is_allowed(context, permission):
            raise AuthAuthorizationError(f"Permission is required: {permission.value}")
