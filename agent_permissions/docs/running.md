# 用户管理服务运行说明

当前服务实现了[接口文档](user-management-api.md)中的 21 个接口，并提供 Console 用户管理页面。默认使用单机 SQLite 持久化用户、会话、审计与限流记录，无需外部数据库或模型服务。

## 1. 准备环境

在仓库根目录运行，Python 版本需为 3.13 或更高：

```bash
uv sync
```

服务复用仓库根目录的 `pyproject.toml`、`uv.lock` 和 `.venv`，不需要建立单独的 Python 环境。

## 2. 初始化管理员

首次使用时执行：

```bash
uv run python -m agent_permissions init-admin --username admin --display-name 管理员
```

命令默认将管理员初始密码设置为 `12345`，无需交互输入。以上命令创建的账号可使用 `admin / 12345` 直接登录。

如需自定义初始密码，添加 `--prompt-password`：

```bash
uv run python -m agent_permissions init-admin --username admin --prompt-password
```

该选项会交互式读取两次密码，不回显输入，也不接受命令行密码参数。自定义密码必填，不限制长度、字符组合或弱密码。

初始化仅在数据库没有用户时执行一次，不能通过重复运行覆盖已有用户或重置密码。创建成功后可使用设置的密码直接登录。

## 3. 启动 HTTP 服务

首次使用 Console 时，先在仓库根目录构建前端。需要 Node.js 20.19+（20.x）或 22.12+，以及 npm：

```bash
cd agent_permissions/console
npm ci
npm run build
cd ../..
```

再启动 FastAPI。构建后的页面由同一服务提供，无需单独运行 Node 服务：

```bash
uv run uvicorn agent_permissions.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

启动后可访问：

- [Console 管理后台](http://127.0.0.1:8000/console/)
- [Swagger 接口调试](http://127.0.0.1:8000/docs)
- [ReDoc 接口说明](http://127.0.0.1:8000/redoc)
- [OpenAPI JSON](http://127.0.0.1:8000/openapi.json)

以上链接需要本机服务处于运行状态。默认启动仅监听本机；`--no-proxy-headers` 使限流来源采用直接连接地址。若部署在反向代理后，应按实际拓扑配置受信代理，并使用 HTTPS。

根地址 `/` 和 `/console` 会跳转到 `/console/`。前端未构建时，Console 入口返回 `503 CONSOLE_NOT_BUILT`，API 仍可使用；完成构建后重新启动服务。

前端开发时可单独运行 `npm run dev`，访问 [Console 开发页面](http://127.0.0.1:5173/console/)。开发服务器将 API 和接口文档请求代理到本机 8000 端口，后端需另行启动。页面职责与项目脚本见 [Console 说明](../console/README.md)。

## 4. 首次登录步骤

1. 打开 `/console/`，使用初始化时创建的管理员账号和初始密码登录。
2. 管理员直接进入用户管理页面，可以新增、搜索、编辑用户，设置角色，重置密码，启用、禁用或删除用户。
3. 普通用户直接进入个人设置页面，只能修改自己的资料与密码；无法看到管理导航，也无法调用管理接口。
4. 需要修改密码时，在个人设置中提交当前密码、新密码和新密码确认，成功后使用新密码重新登录。

管理员创建用户或重置密码后，用户均可直接登录。页面自动管理登录令牌和版本头；版本冲突时保留输入，等待用户主动选择“载入最新信息”，不会自动覆盖其他人的修改。

通过 Swagger 联调时，在 `POST /api/v1/auth/login` 获取 `session_token`，在 Authorize 中输入令牌本身；Swagger 会添加 `Bearer` 前缀。修改前查询用户详情，读取响应中的 `ETag`，在 `If-Match` 中填写完整标签。主动改密使用 `/users/me/password`。改密成功撤销全部旧会话，重新登录后更新令牌；成功登录也会更新用户版本。

## 5. 数据与配置

默认数据库路径为 `agent_permissions/data/users.sqlite3`。数据目录已排除出版本控制，包含密码哈希、令牌摘要和审计记录，不能作为源码提交。

通过环境变量覆盖配置；管理员初始化和 HTTP 服务应使用相同配置：

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `AGENT_USERS_DATABASE_PATH` | 模块目录下的 `data/users.sqlite3` | SQLite 文件路径 |
| `AGENT_USERS_SESSION_SECONDS` | `28800` | 完整会话的固定有效期，单位秒 |
| `AGENT_USERS_LOGIN_ACCOUNT_LIMIT` | `10` | 每个账号在窗口内的登录尝试上限 |
| `AGENT_USERS_LOGIN_SOURCE_LIMIT` | `30` | 每个连接来源在窗口内的登录尝试上限 |
| `AGENT_USERS_PASSWORD_LIMIT` | `10` | 每个操作者、每类改密或重置操作的窗口内尝试上限 |
| `AGENT_USERS_ACCOUNT_WINDOW_SECONDS` | `300` | 登录账号计数窗口 |
| `AGENT_USERS_SOURCE_WINDOW_SECONDS` | `60` | 登录来源计数窗口 |
| `AGENT_USERS_PASSWORD_WINDOW_SECONDS` | `300` | 改密和重置密码计数窗口 |

例如，使用指定数据文件：

```bash
export AGENT_USERS_DATABASE_PATH=/absolute/path/to/users.sqlite3
uv run python -m agent_permissions init-admin --username admin
uv run uvicorn agent_permissions.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

