# 用户管理服务接口文档

| 项目 | 内容 |
| --- | --- |
| 契约版本 | `1.0.0` |
| API 版本 | `/api/v1` |
| 更新日期 | 2026-11-12 |
| 实现技术栈 | Python、FastAPI、SQLite、Argon2id |
| 当前状态 | 服务已实现，具备自动化接口测试；启动方式见运行说明 |
| 所属模块 | `agent_permissions` |

## 1. 范围与角色

本服务提供用户新增、查询、更新、删除、改密、启停和角色分配，并提供识别当前用户所需的登录与退出能力。

启动、管理员初始化和配置方法见[运行说明](running.md)。运行后可在 `/docs` 查看交互式接口文档，在 `/openapi.json` 获取自动生成的 OpenAPI。

第一版按单个系统设计，用户管理权限属于系统范围。`admin`、`member` 是系统角色；[Agent 权限设计](../README.md)中的项目角色与文档权限是后续业务授权示例，不直接作为本服务的角色。

| 角色代码 | 名称 | 能力 |
| --- | --- | --- |
| `member` | 普通用户 | 查询自己的信息与权限、更新自己的基本资料、修改自己的密码 |
| `admin` | 管理员 | 普通用户能力，以及管理系统内用户、设置用户角色 |

用户可以拥有一个或多个已定义的角色，动作权限取各角色权限的并集。角色列表必须非空，重复角色去重并按代码升序返回。

角色采用可配置 RBAC，支持新增角色、编辑权限及用户多角色分配。内置 `admin` 受保护；`member` 的默认权限可以编辑。多租户、开放注册、邮箱找回密码和 Agent 工具接口不在本契约范围内。完整模型和新增接口见 [RBAC 说明](rbac.md)。

### 1.1 权限代码

下表表示内置角色的初始授权，实际权限以当前数据库中的角色配置为准。

| 权限 | 含义 | `member` | `admin` |
| --- | --- | --- | --- |
| `user.self.read` | 查询自己的信息与权限 | 有 | 有 |
| `user.self.update` | 更新自己的基本资料 | 有 | 有 |
| `user.self.password.change` | 修改自己的密码 | 有 | 有 |
| `user.create` | 新增用户 | 无 | 有 |
| `user.read` | 查询用户列表及指定用户 | 无 | 有 |
| `user.update` | 更新用户基本资料 | 无 | 有 |
| `user.delete` | 删除用户 | 无 | 有 |
| `user.status.update` | 启用、禁用用户 | 无 | 有 |
| `user.password.reset` | 重置其他用户的密码 | 无 | 有 |
| `user.roles.assign` | 设置用户角色 | 无 | 有 |
| `role.read` | 查看角色定义与权限目录 | 无 | 有 |
| `role.create` | 新增角色 | 无 | 有 |
| `role.update` | 编辑角色及权限 | 无 | 有 |
| `role.delete` | 删除自定义角色 | 无 | 有 |

接口按具体权限鉴权，后端根据用户当前角色解析权限。客户端传入的用户 ID、角色或权限声明不能替代登录身份。

## 2. 通用约定

### 2.1 请求、时间和标识

- 所有路径均以 `/api/v1` 开头。
- SQLite 与 PostgreSQL 使用相同接口与授权契约；Docker 数据库和迁移说明见 [运行配置](docker-database.md)。
- 有请求体时使用 `Content-Type: application/json`，响应编码为 UTF-8。
- 生产环境使用 HTTPS；登录令牌通过 `Authorization` 请求头传递。
- 时间统一使用 UTC、ISO 8601 格式，例如 `2026-11-12T08:00:00Z`。
- 用户 ID 由服务端生成，是稳定且不可变的不透明字符串。示例中的 `usr_01` 仅用于说明。
- 所有认证和用户管理响应设置 `Cache-Control: no-store`。
- 服务端为每次请求生成 `request_id`，通过 `X-Request-ID` 响应头返回；有响应体时也写入响应体。
- 请求体、查询参数使用字段白名单。未知字段返回 `422 VALIDATION_ERROR`，包括在普通资料更新中提交 `roles`、`status`、`password` 等字段。
- 没有定义请求体的接口不接受非空业务请求体。路径和查询参数类型错误返回 `422`。

### 2.2 成功响应

单对象响应：

