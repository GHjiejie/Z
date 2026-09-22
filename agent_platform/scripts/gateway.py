"""Private, persistent LiteLLM infrastructure for the local platform supervisor."""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "agent_platform/deploy/compose.yml"
SERVICES = ("postgres", "redis", "litellm")
CONTROL_SECRETS = (
    "POSTGRES_PASSWORD",
    "PLATFORM_DB_PASSWORD",
    "LITELLM_DB_PASSWORD",
    "LITELLM_MASTER_KEY",
    "LITELLM_SALT_KEY",
    "UI_USERNAME",
    "UI_PASSWORD",
)


def platform_environment(env: dict[str, str]) -> dict[str, str]:
    """API and Worker receive the runtime key, never gateway administration keys."""
    excluded = {
        *CONTROL_SECRETS,
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "UPSTREAM_API_KEY",
        "UPSTREAM_API_BASE",
        "UPSTREAM_MODEL",
    }
    return {key: value for key, value in env.items() if key not in excluded}


class LocalGateway:
    def __init__(self, state_dir: Path, env: dict[str, str], port: int):
        if not 1 <= port <= 65535:
            raise RuntimeError("LITELLM_PORT 必须在 1 到 65535 之间。")
        self.directory = state_dir.resolve() / "gateway"
        self.control_file = self.directory / ".env.control"
        self.key_file = self.directory / ".env.gateway"
        self.project = (
            "agent-platform-local-"
            + hashlib.sha256(str(state_dir.resolve()).encode()).hexdigest()[:12]
        )
        self.env = env
        self.port = port
        self.started = False
        self.compose_env: dict[str, str] = {}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def command(self) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--env-file",
            str(self.control_file),
            "-f",
            str(COMPOSE_FILE),
        ]

    def prepare(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.control_file.exists():
            controls = {
                name: self.env.get(name) or secrets.token_hex(24)
                for name in CONTROL_SECRETS
            }
            controls["LITELLM_MASTER_KEY"] = self.env.get(
                "LITELLM_MASTER_KEY"
            ) or "sk-" + secrets.token_hex(32)
            controls["UI_USERNAME"] = self.env.get("UI_USERNAME") or "admin"
            # Quote dotenv strings; credentials are configuration, never shell code.
            lines = []
            for name, value in controls.items():
                escaped = value.replace("\\", "\\\\").replace("'", "\\'")
                lines.append(f"{name}='{escaped}'\n")
            fd = os.open(self.control_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as target:
                target.writelines(lines)
        controls = {
            key: value
            for key, value in dotenv_values(self.control_file).items()
            if value is not None
        }
        if any(not controls.get(name) for name in CONTROL_SECRETS):
            raise RuntimeError(
                "本地网关控制配置不完整，请检查 .env.control；不会覆盖已有数据库凭据。"
            )
        if controls["UI_PASSWORD"] == controls["LITELLM_MASTER_KEY"]:
            raise RuntimeError("LiteLLM 管理页密码必须独立于网关 master key。")
        self.control_file.chmod(0o600)
        alias = (
            self.env.get("PLATFORM_DEFAULT_MODEL")
            or self.env.get("MODEL")
            or "platform-chat"
        )
        explicit_upstream = bool(self.env.get("UPSTREAM_API_KEY"))
        upstream_model = (
            self.env.get("UPSTREAM_MODEL", "")
            if explicit_upstream
            else "openai/" + (self.env.get("MODEL") or alias)
        )
        self.compose_env = {
            **os.environ,
            **self.env,
            **controls,
            "COMPOSE_ANSI": "never",
            "COMPOSE_PROGRESS": "plain",
            "GATEWAY_BOOTSTRAP_DIR": str(self.directory),
            "LITELLM_HTTP_PORT": str(self.port),
            "LITELLM_MODEL_ALIAS": alias,
            "UPSTREAM_MODEL": upstream_model,
            "UPSTREAM_API_BASE": (
                self.env.get("UPSTREAM_API_BASE", "")
                if explicit_upstream
                else self.env.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
            ),
            "UPSTREAM_API_KEY": (
                self.env.get("UPSTREAM_API_KEY", "")
                if explicit_upstream
                else self.env.get("OPENAI_API_KEY", "")
            ),
        }

    def start(
        self,
        run: Callable[[list[str], dict[str, str]], None],
        check_port: Callable[[str, int], None],
    ) -> dict[str, str]:
        if not shutil.which("docker"):
            raise RuntimeError(
                "启动 LiteLLM 管理页需要 Docker。请启动 Docker 后重试；已有外部网关可使用 GATEWAY=external。"
            )
        self.prepare()
        available = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if available.returncode:
            raise RuntimeError("Docker 尚未就绪，请启动 Docker 后再次运行 make start。")
        owned = subprocess.run(
            [*self.command, "ps", "--quiet", "litellm"],
            env=self.compose_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if owned.returncode:
            raise RuntimeError("无法读取本地网关容器状态，请检查 Docker Compose 配置。")
        same_port = False
        if owned.stdout.strip():
            ports = subprocess.run(
                [*self.command, "port", "litellm", "4000"],
                env=self.compose_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            same_port = ports.stdout.strip() == f"127.0.0.1:{self.port}"
        if not same_port:
            try:
                check_port("127.0.0.1", self.port)
            except RuntimeError:
                raise RuntimeError(
                    f"LiteLLM 端口 {self.port} 已被占用，请用 LITELLM_PORT=4001 指定其他端口。"
                ) from None
        # Only this deterministic Compose project is managed, including cleanup
        # after a partial start. Persistent database volumes are never removed.
        self.started = True
        run(
            [
                *self.command,
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "240",
                *SERVICES,
            ],
            self.compose_env,
        )
        run(
            [
                *self.command,
                "exec",
                "-T",
                "-e",
                f"GATEWAY_KEY_UID={os.getuid()}",
                "-e",
                f"GATEWAY_KEY_GID={os.getgid()}",
                "litellm",
                "python",
                "/app/platform-bootstrap-key.py",
                "--reuse-existing",
            ],
            self.compose_env,
        )
        runtime_key = dotenv_values(self.key_file).get("PLATFORM_LITELLM_KEY")
        if not runtime_key or runtime_key == self.compose_env["LITELLM_MASTER_KEY"]:
            raise RuntimeError("未生成有效的 LiteLLM 受限调用密钥。")
        self.key_file.chmod(0o600)
        return {
            **platform_environment(self.env),
            "PLATFORM_LITELLM_URL": self.url + "/v1",
            "PLATFORM_LITELLM_KEY": runtime_key,
            "PLATFORM_LITELLM_ADMIN_URL": self.url + "/ui",
            "PLATFORM_DEFAULT_MODEL": self.compose_env["LITELLM_MODEL_ALIAS"],
        }

    def stop(self) -> None:
        if self.started:
            try:
                result = subprocess.run(
                    [*self.command, "stop", "--timeout", "10", *SERVICES],
                    env=self.compose_env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=45,
                )
                stopped = result.returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                stopped = False
            if not stopped:
                print(
                    "本地网关未能完全停止，请检查 Docker；数据库卷已保留。", flush=True
                )
            self.started = False
