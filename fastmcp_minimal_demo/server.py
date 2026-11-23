import asyncio

import httpx2
from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.providers.openapi import MCPType, RouteMap


async def serve(*, port: int, auth: AuthProvider | None = None) -> None:
    async with httpx2.AsyncClient(base_url="http://127.0.0.1:8032") as api_client:
        response = await api_client.get("/openapi.json")
        response.raise_for_status()
        mcp = FastMCP.from_openapi(
            openapi_spec=response.json(),
            client=api_client,
            name="Minimal MCP Demo",
            auth=auth,
            route_maps=[RouteMap(methods=["DELETE"], mcp_type=MCPType.EXCLUDE)],
        )
        await mcp.run_async(transport="http", host="127.0.0.1", port=port, path="/mcp")


if __name__ == "__main__":
    asyncio.run(serve(port=8030))
