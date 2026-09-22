"""One-command local setup and foreground supervision; never manage other apps."""

import argparse
import fcntl
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = ROOT / "agent_platform"
MODULE = "agent_platform.scripts.local"


def say(message: str) -> None:
    print(message, flush=True)


def identity(pid: int) -> str:
    """PID plus exact start time and command protects against stale PID reuse."""
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "args="],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def managed_state(state_dir: Path) -> dict | None:
    try:
        state = json.loads((state_dir / "process.json").read_text())
        pid = state["pid"]
        if type(pid) is not int or pid <= 1:
            return None
        current = identity(pid)
        if current and current == state["identity"] and f"{MODULE} start" in current:
            return state
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def prepare_config(path: Path) -> bool:
    """Never rewrite existing configuration, including deliberately blank values."""
    path.parent.mkdir(parents=True, exist_ok=True)
    contents = (PLATFORM / ".env.example").read_text()
    contents = contents.replace(
        "PLATFORM_ADMIN_PASSWORD=\n",
        f"PLATFORM_ADMIN_PASSWORD={secrets.token_urlsafe(24)}\n",
        1,
    )
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as stream:
        stream.write(contents)
    return True


def check_port(host: str, port: int) -> None:
    if not 1 <= port <= 65535:
        raise RuntimeError("PORT 必须在 1 到 65535 之间。")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
    except OSError:
        raise RuntimeError(
            f"{host}:{port} 已被占用或无法绑定。请使用 make start PORT=8010 指定空闲端口；不会停止已有服务。"
        ) from None


def setup_database() -> None:
    """Adopt an unversioned create_all DB only after a complete schema match."""
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import inspect

    from agent_platform.infrastructure import tables  # noqa: F401
    from agent_platform.infrastructure.config import Settings
    from agent_platform.infrastructure.db import Database, metadata
    from agent_platform.modules.billing import service  # noqa: F401

    db = Database(Settings.from_env().database_url)
    config = Config(str(PLATFORM / "alembic.ini"))
    try:
        with db.read() as connection:
            inspector = inspect(connection)
            names = set(inspector.get_table_names())
            if "alembic_version" not in names and names.intersection(metadata.tables):
                keys_match = all(
                    name in names
                    and inspector.get_pk_constraint(name)["constrained_columns"]
                    == [column.name for column in table.primary_key.columns]
                    for name, table in metadata.tables.items()
                )
                differences = compare_metadata(
                    MigrationContext.configure(
                        connection,
                        opts={"compare_type": True, "compare_server_default": True},
                    ),
                    metadata,
                )
                if (
                    differences
                    or not keys_match
                    or ScriptDirectory.from_config(config).get_current_head() != "0001"
                ):
                    raise RuntimeError(
                        "发现未版本化且结构不匹配的旧数据库。请先备份并处理迁移差异；启动已停止，未补表或覆盖数据。"
                    )
                say("旧数据库与当前初版结构一致，建立迁移基线。")
                command.stamp(config, "0001")
        command.upgrade(config, "head")
    finally:
        db.close()


