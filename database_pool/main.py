"""PostgreSQL 连接池示例，建库及运行命令见同目录 README.md。"""

import os
from concurrent.futures import ThreadPoolExecutor

from psycopg.errors import DivisionByZero
from psycopg_pool import ConnectionPool, PoolTimeout


def initialize_users(pool: ConnectionPool) -> None:
    """正常退出 connection() 时提交事务，并把连接归还池中。"""
    with pool.connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pool_demo_users (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            )
            """
        )
        for name in ("Alice", "Bob"):
            # 参数通过 %s 绑定，不使用字符串拼接或 f-string 构造 SQL。
            conn.execute(
                "INSERT INTO pool_demo_users (name) VALUES (%s) "
                "ON CONFLICT (name) DO NOTHING",
                (name,),
            )


def demonstrate_rollback(pool: ConnectionPool) -> None:
    """异常须传播出 connection()，上下文管理器才会自动回滚。"""
    try:
        with pool.connection() as conn:
            conn.execute(
                "UPDATE pool_demo_users SET name = %s WHERE name = %s",
                ("Alice (should be rolled back)", "Alice"),
            )
            conn.execute("SELECT 1 / 0")  # 故意触发数据库异常。
    except DivisionByZero:
        print("模拟 SQL 失败：事务已自动回滚，Alice 的名字没有改变。")

    # 归还后的连接可继续使用，不会残留上次失败的事务。
    with pool.connection() as conn:
        users = conn.execute(
            "SELECT id, name FROM pool_demo_users ORDER BY id"
        ).fetchall()
        print(f"当前用户：{users}")


def run_task(pool: ConnectionPool, task_id: int) -> int:
    """每个任务独立借用连接；多个线程共享池，不共享正在使用的连接。"""
    with pool.connection() as conn:
        # 仅为演示并发占用连接而等待 0.2 秒，便于观察排队和复用。
        conn.execute("SELECT pg_sleep(0.2)")
        backend_pid = conn.info.backend_pid
        print(f"任务 {task_id:02d} 使用 PostgreSQL 连接 PID={backend_pid}")
        return backend_pid


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        raise SystemExit("请先设置 DATABASE_URL，示例见 database_pool/README.md。")
    else:
        print(f"使用数据库连接：{database_url}")

    # 一个应用进程创建一个池，业务函数复用它；不要每次查询都新建池。
    with ConnectionPool(
        conninfo=database_url,
        min_size=2,  # 池启动后准备 2 个连接。
        max_size=3,  # 同时最多占用 3 个连接，多余任务排队等待。
        timeout=5,  # 借用连接最多等待 5 秒，不是 SQL 执行超时。
        kwargs={"connect_timeout": 3, "application_name": "database_pool_demo"},
        open=False,  # 进入 with 时打开，退出 with 时关闭池及其连接。
    ) as pool:
        pool.wait(timeout=5)  # 等待初始连接就绪，配置错误时尽早失败。
        initialize_users(pool)
        demonstrate_rollback(pool)

        # 8 个并发任务竞争最多 3 个连接，输出中相同 PID 表示连接被复用。
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(run_task, pool, i) for i in range(1, 9)]
            backend_pids = {future.result() for future in futures}

        print(f"8 个任务共使用 {len(backend_pids)} 个数据库连接（上限为 3）。")
        print(f"连接池统计：{pool.get_stats()}")

    print("连接池已关闭，所有连接已释放。")


if __name__ == "__main__":
    try:
        main()
    except PoolTimeout as exc:
        raise SystemExit(
            "等待数据库连接超时，请检查数据库服务、连接配置或池占用。"
        ) from exc
