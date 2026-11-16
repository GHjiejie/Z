# 用户级 Agent 工具授权

Agent 工具授权按用户独立保存，与用户所属角色的业务权限共同约束执行。用户没有某项业务权限时，也能保存该工具的“允许”设置；角色权限收回或恢复不会改写设置。

## 在页面配置

登录 Console，进入“用户管理”，在目标用户的“更多操作”中选择“Agent 工具授权”。

1. 查看目标账号、角色和完整工具列表。
2. 按名称或操作 ID 搜索，勾选需要交给 Agent 的工具。
3. 点击“保存授权”。取消关闭时不保存草稿。

“允许 Agent 调用”和“当前业务权限”分别展示。显示“已允许，当前缺少业务权限”表示设置已保存，但用户的角色尚不允许执行。获得相应角色权限后会生效；权限被收回后停止生效，开关保持原值。工具设置不会增加用户的业务权限。

分组批量操作只影响本组当前搜索匹配项。“刷新权限状态”保留当前草稿；并发冲突时也保留草稿，只有主动点击“载入最新信息（替换草稿）”才载入其他人保存的新设置。账号禁用或工具下线会显示原因，历史配置保留；下线工具只能关闭。

查看需要 `user.read`，编辑还需 `user.agent_tools.manage`，且目标用户权限不能超出操作者的管理范围。内置管理员默认拥有管理权限，可以配置自己的工具。没有编辑权限时页面为只读。首版未提供普通用户在个人设置中自行授权的入口。

## 数据与版本

`user_agent_tool_grants` 保存 `user_id / operation_id / enabled / updated_by / updated_at`，组合主键为 `(user_id, operation_id)`。配置用稳定操作 ID 关联工具，例如 `user.list`。工具定义仍来自 API 契约与当前工具注册表。

`users.agent_tools_version` 是独立的配置版本，初始为 1，只有实际授权变化时增加。登录、资料编辑、用户角色和角色权限变化不增加它，现有用户资料的 `version / ETag` 不受授权编辑影响。配置差异和工具层拒绝写入现有审计表，不存储会话令牌或敏感工具参数。

缺少记录等同于 `enabled=false`。新用户、新工具及从旧版本升级的用户默认未授权，包括管理员。现有业务页面正常工作；Agent 需要先开启相应工具，未开启时仍可普通聊天。

## 配置接口

接口均使用现有 Bearer 业务会话和 `data/request_id` 响应：

| 方法与路径 | 权限 | 行为 |
| --- | --- | --- |
| `GET /api/v1/users/{user_id}/agent-tools` | `user.read` | 完整可授权目录、用户设置、实时 RBAC 与可管理状态 |
| `PATCH /api/v1/users/{user_id}/agent-tools` | `user.read` 和 `user.agent_tools.manage` | 批量更新明确提交的开关，同时校验目标管理范围 |

GET 的 `data` 包含 `user_id / version / catalog_revision / can_manage / items`。每个条目包括：

| 字段 | 含义 |
| --- | --- |
| `operation_id / tool_name` | 稳定操作 ID / 协议工具名，下线后工具名为 null |
| `display_name / resource / effect` | 名称、分组和操作性质 |
| `enabled` | 保存的用户授权设置 |
| `rbac_allowed` | 目标用户的当前角色权限是否满足操作要求 |
| `available` | 当前注册表中是否仍可授权 |
| `required_permissions` | 操作所需的 `all / any` 权限 |
| `state` | `not_granted / missing_business_permission / ready / unavailable / account_disabled` |

`ready` 表示身份状态、用户设置与业务权限满足工具入口要求，业务执行仍可能因版本或业务约束失败。

PATCH 示例：

```json
{
  "expected_version": 1,
  "catalog_revision": "GET 返回的 64 位十六进制摘要",
  "changes": [
    {"operation_id": "user.list", "enabled": true},
    {"operation_id": "user.delete", "enabled": false}
  ]
}
```

`changes` 为非空列表，不接受重复操作、未知字段、字符串形式的布尔值或不存在的操作。开启工具时只校验是否可授权，不要求目标用户已经拥有该工具的业务权限。对下线但已有记录的操作只允许关闭。未提交条目保持原值，整个批次在一个写事务内提交。

`expected_version` 过期返回 `412 VERSION_CONFLICT`；工具目录变化返回 `409 TOOL_VERSION_MISMATCH`；非法参数返回 `422 VALIDATION_ERROR`。相同值重复保存不会增加版本或产生授权变化审计。配置版本与含有 RBAC 派生状态的整个响应不同，因此本接口不使用该版本生成 ETag。

配置接口明确排除在 Agent 工具生成范围之外。

## 发现和实际执行

配置页展示全部会话工具。目前工具包共 21 项：登录为匿名认证工具，用户配置覆盖其余 20 项。登录不纳入用户授权，Chat 继续排除登录工具。

登录用户的 `GET /agent-tools` 返回“工具有效、用户已允许、当前 RBAC 允许”的交集，匿名调用仍只返回登录工具。目录设置 `Cache-Control: private, no-store`，授权变化不改变全局契约摘要。

每次 `POST /agent-tools/call` 都重新认证并读取授权。未允许时外层返回 `403 TOOL_NOT_GRANTED`。通过后继续走现有工具执行器的 RBAC 预检、参数检查和同源 HTTP 调用；原业务服务在事务内再次读取当前角色权限。目标用户身份来自请求的会话，不可由模型参数覆盖。

业务接口拒绝时保留既有工具响应封装：外层请求完成，结果为 `ok=false / httpStatus=403 / body.error.code=PERMISSION_DENIED`；预检发现无权时直接返回外层 `403 PERMISSION_DENIED`。

Chat 每轮重新发现工具，进行中的一轮即使缓存了旧目录，后续工具调用也会检查新设置。已撤销的工具被拒绝；新开放的工具下一轮可发现。没有业务工具时继续普通问答，不回退为全部工具。授权撤销不能撤回已通过检查并开始执行的操作。

## 升级与同步

SQLite / PostgreSQL schema 3 增加授权表和独立配置版本。启动时从 schema 2 原子升级；SQLite 也保留从 schema 1 的完整迁移路径。重复初始化不重置已有开关，失败整体回滚，未知版本拒绝启动。

SQLite 导入 PostgreSQL 支持 schema 2 和 3：schema 3 完整迁移工具授权；schema 2 的新字段使用默认值，校验原数据后补齐新增管理权限，报告注明源与目标版本。升级前按既有备份流程保存数据，回滚需恢复匹配的应用和数据库备份。

新增工具自动进入配置目录、默认关闭；兼容修改保留稳定操作 ID 与设置；下线后不可执行并保留历史。原样恢复同一操作 ID 时沿用设置；操作语义不兼容必须使用新 ID。

原始设计与架构图见[改造方案](user-agent-tool-grants-plan.md)。核心后端回归命令：

```bash
uv run python -m unittest agent_permissions.test_agent_grants agent_permissions.test_tools agent_permissions.test_chat_api -v
```

数据库测试使用临时 SQLite；真实 PostgreSQL 回归通过 `AGENT_USERS_TEST_DATABASE_URL` 选择测试数据库，在独立 schema 内运行并清理。Console 的 `tests/agent-tools.spec.ts` 覆盖授权保存、角色变化、只读、搜索批量范围、取消、错误和冲突草稿、快速关闭及窄屏布局。