class Supervisor:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.children: list[subprocess.Popen] = []
        self.state: dict = {}

    def record(self, phase: str) -> None:
        self.state["phase"] = phase
        target = self.state_dir / "process.json"
        temporary = self.state_dir / "process.tmp"
        temporary.write_text(json.dumps(self.state))
        temporary.replace(target)

    def spawn(self, args: list[str], env: dict | None = None) -> subprocess.Popen:
        child = subprocess.Popen(args, cwd=ROOT, env=env, start_new_session=True)
        self.children.append(child)
        return child

    def run(self, args: list[str], env: dict | None = None) -> None:
        child = self.spawn(args, env)
        if child.wait():
            raise RuntimeError("准备步骤失败，已停止启动。请查看上方错误信息。")
        self.children.remove(child)

    def cleanup(self) -> None:
        # Each session/group was created here; never search/kill by port or name.
        for child in reversed(self.children):
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for child in self.children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        (self.state_dir / "process.json").unlink(missing_ok=True)

    def start(self, env_file: Path, host: str, port: int) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "process.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(
                    "本目录已有启动过程或服务。请先 make status 查看状态。"
                ) from None
            check_port(host, port)
            for executable in ("uv", "npm", "node"):
                if not shutil.which(executable):
                    raise RuntimeError(
                        f"缺少 {executable}，请先安装后运行 make start。"
                    )
            self.state = {
                "pid": os.getpid(),
                "identity": identity(os.getpid()),
                "url": f"http://{host}:{port}",
                "env_file": str(env_file),
            }
            try:
                self.record("准备依赖")
                say("准备 Python 和前端依赖…")
                self.run(["uv", "sync", "--frozen"])
                self.run(["npm", "--prefix", str(PLATFORM / "apps/web"), "ci"])
                created = prepare_config(env_file)
                if created:
                    say(
                        f"已创建 {env_file}（仅当前用户可读写），初始管理员密码在 PLATFORM_ADMIN_PASSWORD 中。"
                    )
                from dotenv import dotenv_values

                # Explicit file first; command-line environment overrides it.
                env = {
                    **os.environ,
                    **{
                        key: value
                        for key, value in dotenv_values(env_file).items()
                        if value is not None
                    },
                }
                env.update(
                    {
                        key: value
                        for key, value in os.environ.items()
                        if key.startswith("PLATFORM_")
                    }
                )
                self.record("构建界面")
                self.run(
                    ["npm", "--prefix", str(PLATFORM / "apps/web"), "run", "build"]
                )
                self.record("初始化数据库")
                self.run([sys.executable, "-m", MODULE, "setup-db"], env)
                self.run([sys.executable, "-m", "agent_platform", "init"], env)
                self.record("等待服务就绪")
                api = self.spawn(
                    [
                        sys.executable,
                        "-m",
                        "agent_platform",
                        "serve",
                        "--host",
                        host,
                        "--port",
                        str(port),
                    ],
                    env,
                )
                if env.get("PLATFORM_EMBEDDED_WORKER", "true").lower() != "true":
                    self.spawn([sys.executable, "-m", "agent_platform", "worker"], env)
                deadline = time.monotonic() + 45
                probe_host = "127.0.0.1" if host == "0.0.0.0" else host
                while time.monotonic() < deadline:
                    if any(child.poll() is not None for child in self.children):
                        raise RuntimeError("服务启动失败，相关进程已退出。")
                    try:
                        with urllib.request.urlopen(
                            f"http://{probe_host}:{port}/api/v1/health", timeout=1
                        ) as response:
                            if json.load(response).get("status") == "ok":
                                break
                    except (OSError, ValueError, urllib.error.URLError):
                        pass
                    time.sleep(0.2)
                else:
                    raise RuntimeError("等待服务就绪超时。")
                self.record("运行中")
                say(
                    f"\nAgent Platform 已启动：{self.state['url']}\n管理员邮箱：{env.get('PLATFORM_ADMIN_EMAIL', 'admin@example.com')}\n初始密码读取 PLATFORM_ADMIN_PASSWORD（环境变量优先于配置文件）；已有账号不会被重置。\n配置文件：{env_file}\n按 Ctrl+C 或另开终端执行 make stop 停止。\n"
                )
                if not (
                    env.get("PLATFORM_LITELLM_URL") and env.get("PLATFORM_LITELLM_KEY")
                ):
                    say(
                        "模型网关尚未配置：管理界面可用，接入真实模型请填写配置中的 LiteLLM 地址与受限密钥。"
                    )
                while api.poll() is None:
                    if any(child.poll() is not None for child in self.children):
                        raise RuntimeError("服务进程意外退出，正在停止其他关联进程。")
                    time.sleep(0.3)
                raise RuntimeError("API 服务已退出。")
            finally:
                self.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Platform local lifecycle")
    parser.add_argument("action", choices=["start", "stop", "status", "setup-db"])
    parser.add_argument("--env-file", type=Path, default=PLATFORM / ".env")
    parser.add_argument("--state-dir", type=Path, default=PLATFORM / ".data/local")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    os.chdir(ROOT)

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.action == "setup-db":
            setup_database()
        elif args.action == "start":
            Supervisor(args.state_dir.resolve()).start(
                args.env_file.resolve(), args.host, args.port
            )
        else:
            state = managed_state(args.state_dir)
            if not state:
                say("未发现本脚本管理的运行进程。")
                return 0
            if args.action == "status":
                say(f"{state['phase']}：{state['url']}（PID {state['pid']}）")
            else:
                os.kill(state["pid"], signal.SIGTERM)
                for _ in range(75):
                    if not managed_state(args.state_dir):
                        say("Agent Platform 已停止，配置和数据已保留。")
                        return 0
                    time.sleep(0.2)
                raise RuntimeError("停止仍在进行，请检查启动终端；不会终止其他进程。")
        return 0
    except KeyboardInterrupt:
        say("Agent Platform 已停止，配置和数据已保留。")
        return 0
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
