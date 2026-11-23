import asyncio

from fastmcp.server.auth import StaticTokenVerifier

from fastmcp_minimal_demo.server import serve
from fastmcp_minimal_demo.settings import TOKEN_ENV, load_token


def main() -> None:
    token = load_token(TOKEN_ENV)
    auth = StaticTokenVerifier(
        tokens={token: {"client_id": "local-agent", "scopes": []}}
    )
    asyncio.run(serve(port=8031, auth=auth))


if __name__ == "__main__":
    main()
