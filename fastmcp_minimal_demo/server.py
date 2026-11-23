import asyncio

import httpx
from fastmcp import FastMCP


async def main() -> None:
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8032") as api_client:
        response = await api_client.get("/openapi.json")
        response.raise_for_status()
        mcp = FastMCP.from_openapi(
            openapi_spec=response.json(),
            client=api_client,
            name="Minimal MCP Demo",
        )
        await mcp.run_async(transport="http", host="127.0.0.1", port=8030, path="/mcp")


if __name__ == "__main__":
    asyncio.run(main())
