# PostgreSQL 连接池示例

`psql` 是 PostgreSQL 的命令行客户端，用于建库和执行 SQL。Python 示例使用
`psycopg_pool.ConnectionPool` 管理 PostgreSQL 连接。

## 1. 使用 psql 创建示例数据库

以下命令假定本地 PostgreSQL 已启动，管理员用户名为 `postgres`。按实际配置修改主机、端口、用户名及密码。

```bash
psql -h 127.0.0.1 -p 5432 -U postgres -d postgres \
  -v ON_ERROR_STOP=1 -c 'CREATE DATABASE pool_demo;'
```

如果数据库已经存在，直接进行下一步。

## 2. 运行 Python 示例

在项目根目录执行，连接串中的密码替换为自己的本地数据库密码：

```bash
export DATABASE_URL='postgresql://postgres:your_password@127.0.0.1:5432/pool_demo'
uv run python database_pool/main.py
```

项目已声明 `psycopg[binary]` 和 `psycopg-pool` 依赖。若在独立 Python 环境中使用此示例，先安装：

```bash
python -m pip install 'psycopg[binary]>=3.2,<4' 'psycopg-pool>=3.2,<4'
```

程序会创建 `pool_demo_users` 表并插入 Alice、Bob；重复运行不会重复插入同名用户。
随后演示 SQL 失败时的事务回滚，再让 8 个线程任务共用最多 3 个连接。

示例输出如下，具体 PID、用户 ID 和统计值会随运行变化：

```text
模拟 SQL 失败：事务已自动回滚，Alice 的名字没有改变。
当前用户：[(1, 'Alice'), (2, 'Bob')]
任务 01 使用 PostgreSQL 连接 PID=12345
任务 02 使用 PostgreSQL 连接 PID=12346
任务 03 使用 PostgreSQL 连接 PID=12347
任务 04 使用 PostgreSQL 连接 PID=12345
...
8 个任务共使用 3 个数据库连接（上限为 3）。
连接池统计：{...}
连接池已关闭，所有连接已释放。
```

## 3. 使用 psql 验证结果

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -c 'SELECT id, name FROM pool_demo_users ORDER BY id;'
```

在另一个终端中设置同一个 `DATABASE_URL`，可在程序运行期间观察数据库会话：

```bash
psql "$DATABASE_URL" -c "SELECT pid, application_name, state
FROM pg_stat_activity
WHERE application_name = 'database_pool_demo';"
```

示例很快结束。运行结束后，这条会话查询应返回 0 行，因为池已关闭。

## 连接池的关键行为

- 一个应用进程创建一个池，业务函数通过 `with pool.connection() as conn` 借用连接。
- 正常退出连接上下文会提交事务；异常传播出上下文会回滚事务；两种情况都会归还连接。
- 在连接上下文之外捕获 SQL 异常，确保上下文管理器先完成回滚。
- `min_size=2` 表示启动时准备 2 个连接；`max_size=3` 表示这个池最多创建 3 个连接。
- `timeout=5` 控制借用连接时的等待时间，SQL 执行时间需另行配置 `statement_timeout`。
- 多个进程各有自己的池，数据库总连接数需要按进程数量累计。

参考：[Psycopg 连接池文档](https://www.psycopg.org/psycopg3/docs/advanced/pool.html)、
[PostgreSQL psql 文档](https://www.postgresql.org/docs/current/app-psql.html)。
