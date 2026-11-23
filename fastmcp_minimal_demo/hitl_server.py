"""Require scoped authorization and confirmation before forwarding a DELETE."""

import asyncio
from secrets import token_urlsafe
from time import monotonic
from urllib.parse import quote

import httpx2
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import StaticTokenVerifier, require_scopes
from fastmcp.server.providers.openapi import MCPType, RouteMap
from mcp.server.request_state import RequestStateSecurity
from mcp.types import (
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
    ToolAnnotations,
)

from fastmcp_minimal_demo.settings import (
    BACKEND_DELETE_TOKEN_ENV,
    DELETE_TOKEN_ENV,
    TOKEN_ENV,
    load_token,
)

MCP_PORT = 8037
DELETE_SCOPE = "records:delete"
CONFIRMATION_TTL = 600


async def serve(
    *, api_url: str = "http://127.0.0.1:8032", port: int = MCP_PORT
) -> None:
    read_token = load_token(TOKEN_ENV)
    delete_token = load_token(DELETE_TOKEN_ENV)
    backend_token = load_token(BACKEND_DELETE_TOKEN_ENV)
    if len({read_token, delete_token, backend_token}) != 3:
        raise SystemExit("查询令牌、删除令牌和后端服务凭据需要不同的值。")

    auth = StaticTokenVerifier(
        tokens={
            read_token: {"client_id": "records-reader", "scopes": []},
            delete_token: {"client_id": "records-deleter", "scopes": [DELETE_SCOPE]},
        }
    )
    async with httpx2.AsyncClient(
        base_url=api_url, timeout=10.0, trust_env=False
    ) as api_client:
        response = await api_client.get("/openapi.json")
        response.raise_for_status()
        openapi_spec = response.json()
        paths = openapi_spec["paths"]
        for path in ("/records/{record_id}", "/records_user"):
            if "delete" not in paths.get(path, {}):
                raise RuntimeError(f"FastAPI 尚未发布 DELETE {path}。")

        mcp = FastMCP.from_openapi(
            openapi_spec=openapi_spec,
            client=api_client,
            name="Records HITL MCP",
            auth=auth,
            request_state_security=RequestStateSecurity.ephemeral(ttl=CONFIRMATION_TTL),
            route_maps=[
                RouteMap(methods=["GET"], mcp_type=MCPType.TOOL),
                RouteMap(mcp_type=MCPType.EXCLUDE),
            ],
        )
        pending_confirmations: dict[str, float] = {}

        async def confirm_delete(
            ctx: Context,
            request: httpx2.Request,
            *,
            message: str,
            target: dict[str, str],
        ) -> dict[str, str] | InputRequiredResult:
            now = monotonic()
            if ctx.input_responses is None:
                expired_states = [
                    state
                    for state, expiry in pending_confirmations.items()
                    if expiry <= now
                ]
                for state in expired_states:
                    del pending_confirmations[state]
                state = token_urlsafe()
                pending_confirmations[state] = now + CONFIRMATION_TTL
                return InputRequiredResult(
                    request_state=state,
                    input_requests={
                        "delete": ElicitRequest(
                            params=ElicitRequestFormParams(
                                message=message,
                                requested_schema={
                                    "type": "object",
                                    "properties": {
                                        "confirm": {
                                            "type": "boolean",
                                            "title": "确认删除",
                                        },
                                    },
                                    "required": ["confirm"],
                                },
                            )
                        ),
                    },
                )

            # FastMCP binds request state to the caller and original tool arguments.
            state = ctx.request_state
            if state is None or pending_confirmations.pop(state, 0) <= now:
                raise ToolError("确认已失效，请重新发起删除。")
            answer = ctx.input_responses.get("delete")
            if (
                not isinstance(answer, ElicitResult)
                or answer.action != "accept"
                or (answer.content or {}).get("confirm") is not True
            ):
                return {"status": "cancelled", **target}

            request.headers["Authorization"] = f"Bearer {backend_token}"
            response = await api_client.send(request)
            response.raise_for_status()
            return response.json()

        delete_annotations = ToolAnnotations(
            read_only_hint=False, destructive_hint=True
        )

        @mcp.tool(auth=require_scopes(DELETE_SCOPE), annotations=delete_annotations)
        async def delete_record(
            record_id: str, ctx: Context
        ) -> dict[str, str] | InputRequiredResult:
            """删除指定记录。执行前需要确认记录 ID。"""
            encoded_id = quote(record_id, safe="")
            if record_id in {".", ".."}:
                encoded_id = "%2E" * len(record_id)
            path = f"/records/{encoded_id}"
            return await confirm_delete(
                ctx,
                api_client.build_request("DELETE", path),
                message=f"确认删除记录 {record_id!r}？",
                target={"record_id": record_id},
            )

        @mcp.tool(auth=require_scopes(DELETE_SCOPE), annotations=delete_annotations)
        async def delete_record_user(
            user_id: str, ctx: Context
        ) -> dict[str, str] | InputRequiredResult:
            """按用户 ID 删除记录。执行前需要确认用户 ID。"""
            params = {"user_id": user_id}
            return await confirm_delete(
                ctx,
                api_client.build_request("DELETE", "/records_user", params=params),
                message=f"确认按用户 ID {user_id!r} 删除记录？",
                target=params,
            )

        await mcp.run_async(transport="http", host="127.0.0.1", port=port, path="/mcp")


if __name__ == "__main__":
    asyncio.run(serve())
