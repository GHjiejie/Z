# 可配置 RBAC

用户权限按 `用户 → 用户角色关联 → 角色 → 角色权限关联 → 操作权限` 解析。用户可以绑定多个角色，最终权限为所有角色权限的并集。不向用户直接保存或分配权限。

## 数据与鉴权

| 表 | 职责 |
| --- | --- |
| `users` | 账号、密码哈希、状态与用户版本 |
| `roles` | 角色编码、名称、说明、内置标记与角色版本 |
| `permissions` | 服务端支持的操作权限目录 |
| `user_roles` | 用户到角色的多对多关联 |
| `role_permissions` | 角色到权限的多对多关联 |

关联表使用组合主键与外键，拒绝不存在的角色或权限。权限目录由实际服务端动作定义，共 14 项；角色从目录中选择权限，不接受未实现的动作代码。角色编码为 3～64 位小写字母、数字、下划线或连字符，首位为字母，创建后不可修改。

后端每次请求都通过会话定位用户，再从关联表读取当前角色和权限。会话只引用身份，不缓存角色快照。写操作在 `BEGIN IMMEDIATE` 事务内重新鉴权、检查版本、更新关联并写入审计，因此角色权限撤销、用户角色调整和并发修改能够串行生效。

`admin` 是受保护的内置系统管理员，拥有全部操作权限，不允许编辑或删除。`member` 默认拥有三项个人账号权限，管理员可以修改其权限，但不能删除该内置角色。新增自定义角色可按需选择任意权限子集，包括空集合。访问 Console 需要 `user.self.read`；无此权限时，页面显示权限拒绝状态。资料保存、改密、用户操作和角色管理控件分别按对应权限显示。

## 角色管理 API

所有接口沿用 `/api/v1`、Bearer 会话、统一 `data/request_id` 响应及错误结构。详情和成功写操作返回 `ETag: "vN"`；编辑与删除必须携带对应角色的 `If-Match`。用户版本和角色版本独立。

| 方法与路径 | 所需权限 | 行为 |
| --- | --- | --- |
| `GET /roles` | `role.read` | 返回所有角色，内置角色优先，再按编码排序 |
| `GET /role-options` | `user.read`、`user.create`、`user.roles.assign`、`role.read` 任一 | 返回角色编码、名称和说明，供用户筛选与分配使用，不返回权限配置 |
| `GET /permissions` | `role.read` | 返回权限目录，每项包含 `code/display_name/resource`，按资源分组展示 |
| `POST /roles` | `role.create` | 创建角色，同时绑定所选权限，返回 `201`、角色详情和 `Location` |
| `GET /roles/{role_code}` | `role.read` | 查询角色详情和最新版本 |
| `PATCH /roles/{role_code}` | `role.update` | 更新名称、说明、权限集合，返回最新详情与版本 |
| `DELETE /roles/{role_code}` | `role.delete` | 删除无未删除用户引用的自定义角色，返回 `204` |

创建请求示例：

```json
{
  "code": "user_auditor",
  "display_name": "用户审计员",
  "description": "查看系统成员，不允许修改其他用户",
  "permissions": ["user.self.read", "user.self.update", "user.self.password.change", "user.read"]
}
```

角色详情的 `data` 结构：

```json
{
  "code": "user_auditor",
  "display_name": "用户审计员",
  "description": "查看系统成员，不允许修改其他用户",
  "permissions": ["user.read", "user.self.password.change", "user.self.read", "user.self.update"],
  "built_in": false,
  "user_count": 0,
  "version": 1,
  "created_at": "2026-11-13T00:00:00Z",
  "updated_at": "2026-11-13T00:00:00Z"
}
```

`GET /users/me/permissions` 保留 `user_id/roles/permissions`，并增加 `role_details`，返回当前用户所绑定角色的编码、名称和说明。Console 的账号区域与个人设置据此显示真实角色名称，包括已改名的内置角色和自定义角色。

创建必填 `code/display_name`，`description` 默认空字符串、最长 500 字符，`permissions` 默认空数组、去重升序保存。PATCH 至少提交 `display_name/description/permissions` 中的一项，不接受未知字段或 `null`；`permissions` 表示完整替换，空数组表示撤销该角色的全部操作权限。成功 PATCH 增加角色版本。角色权限变化不会改变用户的角色编码列表或用户资料版本，实际操作仍逐次读取最新授权。

## 用户角色分配与授权边界

现有 `POST /users` 和 `PUT /users/{user_id}/roles` 支持任意已存在角色编码，角色列表必须非空。默认新增用户绑定 `member`；指定其他角色组合还需 `user.roles.assign`。用户角色调整增加用户版本，沿用原有用户 `If-Match`。`GET /users?role=user_auditor` 按关联表过滤。

操作者只能授予自己当前有效权限的子集。创建、编辑或删除角色时检查授权范围；分配用户角色时检查新角色的权限范围。管理其他用户的资料、状态、角色或密码时，还检查目标用户的权限没有超出操作者范围，避免通过重置高权限账号密码取得更大的权限。失败返回 `403 PERMISSION_DENIED`，不修改授权。

内置管理员角色、最后一名启用管理员、自我禁用或删除等原有保护继续生效。禁用、删除、改密和重置密码仍撤销目标会话。角色权限和用户角色调整不撤销会话，已登录用户的后续请求立即使用新权限。

| 错误码 | HTTP | 含义 |
| --- | --- | --- |
| `ROLE_CODE_TAKEN` | `409` | 角色编码已被使用 |
| `ROLE_NOT_FOUND` | `404` | 角色不存在 |
| `ROLE_IN_USE` | `409` | 仍有未删除用户使用此角色，含已禁用用户 |
| `SYSTEM_ROLE_PROTECTED` | `409` | 修改受保护管理员角色，或删除内置角色 |
| `VALIDATION_ERROR` | `422` | 角色编码、字段或权限目录不合法 |
| `PRECONDITION_REQUIRED` | `428` | 缺少角色版本 |
| `VERSION_CONFLICT` | `412` | 角色已被其他操作修改，须重新读取 |

Console 在版本冲突时保留草稿，只有点击“载入最新信息”才覆盖输入。删除角色前展示确认；已有用户占用时禁用删除并说明原因，后端仍在事务内再次检查，防止并发分配绕过。

## 旧数据库升级

数据库 schema 由 1 升为 2。启动时在单个事务中创建 RBAC 表、初始化权限目录和内置角色，将旧 `users.roles` 的有效用户关联迁移至 `user_roles`，然后移除旧 JSON 字段，最后更新 schema 版本。保留用户 ID、用户名、密码哈希、状态、版本、会话、限流与审计数据。

存在未知旧角色或其他迁移错误时完整回滚，保持 schema 1；未知 schema 版本拒绝启动。重复启动不覆盖自定义角色或普通用户角色已经配置的权限。实际数据库升级前可使用 SQLite backup API 保存一致性备份；回滚版本需要恢复对应备份，不能直接将旧代码连接至 schema 2。

## 验证

```bash
uv run python -m unittest agent_permissions.test_api agent_permissions.test_rbac agent_permissions.test_console -v
uv run ruff check agent_permissions
uv run ruff format --check agent_permissions
```

浏览器测试覆盖角色创建、权限选择、用户多角色分配、只读管理访问、已登录用户权限撤销、占用删除、角色删除、编辑冲突保留草稿，以及没有基础权限时的错误恢复。
