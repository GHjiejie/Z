import os
from pathlib import Path

from dotenv import dotenv_values

TOKEN_ENV = "FASTMCP_DEMO_MCP_TOKEN"
DELETE_TOKEN_ENV = "FASTMCP_DEMO_DELETE_TOKEN"
BACKEND_DELETE_TOKEN_ENV = "FASTMCP_DEMO_BACKEND_DELETE_TOKEN"


def configured_token(env_key: str) -> str:
    config = dotenv_values(Path(__file__).with_name(".env"), interpolate=False)
    return (os.environ.get(env_key, config.get(env_key)) or "").strip()


def load_token(env_key: str) -> str:
    token = configured_token(env_key)
    if not token:
        raise SystemExit(f"请在 fastmcp_minimal_demo/.env 或环境变量中设置 {env_key}。")
    return token
