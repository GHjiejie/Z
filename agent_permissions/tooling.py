"""Same-origin discovery and execution; existing API authentication remains final."""

import json
import logging
from pathlib import Path
from typing import Any

import httpx2
from fastapi import FastAPI, Request
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from agent_permissions import audit
from agent_permissions.audit_http import before_dispatch
from agent_permissions.errors import ApiError
from agent_permissions.hitl_execution import (
    TRANSACTIONAL_OPERATIONS,
    confirmed_execution,
)
from agent_permissions.hitl_forms import requires_confirmation
from agent_permissions.models import StrictModel
from agent_permissions.security import token_digest
from agent_permissions.service import Authority
from api_tools.compiler import compile_app, compile_catalog
from api_tools.executor import ExecutionContext, ToolError, ToolExecutor
from api_tools.registry import ToolRegistry, permitted

TOOL_NAMESPACE = "agent_users"
EXCLUSIONS = {
    "/openapi.json": "OpenAPI document transport",
    "/docs": "Swagger UI",
    "/docs/oauth2-redirect": "Swagger authentication callback",
    "/redoc": "ReDoc UI",
    "/": "Console redirect",
    "/console": "Console redirect",
    "/console/": "Console HTML",
    "/console/assets/{path}": "Console static assets",
    "/console/assets": "Console static assets",
}
CONTROL_METADATA = {
    "x-agent": {"expose": False, "reason": "Tool control plane is not a business tool"}
}
logger = logging.getLogger(__name__)


class ToolCall(StrictModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    arguments: dict[str, Any] = Field(default_factory=dict)
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


def compile_tools(app: FastAPI) -> dict:
    return compile_app(app, namespace=TOOL_NAMESPACE, exclusions=EXCLUSIONS)


def activate_tools(app: FastAPI, *, manifest_path: Path | None = None) -> None:
    expected, requests = compile_catalog(
        app, namespace=TOOL_NAMESPACE, exclusions=EXCLUSIONS
    )
    bundle = json.loads(manifest_path.read_text()) if manifest_path else expected
    registry = ToolRegistry(bundle, expected=expected, requests=requests)
    app.state.tool_registry = registry
    app.state.tool_executor = ToolExecutor(registry)


async def _execution_access(
    request: Request,
) -> tuple[ExecutionContext, frozenset[str]]:
    authorization = request.headers.get("Authorization")
    granted = frozenset()
    operations = frozenset()
    if authorization is not None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 1024:
            raise ApiError(401, "UNAUTHENTICATED", "请重新登录")
        granted, operations = await run_in_threadpool(
            request.app.state.service.tool_access,
            Authority(token_digest(token)),
            request.state.info,
        )
    return ExecutionContext(
        permissions=granted,
        authenticated=authorization is not None,
        authorization=authorization,
        request_id=request.state.info.request_id,
    ), operations


async def execution_context(request: Request) -> ExecutionContext:
    context, _ = await _execution_access(request)
    return context


def mount_agent_tools(app: FastAPI) -> None:
    @app.get("/agent-tools", name="tools.list", openapi_extra=CONTROL_METADATA)
    async def list_tools(request: Request) -> dict:
        context, operations = await _execution_access(request)
        snapshot = app.state.tool_registry.snapshot()
        definitions = [
            item["definition"]
            for item in snapshot["tools"]
            if (
                item["binding"]["policy"]["authentication"] == "none"
                or item["binding"]["operationId"] in operations
            )
            and permitted(
                item["binding"]["policy"],
                set(context.permissions),
                authenticated=context.authenticated,
            )
        ]
        return {
            "bundleDigest": snapshot["bundleDigest"],
            "sourceRevision": snapshot["sourceRevision"],
            "tools": definitions,
            "cacheMaxAgeSeconds": 0,
        }

    @app.post("/agent-tools/call", name="tools.call", openapi_extra=CONTROL_METADATA)
    async def call_tool(payload: ToolCall, request: Request) -> dict:
        snapshot = app.state.tool_registry.snapshot()
        tool = next(
            (
                item
                for item in snapshot["tools"]
                if item["definition"]["name"] == payload.name
            ),
            None,
        )
        call = audit.active.get()
        call.bind(tool, snapshot["bundleDigest"])
        call.requested_target(payload.arguments)
        context, operations = await _execution_access(request)
        address = request.client
        transport = httpx2.ASGITransport(
            app=app, client=(address.host, address.port) if address else ("unknown", 0)
        )
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://agent-tools.internal",
            trust_env=False,
            event_hooks={"request": [before_dispatch]},
        ) as client:
            try:
                if tool is None:
                    raise ToolError(
                        "TOOL_UNAVAILABLE", "工具不存在或已下线，请刷新工具目录", 404
                    )
                if payload.revision != snapshot["bundleDigest"]:
                    raise ToolError(
                        "TOOL_VERSION_MISMATCH", "工具版本已变化，请刷新工具目录"
                    )
                binding = tool["binding"]
                audit.tool_checks(call, tool, context, operations)
                if (
                    binding["policy"]["authentication"] == "session"
                    and context.authenticated
                    and binding["operationId"] not in operations
                ):
                    raise ToolError(
                        "TOOL_NOT_GRANTED", "当前用户未允许 Agent 调用此工具", 403
                    )
                if not permitted(
                    binding["policy"],
                    set(context.permissions),
                    authenticated=context.authenticated,
                ):
                    raise ToolError("PERMISSION_DENIED", "没有执行该工具的权限", 403)
                if (
                    binding["policy"]["effect"] != "read"
                    and binding["operationId"] != "auth.login"
                    and binding["operationId"] not in TRANSACTIONAL_OPERATIONS
                ):
                    raise ToolError(
                        "TOOL_RECEIPT_REQUIRED", "此写工具尚未接入事务执行记录", 409
                    )
                if binding["policy"][
                    "authentication"
                ] == "session" and requires_confirmation(tool):
                    confirmation = confirmed_execution.get()
                    if (
                        confirmation is None
                        or confirmation.user_id != request.state.info.actor_user_id
                        or confirmation.operation_id != binding["operationId"]
                        or confirmation.arguments_digest
                        != app.state.run_store.digest(payload.arguments)
                    ):
                        raise ToolError(
                            "INTERACTION_REQUIRED",
                            "此操作需要在会话页面填写参数并确认",
                            409,
                        )
                await call.emit("requested", fact="requested", permission_checks=[])
                await call.emit("permission.checked")
                result = await app.state.tool_executor.call(
                    payload.name,
                    payload.arguments,
                    revision=payload.revision,
                    context=context,
                    client=client,
                )
                if (
                    result.get("httpStatus") == 503
                    and isinstance(result.get("body"), dict)
                    and result["body"].get("error", {}).get("code")
                    == "AUDIT_UNAVAILABLE"
                ):
                    raise ApiError(503, "AUDIT_UNAVAILABLE", "审计记录暂时不可用")
                return result
            except ToolError as error:
                await run_in_threadpool(
                    request.app.state.service.record_failure,
                    request.state.info,
                    "agent.tool.call",
                    request.state.info.actor_user_id,
                    error.code,
                    operation_id=tool["binding"]["operationId"] if tool else None,
                )
                logger.warning(
                    "Tool rejected name=%s revision=%s request_id=%s reason=%s",
                    tool["definition"]["name"] if tool else "unknown",
                    snapshot["bundleDigest"],
                    context.request_id,
                    error.code,
                )
                raise ApiError(error.status, error.code, error.message) from None
