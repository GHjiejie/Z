"""Expose calculator tool groups individually or through one MCP gateway."""

import argparse
import asyncio
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import MCPType, RouteMap

SERVICE_PORTS = {"gateway": 8034, "basic": 8035, "advanced": 8036}
GROUP_PATTERNS = {
    "basic": r"^/(add|subtract)$",
    "advanced": r"^/(multiply|divide)$",
}


def build_group(
    group: str, openapi_spec: dict[str, Any], api_client: httpx.AsyncClient
) -> FastMCP:
    return FastMCP.from_openapi(
        openapi_spec=openapi_spec,
        client=api_client,
        name=f"Calculator {group} MCP",
        route_maps=[
            RouteMap(
                methods=["GET"], pattern=GROUP_PATTERNS[group], mcp_type=MCPType.TOOL
            ),
            RouteMap(mcp_type=MCPType.EXCLUDE),
        ],
    )


async def serve(service: str) -> None:
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8032") as api_client:
        response = await api_client.get("/openapi.json")
        response.raise_for_status()
        openapi_spec = response.json()

        if service == "gateway":
            mcp = FastMCP("Calculator MCP Gateway")
            for group in GROUP_PATTERNS:
                child = build_group(group, openapi_spec, api_client)
                mcp.mount(child, namespace=group)
        else:
            mcp = build_group(service, openapi_spec, api_client)

        await mcp.run_async(
            transport="http", host="127.0.0.1", port=SERVICE_PORTS[service], path="/mcp"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="运行计算 MCP 子服务或聚合入口")
    parser.add_argument("service", nargs="?", choices=SERVICE_PORTS, default="gateway")
    asyncio.run(serve(parser.parse_args().service))


if __name__ == "__main__":
    main()
