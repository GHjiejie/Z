# Docker PostgreSQL 与 SQLite 数据迁移

后台支持 PostgreSQL 18+ 与本地 SQLite。配置 `AGENT_USERS_DATABASE_URL` 时使用 PostgreSQL；未配置时使用 SQLite。配置了 PostgreSQL 后，连接失败会返回错误，不会自动回退到 SQLite。

## 启动独立数据库

在仓库根目录执行：

```bash
uv run python -m agent_permissions.setup_postgres
docker compose --env-file agent_permissions/deploy/.env -f agent_permissions/deploy/compose.yaml up -d --wait
```

配置生成器只创建不存在的 `deploy/.env`，权限为 `0600`；已存在时保留原配置。数据库密码随机生成，保存在本地忽略提交的文件中，不会输出到终端。`.env.example` 只包含示例值。

| 配置 | 本地默认值 |
| --- | --- |
| Compose 项目 | `agent-permissions` |
| 数据库服务 | `postgres`，镜像 `postgres:18-alpine` |
| 数据库名 / 用户名 | `agent_users` / `agent_users` |
| 本机连接地址 | `127.0.0.1:55432` |
| Docker volume | `agent-permissions_postgres_data` |
| 容器挂载目录 | `/var/lib/postgresql` |

PostgreSQL 18 官方镜像采用按主版本划分的数据目录，volume 应挂载在 `/var/lib/postgresql`。此配置遵循[官方镜像说明](https://hub.docker.com/_/postgres)。数据库仅绑定本机地址，并使用密码认证和健康检查。

## 迁移现有 SQLite

先停止旧 HTTP 服务及其他 SQLite 写入者，确保迁移完成后没有新数据继续写入旧库。然后执行：

```bash
uv run python -m agent_permissions.migrate_postgres \
  --source agent_permissions/data/users.sqlite3 \
  --env-file agent_permissions/deploy/.env
```

迁移器执行以下步骤：

1. 通过 SQLite backup API 创建一致性快照，保存在源数据库旁的 `backups/`，文件权限为 `0600`；不修改或删除源文件。
2. 检查源 schema 为 RBAC 版本 2、SQLite 完整性与外键关系。
3. 创建 PostgreSQL 表结构，确认所有应用数据表均为空；目标已有任何数据时拒绝覆盖。
4. 按外键顺序迁移权限、角色、用户、角色权限关联、用户角色关联、会话、审计和限流记录。
5. 在同一目标事务内逐表比较行数与规范化数据的 SHA-256；全部一致后提交，否则回滚整批导入。
6. 保存迁移报告到快照旁的 `.migration.json`，包含每张表的行数和校验摘要，不包含数据库连接密码。

用户 ID、用户名、密码哈希、版本、账号状态、角色定义、权限关系、会话令牌摘要、审计和限流记录均原样保留。密码不重新生成，已有有效会话继续有效。文本时间保留原始格式，整数和浮点字段分别使用 PostgreSQL `BIGINT` 和 `DOUBLE PRECISION` 保存，避免丢失原有数值精度。

此迁移是一次性导入，不做合并，也不提供覆盖已有目标数据的选项。源库须先通过 RBAC 版本升级到 schema 2。PostgreSQL 不接受文本中的 U+0000；API 对存储文本与搜索关键词提前返回校验错误，迁移遇到不能写入的旧值时回滚，保留原库和快照供处理。

## 切换后台连接

完成迁移后，使用同一配置文件启动后台：

```bash
uv run uvicorn agent_permissions.main:app \
  --host 127.0.0.1 --port 8000 --no-proxy-headers \
  --env-file agent_permissions/deploy/.env
```

页面保持为 [Console](http://127.0.0.1:8000/console/#roles)，接口契约不变。数据库连接信息由配置文件提供，控制台登录仍使用原有账号和密码。

新项目没有旧数据时，先启动数据库，再用相同配置初始化管理员：

```bash
uv run python -m agent_permissions init-admin --username admin \
  --env-file agent_permissions/deploy/.env
```

已有数据迁移时不需要重复初始化管理员。

## PostgreSQL 连接池

每个 API worker 在 FastAPI lifespan 启动时创建并预热一个同步 `psycopg_pool.ConnectionPool`；初始化成功后开始接收请求，退出或启动失败时关闭。每个事务独立借还连接，归还前提交或回滚并关闭游标；空闲连接借出前执行健康检查。SQLite 保持按操作创建、关闭连接。

默认参数如下，可在私有 `deploy/.env` 中覆盖。现有 `.env` 不会被配置生成器改写；未填写的新参数使用默认值。

| 环境变量 | 默认值 | 含义 |
| --- | ---: | --- |
| `AGENT_USERS_DB_POOL_MIN_SIZE` | `2` | 每个 worker 最小连接数 |
| `AGENT_USERS_DB_POOL_MAX_SIZE` | `10` | 每个 worker 最大连接数 |
| `AGENT_USERS_DB_POOL_TIMEOUT_SECONDS` | `3.0` | 借用连接等待预算，秒 |
| `AGENT_USERS_DB_POOL_MAX_WAITING` | `20` | 池内等待借用的队列上限 |
| `AGENT_USERS_DB_POOL_STARTUP_TIMEOUT_SECONDS` | `30.0` | 预热最小连接数的等待预算，秒 |
| `AGENT_USERS_DB_POOL_MAX_IDLE_SECONDS` | `300.0` | 超出最小容量的空闲连接回收参数，秒 |
| `AGENT_USERS_DB_POOL_MAX_LIFETIME_SECONDS` | `1800.0` | 物理连接轮换参数，秒 |
| `AGENT_USERS_DB_POOL_RECONNECT_TIMEOUT_SECONDS` | `60.0` | 连续重连失败告警阈值，秒 |
| `AGENT_USERS_DB_POOL_CLOSE_TIMEOUT_SECONDS` | `5.0` | 关闭时等待池工作线程的预算，秒 |

要求 `1 <= min_size <= max_size`、队列大小为正整数，所有秒数为有限正数。配置只在进程启动时读取，调整后需要重启。管理员与迁移 CLI 也会单独开池并在退出时关闭；一次性工具可在各自的配置文件中设置最小、最大连接数为 `1`。

**连接预算按所有进程相加**：`滚动发布期间副本峰值 × 每副本 worker 数 × max_size`，再加 CLI、迁移和其他客户端，必须小于 PostgreSQL 的连接上限减去保留预算。当前本地数据库实测 `max_connections=100`、`superuser_reserved_connections=3`；这不是其他部署的配置保证。

池等待超时、队列满、池关闭或连接故障返回脱敏的 `503 SERVICE_UNAVAILABLE`，保留请求标识和有效登录会话；这类错误记录故障类别与池统计，不再为失败审计重复排队。普通业务拒绝仍独立写失败审计。连接恢复后重新借用，不自动重放提交结果不确定的写操作。驱动与池后台日志只保留安全事件类别，不输出原始诊断、SQL 或连接串。

3 秒借用预算不等于 HTTP 请求的完整截止时间；健康检查、已开始的 SQL 和网络 I/O 有各自的耗时。原有建连 10 秒、事务锁等待 10 秒和单条 SQL 30 秒的超时继续生效。关池的 5 秒用于等待维护线程，已借出的连接在实际归还时关闭。

内部适配器通过 `pool_stats()` 提供不清空计数的白名单快照，包括容量、空闲数、等待数、借用次数、累计等待、建连尝试和异常归还。指标按 worker 采集，无新增公开 HTTP 接口。生命周期、事务约束与容量计算见[方案文档](connection-pool-plan.md)，测试和压测见[验证报告](connection-pool-validation.md)。

## 并发与搜索语义

PostgreSQL 写事务在第一次鉴权读取前取得统一的事务级 advisory lock，维持原来 SQLite `BEGIN IMMEDIATE` 的写入串行规则。角色撤销、用户修改、会话撤销、限流和最后管理员保护使用同一锁；提交或回滚时自动释放。查询使用只读 `REPEATABLE READ`，避免一次响应读取到不一致的授权与数据快照。[PostgreSQL advisory lock 文档](https://www.postgresql.org/docs/18/functions-admin.html#FUNCTIONS-ADVISORY-LOCKS)

用户搜索保留字面子串匹配，不将 `%`、`_` 当成通配符。PostgreSQL 使用 `casefold` 和 `pg_unicode_fast` 进行 Unicode 大小写折叠，例如 `STRASSE` 仍能匹配 `Straße`。[PostgreSQL 字符串函数](https://www.postgresql.org/docs/18/functions-string.html)

## 数据持久化与备份

```bash
# 查看服务与健康状态
docker compose --env-file agent_permissions/deploy/.env -f agent_permissions/deploy/compose.yaml ps

# 重启数据库，named volume 保留数据
docker compose --env-file agent_permissions/deploy/.env -f agent_permissions/deploy/compose.yaml restart postgres
```

`docker compose down` 不删除 named volume；不要在需要保留数据时使用 `down -v`。容器内目录由 Docker volume 持久化，容器重建仍可复用。`.env` 中的凭据必须与已初始化 volume 保持一致，修改配置文件不会自动更改已有数据库的密码。

迁移前 SQLite 快照只代表切换时的数据。PostgreSQL 上线后的新写入不会反向同步回 SQLite；需要回退时应先停写，并明确处理切换后的新增数据。后续数据库备份应使用 PostgreSQL `pg_dump` 等工具，备份文件按敏感数据处理。

## PostgreSQL 集成验证

先准备 PostgreSQL 18+ 专用测试数据库，在仓库外的私有配置文件中设置 `AGENT_USERS_TEST_DATABASE_URL`，文件权限为 `0600`。该用户需要创建、删除 schema 的权限，以及终止自己测试连接的权限。不要指向正在使用的业务数据库：随机 schema 仍共享数据库级 advisory lock，连接池用例还会终止自己持有的测试 backend。

```bash
uv run python - /absolute/private/agent-users-test.env <<'PY'
import os
import sys
import unittest
from dotenv import dotenv_values
os.environ["AGENT_USERS_TEST_DATABASE_URL"] = dotenv_values(sys.argv[1])["AGENT_USERS_TEST_DATABASE_URL"]
suite = unittest.defaultTestLoader.loadTestsFromNames([
    "agent_permissions.test_postgres",
    "agent_permissions.test_connection_pool",
])
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful() or bool(result.skipped))
PY
```

测试为每个用例创建独立的随机 schema，先关闭池再清理 schema。覆盖用户管理、RBAC、迁移及连接池复用、超时、队列上限、故障恢复、状态清理、资源释放和错误脱敏。未设置测试 URL 时真实 PostgreSQL 用例会跳过，不能将跳过算作验收通过。