```json
{
  "data": {
    "id": "usr_01",
    "username": "zhangsan",
    "display_name": "张三",
    "email": "zhangsan@example.com",
    "roles": ["member"],
    "status": "active",
    "must_change_password": false,
    "version": 1,
    "last_login_at": null,
    "created_at": "2026-11-12T08:00:00Z",
    "updated_at": "2026-11-12T08:00:00Z"
  },
  "request_id": "req_01"
}
```

分页响应：

```json
{
  "data": {
    "items": [],
    "total": 0,
    "page": 1,
    "page_size": 20
  },
  "request_id": "req_02"
}
```

`204 No Content` 没有响应体，仍返回 `X-Request-ID` 等适用响应头。本文示例彼此独立，不表示同一条连续执行记录。

### 2.3 错误响应

```json
{
  "error": {
    "code": "USERNAME_ALREADY_EXISTS",
    "message": "用户名已存在",
    "details": [
      {
        "field": "username",
        "message": "请使用其他用户名"
      }
    ]
  },
  "request_id": "req_03"
}
```

- `code`：稳定的机器可读原因码，客户端据此处理。
- `message`：可向用户展示的说明。
- `details`：字段错误列表，无字段错误时为 `[]`。
- 密码校验错误只返回字段名和规则说明，不返回提交值、密码哈希或完整请求体。
- 生产错误响应不返回异常堆栈、SQL、内部文件路径或凭据。

### 2.4 并发控制

`GET /users/me`、`GET /users/{user_id}` 以及返回更新后用户的写接口，均返回用户版本对应的响应头：

```http
ETag: "v3"
```

所有修改已有用户的接口，包括资料、密码、角色、状态和删除，要求携带目标用户的版本条件：

```http
If-Match: "v3"
```

| 情况 | 结果 |
| --- | --- |
| 未提供 `If-Match` | `428 PRECONDITION_REQUIRED` |
| 不符合单个带引号的 `"v正整数"` 格式 | `422 VALIDATION_ERROR` |
| 版本不匹配 | `412 VERSION_CONFLICT`，本次请求不修改用户 |
| 版本匹配 | 在事务中检查业务规则并执行修改 |

本版只接受上述单个版本标签，不接受 `*`、弱标签或标签列表。客户端应重新读取用户详情，再决定是否重试；不能自动用新版本覆盖其他人的修改。

用户资料、角色、状态、密码、删除状态发生变化时增加 `version`。成功登录更新 `last_login_at`、`updated_at` 时也增加版本；单纯退出或会话过期不增加用户版本。

新增用户从 `version=1` 开始。返回 `204` 的操作完成后，若仍需编辑目标用户，应重新查询最新版本。

## 3. 接口总览

| 编号 | 方法 | 路径 | 功能 | 权限 | 成功状态 |
| --- | --- | --- | --- | --- | --- |
| API-01 | `POST` | `/api/v1/auth/login` | 登录 | 无需登录 | `200` |
| API-02 | `POST` | `/api/v1/auth/logout` | 退出当前登录 | 有效会话 | `204` |
| API-03 | `GET` | `/api/v1/users/me` | 查询自己的信息 | `user.self.read` | `200` |
| API-04 | `PATCH` | `/api/v1/users/me` | 更新自己的基本资料 | `user.self.update` | `200` |
| API-05 | `PUT` | `/api/v1/users/me/password` | 修改自己的密码 | `user.self.password.change` | `204` |
| API-06 | `GET` | `/api/v1/users/me/permissions` | 查询自己的权限 | `user.self.read`，完整会话 | `200` |
| API-07 | `POST` | `/api/v1/users` | 新增用户 | `user.create` | `201` |
| API-08 | `GET` | `/api/v1/users` | 分页查询用户 | `user.read` | `200` |
| API-09 | `GET` | `/api/v1/users/{user_id}` | 查询指定用户 | `user.read` | `200` |
| API-10 | `PATCH` | `/api/v1/users/{user_id}` | 更新用户基本资料 | `user.update` | `200` |
| API-11 | `DELETE` | `/api/v1/users/{user_id}` | 删除用户 | `user.delete` | `204` |
| API-12 | `PUT` | `/api/v1/users/{user_id}/status` | 启用、禁用用户 | `user.status.update` | `200` |
| API-13 | `PUT` | `/api/v1/users/{user_id}/password` | 管理员重置密码 | `user.password.reset` | `204` |
| API-14 | `PUT` | `/api/v1/users/{user_id}/roles` | 设置用户角色 | `user.roles.assign` | `200` |
| API-15 | `GET` | `/api/v1/roles` | 查询角色及权限 | `role.read` | `200` |
| API-16 | `GET` | `/api/v1/role-options` | 用户表单可选角色 | `user.read` / `user.create` / `user.roles.assign` / `role.read` 任一 | `200` |
| API-17 | `GET` | `/api/v1/permissions` | 查询操作权限目录 | `role.read` | `200` |
| API-18 | `POST` | `/api/v1/roles` | 新增角色并绑定权限 | `role.create` | `201` |
| API-19 | `GET` | `/api/v1/roles/{role_code}` | 查询角色详情与版本 | `role.read` | `200` |
| API-20 | `PATCH` | `/api/v1/roles/{role_code}` | 编辑角色及权限 | `role.update` | `200` |
| API-21 | `DELETE` | `/api/v1/roles/{role_code}` | 删除未使用的自定义角色 | `role.delete` | `204` |

