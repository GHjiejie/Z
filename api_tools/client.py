"""HTTP discovery and LangChain tools sharing the same generated input schemas."""

from collections.abc import Callable

import httpx
from langchain_core.tools import StructuredTool


class AgentToolClient:
    """One instance per trusted user context; call refresh before each agent task."""

    def __init__(
        self, client: httpx.AsyncClient, *, token_provider: Callable[[], str | None]
    ) -> None:
        self._client = client
        self._token_provider = token_provider
        self._catalog: dict | None = None

    def _headers(self) -> dict[str, str]:
        token = self._token_provider()
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def refresh(self) -> dict:
        response = await self._client.get(
            "/agent-tools", headers=self._headers(), follow_redirects=False
        )
        response.raise_for_status()
        self._catalog = response.json()
        return self._catalog

    async def call(
        self, name: str, arguments: dict, *, revision: str | None = None
    ) -> dict:
        if self._catalog is None:
            await self.refresh()
        response = await self._client.post(
            "/agent-tools/call",
            headers=self._headers(),
            json={
                "name": name,
                "arguments": arguments,
                "revision": revision or self._catalog["bundleDigest"],
            },
            follow_redirects=False,
        )
        return response.json()

    async def langchain_tools(self) -> list[StructuredTool]:
        """Refresh discovery; bind each callable to the exact advertised revision."""
        catalog = await self.refresh()
        revision = catalog["bundleDigest"]

        def adapt(definition: dict) -> StructuredTool:
            async def invoke(**arguments: object) -> dict:
                return await self.call(definition["name"], arguments, revision=revision)

            return StructuredTool(
                name=definition["name"],
                description=definition["description"],
                args_schema=definition["inputSchema"],
                coroutine=invoke,
            )

        return [adapt(definition) for definition in catalog["tools"]]
