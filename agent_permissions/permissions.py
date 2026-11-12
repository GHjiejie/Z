"""System roles map to actions, independently of HTTP route handlers."""

MEMBER_PERMISSIONS = frozenset(
    {"user.self.read", "user.self.update", "user.self.password.change"}
)
ADMIN_PERMISSIONS = MEMBER_PERMISSIONS | {
    "user.create",
    "user.read",
    "user.update",
    "user.delete",
    "user.status.update",
    "user.password.reset",
    "user.roles.assign",
}
ROLE_PERMISSIONS = {"admin": ADMIN_PERMISSIONS, "member": MEMBER_PERMISSIONS}


def permissions_for(roles: list[str]) -> list[str]:
    return sorted(
        {permission for role in roles for permission in ROLE_PERMISSIONS.get(role, ())}
    )


def role_definitions() -> list[dict]:
    return [
        {
            "code": "admin",
            "display_name": "管理员",
            "description": "管理系统内用户和角色分配",
            "permissions": sorted(ADMIN_PERMISSIONS),
        },
        {
            "code": "member",
            "display_name": "普通用户",
            "description": "管理自己的基本资料和密码",
            "permissions": sorted(MEMBER_PERMISSIONS),
        },
    ]
