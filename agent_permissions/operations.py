"""Shared operation semantics for HTTP contracts, tool discovery and transactions."""

from dataclasses import dataclass


@dataclass(frozen=True)
class OperationSpec:
    description: str
    all_permissions: tuple[str, ...] = ()
    any_permissions: tuple[str, ...] = ()
    effect: str = "read"
    idempotent: bool = True
    authentication: str = "session"
    expose: bool = True

    def metadata(self) -> dict:
        if not self.expose:
            return {
                "expose": False,
                "reason": "User Agent delegation management is not an Agent tool",
            }
        return {
            "expose": True,
            "authentication": self.authentication,
            "allPermissions": list(self.all_permissions),
            "anyPermissions": list(self.any_permissions),
            "effect": self.effect,
            "idempotent": self.idempotent,
        }


OPERATIONS = {
    "auth.login": OperationSpec(
        "登录并创建会话。返回的 session_token 应由宿主安全保存，用于后续请求的认证上下文。",
        effect="write",
        idempotent=False,
        authentication="none",
    ),
    "auth.logout": OperationSpec(
        "退出当前会话，成功后该会话不能继续调用工具。", effect="write", idempotent=False
    ),
    "user.self.read": OperationSpec(
        "查看当前用户资料，返回 version 和 ETag，修改前应重新读取。",
        ("user.self.read",),
    ),
    "user.self.permissions": OperationSpec(
        "查看当前用户的角色与实时权限。", ("user.self.read",)
    ),
    "user.self.update": OperationSpec(
        "更新当前用户资料。通过 header.If-Match 提交最近读取的 ETag。",
        ("user.self.update",),
        effect="write",
        idempotent=False,
    ),
    "user.self.password.change": OperationSpec(
        "修改自己的密码。需要最近的 If-Match；成功后全部旧会话失效，需重新登录。",
        ("user.self.password.change",),
        effect="destructive",
        idempotent=False,
    ),
    "user.create": OperationSpec(
        "创建用户。创建成功返回用户资料与 ETag；分配角色还需相应权限。",
        ("user.create",),
        effect="write",
        idempotent=False,
    ),
    "user.list": OperationSpec(
        "分页查询用户，支持关键词、角色、状态和排序。page_size 最大为 100。",
        ("user.read",),
    ),
    "user.read": OperationSpec(
        "读取指定用户资料及 ETag，用于后续版本校验。", ("user.read",)
    ),
    "user.update": OperationSpec(
        "修改指定用户资料。需提交最近读取的 header.If-Match；冲突时重新读取后处理。",
        ("user.update",),
        effect="write",
        idempotent=False,
    ),
    "user.delete": OperationSpec(
        "删除指定用户并撤销其会话。需要 If-Match，删除后不能通过 API 恢复。",
        ("user.delete",),
        effect="destructive",
        idempotent=False,
    ),
    "user.status.update": OperationSpec(
        "启用或禁用指定用户，需要 If-Match。禁用时撤销用户会话。",
        ("user.status.update",),
        effect="write",
        idempotent=False,
    ),
    "user.password.reset": OperationSpec(
        "重置指定用户密码并撤销其全部旧会话，需要 If-Match。",
        ("user.password.reset",),
        effect="destructive",
        idempotent=False,
    ),
    "user.roles.assign": OperationSpec(
        "替换指定用户的角色集合，需要 If-Match，不能授予超出自身范围的权限。",
        ("user.roles.assign",),
        effect="write",
        idempotent=False,
    ),
    "role.list": OperationSpec(
        "查询全部角色定义、权限及关联用户数量。", ("role.read",)
    ),
    "role.options": OperationSpec(
        "查询可用于用户筛选与角色分配的角色选项。",
        any_permissions=("user.read", "user.create", "user.roles.assign", "role.read"),
    ),
    "permission.list": OperationSpec("查询系统权限目录及各权限含义。", ("role.read",)),
    "role.create": OperationSpec(
        "创建自定义角色，所授予权限不能超出操作者自身权限。",
        ("role.create",),
        effect="write",
        idempotent=False,
    ),
    "role.read": OperationSpec(
        "读取角色定义及 ETag，用于修改或删除前的版本校验。", ("role.read",)
    ),
    "role.update": OperationSpec(
        "修改角色及权限，需要 If-Match。权限变化对已登录用户立即生效。",
        ("role.update",),
        effect="write",
        idempotent=False,
    ),
    "role.delete": OperationSpec(
        "删除未分配给用户的自定义角色，需要 If-Match；内置角色不能删除。",
        ("role.delete",),
        effect="destructive",
        idempotent=False,
    ),
    "user.agent_tools.read": OperationSpec(
        "查看指定用户的 Agent 工具授权及当前业务权限。",
        ("user.read",),
        expose=False,
    ),
    "user.agent_tools.update": OperationSpec(
        "批量更新指定用户的 Agent 工具授权，不改变其 RBAC 权限。",
        ("user.read", "user.agent_tools.manage"),
        effect="write",
        idempotent=False,
        expose=False,
    ),
}


def operation_spec(operation: str) -> OperationSpec:
    return OPERATIONS[operation]
