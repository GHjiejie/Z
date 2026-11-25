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
    interaction_policy: str | None = None
    search_examples: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.interaction_policy not in {None, "when_needed", "always_confirm"}:
            raise ValueError("Unsupported Agent interaction policy")

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
            "interactionPolicy": self.interaction_policy
            or ("when_needed" if self.effect == "read" else "always_confirm"),
            "formVersion": 1,
        }


OPERATIONS = {
    "auth.login": OperationSpec(
        "登录并创建会话。返回的 session_token 应由宿主安全保存，用于后续请求的认证上下文。",
        effect="write",
        idempotent=False,
        authentication="none",
    ),
    "auth.logout": OperationSpec(
        "退出当前会话，成功后该会话不能继续调用工具。",
        effect="write",
        idempotent=False,
        search_examples=("退出登录，结束本次会话", "断开当前登录"),
    ),
    "user.self.read": OperationSpec(
        "查看当前用户资料，返回 version 和 ETag，修改前应重新读取。",
        ("user.self.read",),
        search_examples=("看一下我的账号信息", "我叫什么名字，邮箱是什么"),
    ),
    "user.self.permissions": OperationSpec(
        "查看当前用户的角色与实时权限。",
        ("user.self.read",),
        search_examples=("我有哪些角色和访问权限",),
    ),
    "user.self.token_usage": OperationSpec(
        "查询当前登录账号的模型 Token 用量。用户由登录态确定。"
        "可选开始、结束日期按北京时间计算，省略日期时查询累计用量。",
        search_examples=("我用了多少 token", "统计我调用模型消耗的词元"),
    ),
    "user.self.update": OperationSpec(
        "更新当前用户资料。通过 header.If-Match 提交最近读取的 ETag。",
        ("user.self.update",),
        effect="write",
        idempotent=False,
        search_examples=("更换自己的昵称、显示名称或联系邮箱",),
    ),
    "user.self.password.change": OperationSpec(
        "修改自己的密码。需要最近的 If-Match；成功后全部旧会话失效，需重新登录。",
        ("user.self.password.change",),
        effect="destructive",
        idempotent=False,
        search_examples=("给自己换一个登录密码",),
    ),
    "user.create": OperationSpec(
        "创建用户。创建成功返回用户资料与 ETag；分配角色还需相应权限。",
        ("user.create",),
        effect="write",
        idempotent=False,
        search_examples=("给新同事开通一个账号",),
    ),
    "user.list": OperationSpec(
        "分页查询用户，支持关键词、角色、状态和排序。page_size 最大为 100。",
        ("user.read",),
        search_examples=("看看系统里都有谁", "按条件找出所有成员"),
    ),
    "user.read": OperationSpec(
        "读取指定用户资料及 ETag，用于后续版本校验。",
        ("user.read",),
        search_examples=("查看某位同事的账号详情",),
    ),
    "user.token_usage": OperationSpec(
        "按用户 ID 查询指定账号的模型 Token 用量。使用已知的用户 ID。"
        "可选开始、结束日期按北京时间计算，省略日期时查询累计用量。",
        ("agent.usage.read",),
        search_examples=("统计指定同事调用模型消耗的 token",),
    ),
    "user.update": OperationSpec(
        "修改指定用户资料。需提交最近读取的 header.If-Match；冲突时重新读取后处理。",
        ("user.update",),
        effect="write",
        idempotent=False,
        search_examples=("编辑指定成员的昵称、显示名称或邮箱", "修改同事的联系资料"),
    ),
    "user.delete": OperationSpec(
        "删除指定用户并撤销其会话。需要 If-Match，删除后不能通过 API 恢复。",
        ("user.delete",),
        effect="destructive",
        idempotent=False,
        search_examples=("永久移除一个用户账号",),
    ),
    "user.status.update": OperationSpec(
        "启用或禁用指定用户，需要 If-Match。禁用时撤销用户会话。",
        ("user.status.update",),
        effect="write",
        idempotent=False,
        search_examples=("暂时让同事不能登录，冻结或停用账号", "解封账号，恢复登录"),
    ),
    "user.password.reset": OperationSpec(
        "重置指定用户密码并撤销其全部旧会话，需要 If-Match。",
        ("user.password.reset",),
        effect="destructive",
        idempotent=False,
        search_examples=("同事忘了登录密码，帮他重设口令",),
    ),
    "user.roles.assign": OperationSpec(
        "替换指定用户的角色集合，需要 If-Match，不能授予超出自身范围的权限。",
        ("user.roles.assign",),
        effect="write",
        idempotent=False,
        search_examples=("调整同事所属的角色或用户组",),
    ),
    "role.list": OperationSpec(
        "查询全部角色定义、权限及关联用户数量。",
        ("role.read",),
        search_examples=("系统一共有哪几种角色",),
    ),
    "role.options": OperationSpec(
        "查询可用于用户筛选与角色分配的角色选项。",
        any_permissions=("user.read", "user.create", "user.roles.assign", "role.read"),
        search_examples=("分配账号时有哪些角色可以选",),
    ),
    "permission.list": OperationSpec(
        "查询系统权限目录及各权限含义。",
        ("role.read",),
        search_examples=("列出系统里所有权限项及说明",),
    ),
    "role.create": OperationSpec(
        "创建自定义角色，所授予权限不能超出操作者自身权限。",
        ("role.create",),
        effect="write",
        idempotent=False,
        search_examples=("新建一种自定义角色",),
    ),
    "role.read": OperationSpec(
        "读取角色定义及 ETag，用于修改或删除前的版本校验。",
        ("role.read",),
        search_examples=("这个角色拥有哪些权限",),
    ),
    "role.update": OperationSpec(
        "修改角色及权限，需要 If-Match。权限变化对已登录用户立即生效。",
        ("role.update",),
        effect="write",
        idempotent=False,
        search_examples=("调整某个角色拥有的权限",),
    ),
    "role.delete": OperationSpec(
        "删除未分配给用户的自定义角色，需要 If-Match；内置角色不能删除。",
        ("role.delete",),
        effect="destructive",
        idempotent=False,
        search_examples=("移除已经不用的自定义角色",),
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
