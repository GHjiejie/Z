import asyncio
import os
from pathlib import Path

from dotenv import dotenv_values
from fastmcp.server.auth import StaticTokenVerifier

from fastmcp_minimal_demo.server import serve

TOKEN_ENV = "FASTMCP_DEMO_MCP_TOKEN"


def main() -> None:
    config = dotenv_values(Path(__file__).with_name(".env"), interpolate=False)
    token = (os.environ.get(TOKEN_ENV, config.get(TOKEN_ENV)) or "").strip()
    if not token:
        raise SystemExit(
            f"请在 fastmcp_minimal_demo/.env 或环境变量中设置 {TOKEN_ENV}。"
        )

    auth = StaticTokenVerifier(
        tokens={token: {"client_id": "local-agent", "scopes": []}}
    )
    asyncio.run(serve(port=8031, auth=auth))


if __name__ == "__main__":
    main()