除登录外均需有效会话，并逐次校验当前角色权限。首次登录及管理员重置密码后可直接访问有权限的接口。静态路径 `/users/me` 及其子路径应与动态用户 ID 路由明确区分。

## 4. 公共数据结构

### 4.1 User

| 字段 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 否 | 用户标识，服务端生成 |
| `username` | string | 否 | 登录名，创建后不可修改 |
| `display_name` | string | 否 | 展示名称 |
| `email` | string | 是 | 联系邮箱，不作为本版登录或密码找回凭据 |
| `roles` | string[] | 否 | 角色代码，非空、去重、升序 |
| `status` | string | 否 | `active` 或 `disabled` |
| `must_change_password` | boolean | 否 | 兼容字段；创建、改密和重置密码时为 `false`，历史值在成功登录时清除 |
| `version` | integer | 否 | 大于等于 1 的用户版本 |
| `last_login_at` | string | 是 | 最近成功登录的 UTC 时间 |
| `created_at` | string | 否 | 创建时间 |
| `updated_at` | string | 否 | 最近更新时间 |

`User` 是详情、列表项、当前用户、登录返回用户以及用户写操作响应的统一结构。`password_hash`、内部会话版本、`deleted_at` 等字段只在服务端保存，不返回客户端。

### 4.2 字段校验

| 字段 | 规则 |
| --- | --- |
| `username` | 去除首尾空格并转为小写；3～64 字符；只允许 ASCII 字母、数字、`.`、`_`、`-`；归一化后全系统唯一，含已删除账号 |
| `display_name` | 去除首尾空格后为 1～100 字符；不能为空或 `null` |
| `email` | 可省略或为 `null`；非空时去除首尾空格并验证邮箱格式，最长 254 字符；本版仅作为联系信息，不要求唯一 |
| `roles` | 创建时可省略，默认 `["member"]`；显式传入时必须非空，元素为已存在的角色编码（3～64 位小写字母、数字、下划线或连字符，字母开头）；`null` 和未知角色返回 `422` |
| 新密码 | 必填的非空 Unicode 字符串；不限制长度、字符组合或弱密码，允许与当前密码相同；不裁剪空格、不静默截断 |
| 登录密码、旧密码 | 必填的非空 Unicode 字符串，按原始输入核验，不限制长度 |
| `status` | 只接受小写 `active`、`disabled` |

密码存储使用 Argon2id 和独立盐，参数在实现时按部署环境校准，参考 [OWASP Password Storage 指南](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)。

## 5. 认证接口

### API-01 登录

```http
POST /api/v1/auth/login
Content-Type: application/json
```

请求体：

