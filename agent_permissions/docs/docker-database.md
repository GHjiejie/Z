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

```bash
uv run python - <<'PY'
import os
import unittest
from dotenv import dotenv_values
os.environ["AGENT_USERS_TEST_DATABASE_URL"] = dotenv_values("agent_permissions/deploy/.env")["AGENT_USERS_DATABASE_URL"]
suite = unittest.defaultTestLoader.loadTestsFromName("agent_permissions.test_postgres")
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
PY
```

测试为每个用例创建独立的随机 schema，并在结束时清理。它们复用用户管理与 RBAC 的接口契约测试，另外验证完整迁移、会话延续、导入失败回滚、重复导入保护以及 PostgreSQL 错误脱敏；不清空或写入应用的 `public` 数据。
