"""Permission catalog and database-backed RBAC queries."""

from agent_permissions.storage import Connection

PERMISSION_CATALOG = [
    ("user.self.read", "查看自己的信息与权限", "个人账号"),
    ("user.self.update", "更新自己的资料", "个人账号"),
    ("user.self.password.change", "修改自己的密码", "个人账号"),
    ("user.create", "新增用户", "用户管理"),
    ("user.read", "查询系统用户", "用户管理"),
    ("user.update", "编辑用户资料", "用户管理"),
    ("user.delete", "删除用户", "用户管理"),
    ("user.status.update", "启用或禁用用户", "用户管理"),
    ("user.password.reset", "重置用户密码", "用户管理"),
    ("user.roles.assign", "分配用户角色", "用户管理"),
    ("role.read", "查看角色与权限", "角色管理"),
    ("role.create", "新增角色", "角色管理"),
    ("role.update", "编辑角色及权限", "角色管理"),
    ("role.delete", "删除角色", "角色管理"),
]
MEMBER_PERMISSIONS = frozenset(
    {"user.self.read", "user.self.update", "user.self.password.change"}
)
ADMIN_PERMISSIONS = frozenset(code for code, _, _ in PERMISSION_CATALOG)


def roles_for(connection: Connection, user_id: str) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT role_code FROM user_roles WHERE user_id=? ORDER BY role_code",
            (user_id,),
        )
    ]


def permissions_for(connection: Connection, user_id: str) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            """SELECT DISTINCT rp.permission_code FROM user_roles ur
            JOIN role_permissions rp ON rp.role_code=ur.role_code
            WHERE ur.user_id=? ORDER BY rp.permission_code""",
            (user_id,),
        )
    ]