```json
{
  "username": "zhangsan",
  "password": "My new long passphrase 2026!"
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `username` | 是 | 按用户名规则归一化 |
| `password` | 是 | 登录密码原文，仅用于本次校验 |

成功响应 `200 OK`：

```json
{
  "data": {
    "session_token": "<opaque-session-token>",
    "token_type": "Bearer",
    "expires_in": 28800,
    "auth_scope": "full",
    "user": {
      "id": "usr_01",
      "username": "zhangsan",
      "display_name": "张三",
      "email": "zhangsan@example.com",
      "roles": ["member"],
      "status": "active",
      "must_change_password": false,
      "version": 3,
      "last_login_at": "2026-11-12T08:10:00Z",
      "created_at": "2026-11-12T08:00:00Z",
      "updated_at": "2026-11-12T08:10:00Z"
    }
  },
  "request_id": "req_login"
}
```

返回字段：

| 字段 | 说明 |
| --- | --- |
| `session_token` | 随机会话令牌，仅本次响应返回明文，服务端只保存摘要 |
| `token_type` | 固定为 `Bearer` |
| `expires_in` | 本次会话的有效秒数，以响应实际值为准 |
| `auth_scope` | 固定为 `full`；具体操作仍校验当前角色权限 |
| `user` | 完整 `User` 对象，包含登录完成后的版本 |

行为规则：

1. 用户存在、未删除、已启用且密码正确时才签发会话。
2. 默认会话有效期为 8 小时，即 `28800` 秒。配置调整以响应中的 `expires_in` 为准。
3. 所有成功登录均签发 `auth_scope=full` 的会话，同时清除历史 `must_change_password` 标记。用户可直接进入当前角色允许的页面。
4. 会话采用固定到期时间，本版没有自动续期和刷新令牌接口。成功改密后重新登录取得新会话。
5. 每次成功登录更新 `last_login_at`、`updated_at` 和 `version`。创建会话与账户状态检查使用一致的事务边界，避免并发禁用后仍签发有效会话。
6. 不存在、密码错误、禁用、已删除统一返回 `401 INVALID_CREDENTIALS`，提示“用户名或密码错误”。
7. 登录限流触发时返回 `429 RATE_LIMITED`，并设置 `Retry-After`。输入格式错误返回 `422`。

### API-02 退出当前登录

```http
POST /api/v1/auth/logout
Authorization: Bearer <session_token>
```

无请求体，无 `If-Match` 要求。有效会话可调用。

成功返回 `204 No Content`。服务端撤销本次请求对应的会话，其他会话保持有效；客户端清除本地令牌。已撤销、过期或无效的会话返回 `401 UNAUTHENTICATED`，客户端同样清除令牌。

## 6. 当前用户接口

### API-03 查询自己的信息

```http
GET /api/v1/users/me
Authorization: Bearer <session_token>
```

- 权限：`user.self.read`。
- 请求体：无。
- 成功：`200 OK`，`data` 为当前登录用户的完整 `User`，并返回 `ETag`。
- 当前用户从会话确定，不接受 `user_id`、`roles` 等查询参数覆盖身份。

### API-04 更新自己的基本资料

```http
PATCH /api/v1/users/me
Authorization: Bearer <session_token>
Content-Type: application/json
If-Match: "v3"
```

请求体示例：

```json
{
  "display_name": "张三丰",
  "email": "zhangsanfeng@example.com"
}
```

- 权限：`user.self.update`，完整会话。
- 可写字段仅为 `display_name`、`email`，至少提交一项。
- 字段省略表示保持原值；`email: null` 表示清空邮箱；空对象或 `display_name: null` 返回 `422`。
- 成功：`200 OK`，`data` 为更新后的 `User`，返回最新 `ETag`。
- 不改变密码、角色、状态或现有会话。

### API-05 修改自己的密码

```http
PUT /api/v1/users/me/password
Authorization: Bearer <session_token>
Content-Type: application/json
If-Match: "v3"
```

请求体：

```json
{
  "current_password": "Temporary passphrase 2026!",
  "new_password": "My new long passphrase 2026!"
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `current_password` | 是 | 当前密码，必须核验成功 |
| `new_password` | 是 | 任意非空密码，允许与当前密码相同 |

- 权限：`user.self.password.change`。
- 旧密码错误返回 `400 CURRENT_PASSWORD_INCORRECT`。
- 密码为空、类型错误或不是有效 Unicode 文本时返回 `422`。
- 成功：`204 No Content`。
- 同一事务内更新密码哈希、将 `must_change_password` 设为 `false`、增加用户版本并撤销该用户的全部会话，包括当前会话。
- 本次响应成功后，客户端清除令牌并使用新密码重新登录。后续请求携带旧会话返回 `401`。
- 改密校验也需限流，触发后返回 `429`。

要求有效会话并核验旧密码，参考 [OWASP 改密功能建议](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html#change-password-feature)。

### API-06 查询自己的权限

```http
GET /api/v1/users/me/permissions
Authorization: Bearer <session_token>
```

- 权限：`user.self.read`，完整会话。
- 请求体：无。
- 成功：`200 OK`。

响应示例：

```json
{
  "data": {
    "user_id": "usr_01",
    "roles": ["member"],
    "permissions": [
      "user.self.password.change",
      "user.self.read",
      "user.self.update"
    ]
  },
  "request_id": "req_permissions"
}
```

角色和权限均按当前服务端数据计算，列表去重并按代码升序返回。此接口供客户端展示能力和调整交互，实际操作仍需后端再次鉴权。

## 7. 用户管理接口

### API-07 新增用户

```http
POST /api/v1/users
Authorization: Bearer <session_token>
Content-Type: application/json
```

请求体：

```json
{
  "username": "zhangsan",
  "display_name": "张三",
  "email": "zhangsan@example.com",
  "password": "Temporary passphrase 2026!",
  "roles": ["member"]
}
```

| 字段 | 必填 | 默认值或说明 |
| --- | --- | --- |
| `username` | 是 | 创建后不可修改 |
| `display_name` | 是 | 展示名称 |
| `email` | 否 | 默认 `null` |
| `password` | 是 | 初始密码，遵循新密码规则 |
| `roles` | 否 | 默认 `["member"]` |

- 权限：`user.create`，完整会话。请求中包含 `admin` 角色时，还需 `user.roles.assign`；本版管理员拥有这两项权限。
- `status` 固定初始化为 `active`，`must_change_password` 固定为 `false`，由后端赋值；新用户可直接登录。
- 成功：`201 Created`，`data` 为新用户 `User`，返回 `ETag: "v1"` 和 `Location: /api/v1/users/{user_id}`。
- 用户名归一化后重复，包括与已删除用户重复，返回 `409 USERNAME_ALREADY_EXISTS`。
- 数据库唯一约束负责兜底并发重名，不能只在插入前查询一次。
- 密码不在响应和日志中回显，初始密码通过受控方式交付给新用户。
- 首名管理员由一次性初始化流程创建，初始化入口不属于公开 HTTP API。

### API-08 分页查询用户

```http
GET /api/v1/users?page=1&page_size=20&q=zhang&status=active&role=member&sort_by=created_at&sort_order=desc
Authorization: Bearer <session_token>
```

权限：`user.read`，完整会话。无请求体。

| 参数 | 类型 | 必填 | 默认值 | 规则 |
| --- | --- | --- | --- | --- |
| `page` | integer | 否 | `1` | 1～2147483647，避免分页偏移超出数据库整数范围 |
| `page_size` | integer | 否 | `20` | 1～100 |
| `q` | string | 否 | 无 | 最长 100 字符；去除首尾空格，空字符串按未筛选处理 |
| `status` | string | 否 | 无 | `active`、`disabled`；不传则包含两者 |
| `role` | string | 否 | 无 | 任意已存在角色编码；用户拥有该角色即匹配 |
| `sort_by` | string | 否 | `created_at` | `username`、`display_name`、`created_at`、`updated_at`、`last_login_at` |
| `sort_order` | string | 否 | `desc` | `asc`、`desc` |

查询规则：

- `q` 对用户名、展示名、邮箱做不区分大小写的包含匹配；按字面文本处理搜索字符。
- 多个不同筛选条件之间为 AND，搜索的三个字段之间为 OR。
- 只返回未删除用户；本版不提供查询删除记录的参数。
- `total` 为应用筛选后、分页前的匹配数；超出末页返回空 `items`，保留真实 `total`。
- 主排序值相同时按用户 ID 升序排序；`last_login_at=null` 始终放在最后。
- 分页中的每个元素为完整 `User`，权限与字段范围和详情接口一致。
- 分页查询不承诺跨多个请求冻结快照，期间新增或修改用户可能改变后续页面内容。

成功响应 `200 OK`：

```json
{
  "data": {
    "items": [
      {
        "id": "usr_01",
        "username": "zhangsan",
        "display_name": "张三",
        "email": "zhangsan@example.com",
        "roles": ["member"],
        "status": "active",
        "must_change_password": false,
        "version": 1,
        "last_login_at": null,
        "created_at": "2026-11-12T08:00:00Z",
        "updated_at": "2026-11-12T08:00:00Z"
      }
    ],
    "total": 1,
    "page": 1,
    "page_size": 20
  },
  "request_id": "req_users"
}
```

### API-09 查询指定用户

```http
GET /api/v1/users/usr_01
Authorization: Bearer <session_token>
```

- 权限：`user.read`，完整会话。
- 路径参数：`user_id`，目标用户标识。
- 请求体：无。
- 成功：`200 OK`，`data` 为 `User`，返回用户 `ETag`。
- 未找到或已删除统一返回 `404 USER_NOT_FOUND`。
- 普通用户即使填写自己的 ID，也需通过管理接口权限检查；查询自身使用 API-03。

### API-10 更新用户基本资料

```http
PATCH /api/v1/users/usr_01
Authorization: Bearer <session_token>
Content-Type: application/json
If-Match: "v3"
```

请求体示例：

```json
{
  "display_name": "张三丰",
  "email": null
}
```

- 权限：`user.update`，完整会话。
- 可写字段、字段省略与 `null` 的语义同 API-04；至少提交一个可写字段。
- `username`、`roles`、`status`、密码和只读字段不能通过此接口修改。
- 成功：`200 OK`，`data` 为更新后的 `User`，返回最新 `ETag`。
- 未找到或已删除返回 `404`；版本错误按通用约定处理。
- 允许管理员更新自己的基本资料，不撤销会话。

### API-11 删除用户

```http
DELETE /api/v1/users/usr_01
Authorization: Bearer <session_token>
If-Match: "v3"
```

- 权限：`user.delete`，完整会话。
- 请求体：无。
- 成功：`204 No Content`。
- 同一事务内记录内部删除时间、将用户设为禁用、增加版本并撤销目标用户全部会话。
- 用户 ID、用户名和必要审计关联保留；不级联删除该用户的业务历史。
- 删除后该用户不出现在列表，登录失败，详情和修改接口返回 `404`。
- 不存在或已删除的目标返回 `404 USER_NOT_FOUND`；重复删除不会再次产生状态变化。
- 删除自己返回 `409 SELF_OPERATION_NOT_ALLOWED`；触发最后管理员保护返回 `409 LAST_ADMIN_PROTECTED`。
- 本版不提供恢复和物理清理接口。

### API-12 启用、禁用用户

```http
PUT /api/v1/users/usr_01/status
Authorization: Bearer <session_token>
Content-Type: application/json
If-Match: "v3"
```

请求体：

```json
{
  "status": "disabled"
}
```

- 权限：`user.status.update`，完整会话。
- `status` 必填，只能为 `active`、`disabled`。
- 成功：`200 OK`，`data` 为目标 `User`，返回最新 `ETag`。
- 禁用时在同一事务中撤销目标用户全部会话，后续请求返回 `401`。
- 重新启用只恢复登录资格，不恢复旧会话。
- 设置为已有状态时，仍需有效版本和授权，返回当前用户；无实际变化时不增加版本。
- 禁用自己返回 `409 SELF_OPERATION_NOT_ALLOWED`；触发最后管理员保护返回 `409 LAST_ADMIN_PROTECTED`。
- 不存在或已删除返回 `404`。

### API-13 管理员重置用户密码

```http
PUT /api/v1/users/usr_01/password
Authorization: Bearer <session_token>
Content-Type: application/json
If-Match: "v3"
```

请求体：

```json
{
  "new_password": "Another temporary passphrase 2026!"
}
```

- 权限：`user.password.reset`，完整会话。
- `new_password` 必填，接受任意非空密码，允许与目标用户现密码相同，无需提供目标用户旧密码。
- 密码为空、类型错误或不是有效 Unicode 文本时返回 `422`。
- 成功：`204 No Content`。
- 同一事务内更新密码哈希、将 `must_change_password` 设为 `false`、增加用户版本并撤销目标用户全部会话。
- 保留目标用户原来的启用或禁用状态；重置密码不自动启用账号。
- 目标用户可以使用重置后的密码直接登录并访问有权限的页面。
- 管理员对自己调用此接口返回 `409 SELF_OPERATION_NOT_ALLOWED`，自己的密码使用 API-05 修改。
- 不存在或已删除返回 `404`。该接口也需限流并记录审计事件。

### API-14 设置用户角色

```http
PUT /api/v1/users/usr_01/roles
Authorization: Bearer <session_token>
Content-Type: application/json
If-Match: "v3"
```

请求体：

```json
{
  "roles": ["admin"]
}
```

- 权限：`user.roles.assign`，完整会话。
- `roles` 必填且非空；仅接受已定义角色，完整替换原角色列表。
- 成功：`200 OK`，`data` 为更新后的 `User`，返回最新 `ETag`。
- 角色变化后，已有会话的后续请求按新角色鉴权，不等待会话自然过期。会话本身保持有效。
- 去重后的角色集合没有变化时，返回当前用户，不增加版本。
- 管理员移除自己的 `admin` 角色返回 `409 SELF_OPERATION_NOT_ALLOWED`。
- 触发最后管理员保护返回 `409 LAST_ADMIN_PROTECTED`；不存在或已删除返回 `404`。
- 禁用用户也可预先设置角色，但不会因此恢复登录资格。

### API-15～API-21 角色与权限管理

`GET /roles` 查询持久化角色定义，所需权限为 `role.read`。角色详情新增 `built_in/user_count/version/created_at/updated_at`，角色列表以内置角色优先、编码升序返回。管理员默认拥有全部 14 项权限。

新增角色选项、权限目录、角色创建、详情、更新与删除接口。请求字段、响应示例、版本条件、授权范围、角色占用保护和数据库迁移见 [可配置 RBAC 接口说明](rbac.md)。

## 8. 认证、授权与事务规则

### 8.1 会话检查

后续请求使用以下头部：

```http
Authorization: Bearer <session_token>
```

服务端读取会话记录并检查：令牌有效、会话未撤销且未过期、所属用户未删除且已启用、当前角色具备所需权限。

| 情况 | 返回 |
| --- | --- |
| 凭据缺失、无效、过期、已撤销，或所属用户已禁用、删除 | `401 UNAUTHENTICATED` |
| 完整会话缺少操作权限 | `403 PERMISSION_DENIED` |
| 无法读取认证或权限数据 | `503 SERVICE_UNAVAILABLE`，不执行受保护操作 |

权限来源为当前服务端数据，不能只在登录时缓存一份永久生效的角色快照。用户和权限查询结果不能当作后续写请求的执行凭证。

### 8.2 管理写操作与管理员保护

- 修改事务内重新核验操作者当前权限、会话状态、目标状态及 `If-Match`，防止在鉴权后、提交前使用已撤销的权限。
- 管理员不能删除、禁用自己，不能移除自己的 `admin` 角色；这类请求返回 `SELF_OPERATION_NOT_ALLOWED`。
- 删除、禁用、移除角色均不能使系统失去最后一名未删除且启用的管理员。
- 最后管理员保护必须在事务中通过串行化校验或等效锁实现，覆盖两个管理员同时修改对方的情况；不能只做事务外计数。
- 有效的初始管理员应在服务对外开放前完成初始化，设置密码后即可直接登录管理后台。
- 会话撤销、密码更新和账号状态变更需要原子提交，并与并发登录协调；事务失败不得只修改其中一部分。
- 对已授权管理请求，未找到或已删除目标统一为 `404`。目标存在后再检查其版本条件，版本通过后检查自身操作和最后管理员保护。

### 8.3 更新与重试

- PATCH 更新省略字段表示不修改，显式 `null` 只在允许清空的字段上合法。
- 资料、状态、角色请求没有造成实际变化时，允许返回当前 `User` 且保持版本；版本条件仍必须满足。
- 密码更新成功必须增加版本并执行会话撤销，不能作为普通资料更新处理。
- 创建接口未定义自动重试幂等键。网络结果不确定时，应由管理员按用户名查询确认，避免盲目重复提交；用户名唯一约束避免创建同名账号。
- 修改接口在旧版本上重复提交返回 `412`。删除后的再次删除返回 `404`，不会重复变更状态。

### 8.4 审计与限流

记录登录结果、退出、用户创建、资料更新、密码修改或重置、状态变化、用户角色变化、角色定义和权限变更及删除。事件包含操作者、目标用户、动作、结果、时间、请求标识和必要的变更摘要。

管理状态变化与对应审计事件原子记录；失败操作可单独记录结果。资料变更摘要按必要范围保存，密码操作仅记录发生过变更，不记录原文、哈希、会话令牌或完整请求体。

登录采用账号维度和来源维度的组合限流，避免未知用户名绕过来源限制。旧密码校验、密码重置等敏感接口另设限流；具体阈值由部署配置提供，触发后返回 `429` 和 `Retry-After` 秒数。

## 9. 状态码与原因码

| HTTP 状态 | 原因码 | 场景 |
| --- | --- | --- |
| `200` | 不适用 | 查询或返回更新后的用户成功 |
| `201` | 不适用 | 创建用户成功 |
| `204` | 不适用 | 退出、改密、重置密码、删除成功，无响应体 |
| `400` | `CURRENT_PASSWORD_INCORRECT` | 当前密码验证失败 |
| `401` | `INVALID_CREDENTIALS` | 登录用户名或密码错误，或账号不能登录 |
| `401` | `UNAUTHENTICATED` | 受保护接口的会话缺失、无效或失效 |
| `403` | `PERMISSION_DENIED` | 当前用户缺少所需权限 |
| `404` | `USER_NOT_FOUND` | 目标用户不存在或已删除 |
| `409` | `USERNAME_ALREADY_EXISTS` | 归一化用户名冲突 |
| `409` | `SELF_OPERATION_NOT_ALLOWED` | 禁止的自身删除、禁用、降级或管理员重置自身密码 |
| `409` | `LAST_ADMIN_PROTECTED` | 操作会使系统失去最后一名有效管理员 |
| `412` | `VERSION_CONFLICT` | 目标版本已经变化 |
| `422` | `VALIDATION_ERROR` | 字段、查询参数、请求体或版本标签格式不合法 |
| `428` | `PRECONDITION_REQUIRED` | 修改已有用户时缺少 `If-Match` |
| `429` | `RATE_LIMITED` | 超出请求或校验频率 |
| `500` | `INTERNAL_ERROR` | 未预期内部异常，返回通用说明及请求标识 |
| `503` | `SERVICE_UNAVAILABLE` | 无法完成认证、授权或必要持久化 |

`401` 响应包含 `WWW-Authenticate: Bearer`；`429` 响应包含 `Retry-After`。所有错误使用第 2.3 节的统一响应结构。

## 10. 联调与验收场景

以下为实现遵循的验收要求；自动化测试位于 `agent_permissions/test_api.py`，执行方式见运行说明：

| 编号 | 场景 | 预期 |
| --- | --- | --- |
| AC-01 | 管理员新增普通用户并查询详情 | 创建 `201`；查询 `200`；无密码和内部凭据字段 |
| AC-02 | 用户名大小写或首尾空格不同但归一化后相同 | `409 USERNAME_ALREADY_EXISTS` |
| AC-03 | 用户使用初始密码登录 | 取得 `full` 会话；可直接访问当前角色允许的接口 |
| AC-04 | 用户提交正确旧密码完成改密 | `204`；全部旧会话失效；新密码可重新登录 |
| AC-05 | 普通用户查询、编辑其他用户，或分配角色 | `403 PERMISSION_DENIED`，目标数据不变 |
| AC-06 | 在资料更新中注入角色、状态或密码字段 | `422 VALIDATION_ERROR`，无部分更新 |
| AC-07 | 管理员重置他人密码 | `204`；旧会话失效；重置密码可直接登录；原状态保留 |
| AC-08 | 禁用后重新启用用户 | 禁用时旧会话失效；启用后需重新登录 |
| AC-09 | 两个客户端基于相同版本编辑同一用户 | 首次成功；旧版本修改返回 `412`，不覆盖首次修改 |
| AC-10 | 删除用户后查询、登录、再次删除 | 查询及再次删除 `404`；登录统一 `401`；用户名仍保留 |
| AC-11 | 管理员删除、禁用或降级自己 | `409 SELF_OPERATION_NOT_ALLOWED` |
| AC-12 | 两名管理员同时尝试使对方失去管理资格 | 不能出现零名有效管理员；至少一项操作被保护规则或权限复核拒绝 |
| AC-13 | 管理员被其他管理员移除角色后使用旧会话继续管理 | 后续管理请求 `403`，普通自助能力仍有效 |
| AC-14 | 列表使用组合筛选、排序及空页 | `total`、筛选、稳定排序和分页语义符合文档 |
| AC-15 | 修改操作缺少版本头、版本头格式错误 | 分别 `428`、`422`，无状态变化 |
| AC-16 | 无法读取会话或权限数据 | `503`，受保护操作不执行 |
| AC-17 | 密码字段校验失败、认证失败及操作审计 | 错误和日志均无密码原文、哈希或令牌 |
| AC-18 | 登录、校验旧密码超过部署限流阈值 | `429`，包含有效 `Retry-After` |

## 11. 与后续 Agent 授权的连接

本服务产生可信用户身份，角色映射产生系统动作权限。后续 Agent 入口使用当前会话确定用户，再由服务端构建 Agent 运行上下文。

Agent 调用用户管理工具时，必须复用相同的业务服务授权与事务规则。不能将 `GET /users/me/permissions` 的结果、聊天中声明的角色或模型填写的用户 ID 当作执行凭证。

本契约中的 `admin` 仅表示本系统用户管理角色；后续项目、文档或外部系统的访问范围，需要在对应业务授权契约中单独定义。
