"""Load the backend's OpenAPI document and serve its operations over MCP."""

import argparse
import asyncio

import httpx
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import MCPType, RouteMap


async def serve(api_url: str, host: str, port: int) -> None:
    async with httpx.AsyncClient(base_url=api_url, timeout=10.0) as api_client:
        response = await api_client.get("/openapi.json")
        response.raise_for_status()
        openapi_spec = response.json()
        if not isinstance(openapi_spec, dict) or not isinstance(
            openapi_spec.get("paths"), dict
        ):
            raise TypeError("FastAPI 的 /openapi.json 未返回有效的 OpenAPI 规格。")

        mcp = FastMCP.from_openapi(
            openapi_spec=openapi_spec,
            client=api_client,
            name="Sprint Tasks MCP",
            route_maps=[
                RouteMap(
                    methods=["POST"],
                    pattern=r"^/v1/responses$",
                    mcp_type=MCPType.EXCLUDE,
                ),
            ],
        )
        await mcp.run_async(
            transport="http", host=host, port=port, path="/mcp", show_banner=False
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="从 FastAPI OpenAPI 生成 MCP 服务")
    parser.add_argument(
        "--api-url", default="http://127.0.0.1:8022", help="FastAPI 地址"
    )
    parser.add_argument("--host", default="127.0.0.1", help="MCP 监听地址")
    parser.add_argument("--port", type=int, default=8020, help="MCP 监听端口")
    args = parser.parse_args()

    try:
        asyncio.run(serve(args.api_url, args.host, args.port))
    except (httpx.HTTPError, TypeError, ValueError) as error:
        parser.exit(1, f"MCP 服务启动失败。{error}\n")


if __name__ == "__main__":
    main()
