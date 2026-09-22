"""Test pinned LiteLLM + PostgreSQL + Redis with a local, free provider fixture.

Run from the repository root with uv. Only a unique throwaway Compose project
and its own volumes are created and removed. No real provider credentials are
read, no existing deployment is touched, and secrets are not printed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import uuid
from decimal import Decimal
from pathlib import Path

import httpx

from agent_platform.modules.runtime.engine import execute_agent
from agent_platform.modules.runtime.gateway import Gateway, GatewayError

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "agent_platform/deploy"


def command(args: list[str], **kwargs) -> str:
    if args[:2] == ["docker", "compose"]:
        # Shell variables override Compose --env-file values. Exclude any real
        # deployment credentials and force this run's generated test settings.
        process_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(
                (
                    "PLATFORM_",
                    "LITELLM_",
                    "UPSTREAM_",
                    "GATEWAY_",
                    "POSTGRES_",
                    "COMPOSE_",
                    "UI_",
                )
            )
        }
        env_file = Path(args[args.index("--env-file") + 1])
        process_env.update(
            line.split("=", 1) for line in env_file.read_text().splitlines()
        )
        kwargs["env"] = process_env
    result = subprocess.run(args, capture_output=True, text=True, check=False, **kwargs)
    if result.returncode:
        # Do not include env-sensitive compose configuration or provider logs.
        details = (
            re.sub(r"://[^/@]+:[^/@]+@", "://***@", result.stderr)
            if "alembic" in args
            else ""
        )
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(args[:3])}\n{details}"
        )
    return result.stdout.strip()


async def verify_gateway(base: str, key: str) -> None:
    gateway = Gateway(base + "/v1", key)
    events = [
        event
        async for event in gateway.stream(
            [{"role": "user", "content": "hello"}], "platform-chat", 64, 0, []
        )
    ]
    result = events[-1].data
    assert result.content == "Answer: 4"
    assert (result.input_tokens, result.output_tokens) == (12, 4)
    print("PASS: actual LiteLLM streaming text and complete usage", flush=True)
    estimated = [
        event
        async for event in gateway.stream(
            [{"role": "user", "content": "missing-usage-test"}],
            "platform-chat",
            64,
            0,
            [],
        )
    ][-1].data
    print(
        f"OBSERVED: provider omitted usage; LiteLLM returned {estimated.input_tokens}/{estimated.output_tokens} tokens",
        flush=True,
    )

    async def emit(_name: str, _data: dict) -> None:
        pass

    async def cancelled() -> bool:
        return False

    async def invoke(messages: list[dict], tools: list[dict]):
        result = None
        async for event in gateway.stream(messages, "platform-chat", 64, 0, tools):
            if event.type == "result":
                result = event.data
        assert result is not None
        return result

    answer = await execute_agent(
        {"max_steps": 3, "tools": ["calculator"]},
        [{"role": "user", "content": "calculate 2+2"}],
        invoke,
        emit,
        cancelled,
    )
    assert answer == "Answer: 4"
    print("PASS: actual LiteLLM tool fragments through native LangGraph v3", flush=True)

    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + key}) as client:
        denied = await client.post(
            base + "/key/generate", json={"models": ["platform-chat"]}
        )
        assert denied.status_code in {401, 403}, denied.status_code
        denied = await client.post(
            base + "/v1/chat/completions",
            json={
                "model": "forbidden-alias",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert denied.status_code in {401, 403}, denied.status_code
    print("PASS: runtime virtual key rejects management and unlisted model", flush=True)
    try:
        _ = [
            event
            async for event in gateway.stream(
                [{"role": "user", "content": "retry-test"}], "platform-chat", 64, 0, []
            )
        ]
    except GatewayError:
        pass
    else:
        raise AssertionError("Expected failed provider request")


def verify_platform(base: str, password: str) -> None:
    """Exercise public HTTP only; the independent container worker owns execution."""
    with httpx.Client(base_url=base, timeout=45) as client:
        assert client.get("/").status_code == 200
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": password},
        )
        login.raise_for_status()
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]

        def api(method: str, path: str, **kwargs):
            response = client.request(method, "/api/v1" + path, **kwargs)
            response.raise_for_status()
            return response.json()

        wallet = api("GET", "/billing/wallet")
        assert Decimal(wallet["balance"]) == 0, wallet
        model = api(
            "POST",
            "/models",
            json={
                "name": "Smoke model",
                "alias": "platform-chat",
                "input_price": "1",
                "output_price": "2",
                "context_window": 32768,
                "max_output_tokens": 256,
            },
        )
        agent = api(
            "POST",
            "/agents",
            json={
                "name": "Smoke Agent",
                "model_id": model["id"],
                "system_prompt": "Use the calculator to answer.",
                "max_tokens": 64,
                "max_steps": 3,
                "tools": ["calculator"],
            },
        )
        assert api("POST", f"/agents/{agent['id']}/publish")["version"] == 1
        api(
            "POST",
            "/billing/credits",
            headers={"Idempotency-Key": "smoke-credit"},
            json={"amount": "10", "description": "Container smoke credit"},
        )
        session = api("POST", "/sessions", json={"agent_id": agent["id"]})
        run = api(
            "POST",
            f"/sessions/{session['id']}/runs",
            headers={"Idempotency-Key": "smoke-run"},
            json={"message": "calculate 2+2"},
        )
        stream = client.get(f"/api/v1/runs/{run['id']}/events")
        stream.raise_for_status()
        assert "event: tool.finished" in stream.text, stream.text
        assert "event: run.completed" in stream.text, stream.text
        assert "Answer: 4" in stream.text, stream.text
        event_ids = [
            int(line[4:])
            for line in stream.text.splitlines()
            if line.startswith("id: ")
        ]
        assert event_ids == list(range(1, len(event_ids) + 1)), event_ids
        replay = client.get(
            f"/api/v1/runs/{run['id']}/events",
            headers={"Last-Event-ID": str(event_ids[-2])},
        )
        replay.raise_for_status()
        assert "event: run.completed" in replay.text
        assert "event: run.queued" not in replay.text
        saved = api("GET", f"/runs/{run['id']}")
        assert saved["status"] == "succeeded", saved
        assert Decimal(saved["cost"]) == Decimal("0.000040"), saved
        usage = api("GET", "/usage/calls")["items"]
        assert len(usage) == 2, usage
        assert all(
            item["status"] == "confirmed"
            and item["input_tokens"] == 12
            and item["output_tokens"] == 4
            and item["usage_source"] == "litellm_gateway"
            for item in usage
        ), usage
        wallet = api("GET", "/billing/wallet")
        assert Decimal(wallet["balance"]) == Decimal("9.999960"), wallet
        assert Decimal(wallet["reserved"]) == 0, wallet
        ledger = api("GET", "/billing/ledger")["items"]
        assert len(ledger) == 3, ledger
        detail = api("GET", f"/sessions/{session['id']}")
        assert [message["role"] for message in detail["messages"]] == [
            "user",
            "assistant",
        ], detail
        assert detail["messages"][-1]["content"] == "Answer: 4", detail
        dashboard = api("GET", "/dashboard")
        assert dashboard["requests"] == 2 and dashboard["tokens"] == 32, dashboard
        assert Decimal(dashboard["cost"]) == Decimal("0.000040"), dashboard
    print(
        "PASS: separate API/Worker containers login → publish → credit → run/tool/SSE → gateway usage → billing/history/dashboard",
        flush=True,
    )


def main() -> None:
    project = "agent-platform-smoke-" + uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix=project) as directory:
        root = Path(directory)
        bootstrap = root / "bootstrap"
        bootstrap.mkdir()
        passwords = {
            name: secrets.token_hex(24)
            for name in [
                "POSTGRES_PASSWORD",
                "PLATFORM_DB_PASSWORD",
                "LITELLM_DB_PASSWORD",
                "LITELLM_SALT_KEY",
                "PLATFORM_ADMIN_PASSWORD",
                "UI_PASSWORD",
            ]
        }
        env = {
            **passwords,
            "LITELLM_MASTER_KEY": "sk-" + secrets.token_hex(24),
            "UPSTREAM_MODEL": "openai/gpt-4o-mini",
            "UPSTREAM_API_BASE": "http://mock:8080/v1",
            "UPSTREAM_API_KEY": "smoke-only",
            "LITELLM_MODEL_ALIAS": "platform-chat",
            "PLATFORM_HTTP_PORT": "0",
            "LITELLM_HTTP_PORT": "0",
            "GATEWAY_BOOTSTRAP_DIR": str(bootstrap),
            "PLATFORM_SECURE_COOKIES": "false",
        }
        env_file = root / ".env"
        env_file.write_text("".join(f"{key}={value}\n" for key, value in env.items()))
        env_file.chmod(0o600)
        override = root / "smoke.json"
        override.write_text(
            json.dumps(
                {
                    "services": {
                        "api": {
                            "environment": {
                                "PLATFORM_LITELLM_KEY": "${SMOKE_RUNTIME_KEY:-}"
                            }
                        },
                        "worker": {
                            "environment": {
                                "PLATFORM_LITELLM_KEY": "${SMOKE_RUNTIME_KEY:-}"
                            }
                        },
                        "init": {
                            "environment": {
                                "PLATFORM_LITELLM_KEY": "${SMOKE_RUNTIME_KEY:-}"
                            }
                        },
                        "postgres": {"ports": ["127.0.0.1::5432"]},
                        "mock": {
                            "image": "python:3.13-slim-bookworm",
                            "command": ["python", "/mock.py"],
                            "volumes": [f"{DEPLOY / 'mock_provider.py'}:/mock.py:ro"],
                        },
                    }
                }
            )
        )
        compose = [
            "docker",
            "compose",
            "--project-name",
            project,
            "--env-file",
            str(env_file),
            "-f",
            str(DEPLOY / "compose.yml"),
            "-f",
            str(override),
        ]
        try:
            command([*compose, "config", "--quiet"])
            print(
                "Starting isolated PostgreSQL, Redis, pinned LiteLLM and local mock…",
                flush=True,
            )
            command(
                [
                    *compose,
                    "up",
                    "-d",
                    "--wait",
                    "--wait-timeout",
                    "240",
                    "postgres",
                    "redis",
                    "mock",
                    "litellm",
                ],
                timeout=300,
            )
            version = command(
                [
                    *compose,
                    "exec",
                    "-T",
                    "litellm",
                    "python",
                    "-c",
                    "import importlib.metadata; print(importlib.metadata.version('litellm'))",
                ]
            )
            assert version == "1.83.0", version
            print("PASS: pinned container runs LiteLLM 1.83.0", flush=True)
            command(
                [
                    *compose,
                    "exec",
                    "-T",
                    "-e",
                    f"GATEWAY_KEY_UID={os.getuid()}",
                    "-e",
                    f"GATEWAY_KEY_GID={os.getgid()}",
                    "litellm",
                    "python",
                    "/app/platform-bootstrap-key.py",
                ]
            )
            key = (bootstrap / ".env.gateway").read_text().strip().split("=", 1)[1]
            litellm_address = command([*compose, "port", "litellm", "4000"])
            asyncio.run(verify_gateway("http://" + litellm_address, key))
            counts = json.loads(
                command(
                    [
                        *compose,
                        "exec",
                        "-T",
                        "litellm",
                        "python",
                        "-c",
                        "import urllib.request; print(urllib.request.urlopen('http://mock:8080/stats').read().decode())",
                    ]
                )
            )
            assert counts["retry_test"] == 1, counts
            print(
                "PASS: provider 500 receives exactly one attempt (SDK and Proxy retries disabled)",
                flush=True,
            )

            address = command([*compose, "port", "postgres", "5432"])
            db_env = {
                **os.environ,
                "PLATFORM_DATABASE_URL": f"postgresql+psycopg://platform:{passwords['PLATFORM_DB_PASSWORD']}@{address}/platform",
            }
            for action in [
                ("upgrade", "head"),
                ("check",),
                ("downgrade", "base"),
                ("upgrade", "head"),
                ("check",),
            ]:
                command(
                    [
                        sys.executable,
                        "-m",
                        "alembic",
                        "-c",
                        str(ROOT / "agent_platform/alembic.ini"),
                        *action,
                    ],
                    env=db_env,
                    cwd=ROOT,
                )
            print(
                "PASS: PostgreSQL migration upgrade/check/downgrade/re-upgrade",
                flush=True,
            )
            print(
                "Building final application image from root uv.lock and React source…",
                flush=True,
            )
            command(
                [
                    "docker",
                    "build",
                    "-f",
                    str(DEPLOY / "Dockerfile"),
                    "-t",
                    "agent-platform:0.1.0",
                    str(ROOT),
                ],
                timeout=600,
            )
            env["SMOKE_RUNTIME_KEY"] = key
            env_file.write_text(
                "".join(f"{name}={value}\n" for name, value in env.items())
            )
            command(
                [
                    *compose,
                    "up",
                    "-d",
                    "--no-build",
                    "--wait",
                    "--wait-timeout",
                    "120",
                    "api",
                    "worker",
                ],
                timeout=180,
            )
            assert (
                command(
                    [
                        *compose,
                        "exec",
                        "-T",
                        "api",
                        "python",
                        "-c",
                        "import os; assert os.environ['PLATFORM_EMBEDDED_WORKER'] == 'false'; assert 'LITELLM_MASTER_KEY' not in os.environ; print('isolated')",
                    ]
                )
                == "isolated"
            )
            assert (
                command(
                    [
                        *compose,
                        "exec",
                        "-T",
                        "worker",
                        "python",
                        "-c",
                        "import os; assert 'LITELLM_MASTER_KEY' not in os.environ; print('isolated')",
                    ]
                )
                == "isolated"
            )
            api_address = command([*compose, "port", "api", "8000"])
            verify_platform(
                "http://" + api_address, passwords["PLATFORM_ADMIN_PASSWORD"]
            )
        finally:
            command([*compose, "down", "--volumes", "--remove-orphans"], timeout=120)


if __name__ == "__main__":
    main()