所有时间和次数配置必须为正整数。代码不自动读取仓库其他模块的 `.env`。管理员初始化默认使用密码 `12345`，仅在空数据库中生效；已有用户的密码不会在初始化或服务启动时被覆盖。

限流使用持久化固定窗口。进入登录验证流程的请求，不论成功或失败，均计数；改密失败也计数。触发限制后返回 `429` 和 `Retry-After` 秒数，窗口到期后恢复。

创建用户、初始化管理员、本人改密和管理员重置密码均接受任意非空密码，允许与当前密码相同。前端只校验必填和两次输入一致；服务端按原始输入处理，不裁剪空格，并使用 Argon2id 哈希保存。

## 6. 验证

```bash
uv run python -m unittest agent_permissions.test_api agent_permissions.test_rbac agent_permissions.test_console -v
uv run ruff check agent_permissions
uv run ruff format --check agent_permissions
```

测试使用临时数据库和受控时钟，不需要真实账户，不修改默认数据目录，不调用模型或外部业务服务。

覆盖用户生命周期、首次登录直接访问、主动改密、跨用户越权、非法字段、角色立即生效、会话撤销、限流、版本冲突、并发管理员保护、数据库不可用、审计失败回滚及错误脱敏。

前端验证在 Console 目录运行：

```bash
cd agent_permissions/console
npm run typecheck
npm run format:check
npx playwright install chromium
npm run test:e2e
```

浏览器测试会构建前端，并在 18765 端口启动真实 FastAPI。测试账号仅在临时数据库中创建，测试完成后停止服务并清理数据库，不更改实际用户数据。桌面、登录与窄屏截图位于 Console 的 `test-results/`，已排除出版本控制。

## 7. 实现边界

- 当前持久化后端仅支持本机 SQLite 文件；多个进程可共享同一份本地文件，通过数据库写锁协调权限变化。
- 写事务使用 `BEGIN IMMEDIATE`，将操作者权限复核、目标版本检查、更新、会话撤销及成功审计串行提交。适合当前单机用户管理服务，不提供跨主机部署或网络共享盘数据库的保证。
- 生产升级数据库结构前需增加对应迁移。当前 schema 版本为 2，支持空库初始化及 schema 1 的原子迁移；保留账号、密码与会话，遇到未知版本停止启动。具体流程见 [RBAC 迁移说明](rbac.md#旧数据库升级)。
- 初始管理员创建属于本地维护入口；HTTP API 不提供公开注册、找回密码、删除恢复。
- 业务日志和接口错误不输出密码、密码哈希或会话令牌；排查时可通过 `request_id` 关联数据库中的审计事件。

## 8. 主要代码

| 文件 | 职责 |
| --- | --- |
| `main.py` | Uvicorn 应用入口 |
| `api.py` | FastAPI 路由、请求约束、统一响应与错误处理 |
| `service.py` | 用户业务、事务内鉴权、会话、审计与持久化限流 |
| `database.py` | SQLite schema、连接及事务 |
| `models.py` | 请求与响应字段、输入校验 |
| `permissions.py` | 权限目录和数据库授权查询 |
| `security.py` | 密码哈希、令牌摘要 |
| `config.py` | 默认值和环境变量配置 |
| `__main__.py` | 默认密码或交互式自定义密码的管理员初始化 |
| `test_api.py` | 离线接口与并发测试 |
| `console_ui.py`、`test_console.py` | Console 静态页面托管及隔离验证 |
| `console/` | React + TypeScript 页面、接口客户端与浏览器测试 |
