"""Execute a generated mapping through a trusted HTTP client without retries."""

import json
import logging
from dataclasses import dataclass, field
from time import monotonic

import httpx
import httpx2
from jsonschema import Draft202012Validator

from api_tools.compiler import canonical
from api_tools.registry import ToolRegistry, permitted

logger = logging.getLogger(__name__)
RESPONSE_HEADERS = {"etag", "location", "retry-after"}


class ToolError(Exception):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass(frozen=True)
class ExecutionContext:
    """Application-authenticated context; never populated from model arguments."""

    permissions: frozenset[str] = frozenset()
    authenticated: bool = False
    authorization: str | None = field(default=None, repr=False)
    request_id: str = ""


class ToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def call(
        self,
        name: str,
        arguments: dict,
        *,
        revision: str,
        context: ExecutionContext,
        client: httpx.AsyncClient | httpx2.AsyncClient,
    ) -> dict:
        snapshot, requests = self.registry.execution_snapshot()
        tool = next(
            (item for item in snapshot["tools"] if item["definition"]["name"] == name),
            None,
        )
        if tool is None:
            raise ToolError(
                "TOOL_UNAVAILABLE", "工具不存在或已下线，请刷新工具目录", 404
            )
        if revision != snapshot["bundleDigest"]:
            raise ToolError("TOOL_VERSION_MISMATCH", "工具版本已变化，请刷新工具目录")
        definition, binding = tool["definition"], tool["binding"]
        if not permitted(
            binding["policy"],
            set(context.permissions),
            authenticated=context.authenticated,
        ):
            raise ToolError("PERMISSION_DENIED", "没有执行该工具的权限", 403)
        try:
            canonical(arguments)
            valid = Draft202012Validator(definition["inputSchema"]).is_valid(arguments)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            # JSON Schema exception messages can include passwords or arbitrary input.
            raise ToolError("TOOL_INPUT_INVALID", "工具参数不符合当前输入结构", 422)
        try:
            request = requests.build(binding["operationId"], arguments)
        except (ValueError, TypeError, httpx2.InvalidURL, httpx2.LocalProtocolError):
            raise ToolError(
                "TOOL_INPUT_INVALID", "工具参数无法转换为 HTTP 请求", 422
            ) from None
        # The caller owns the destination and session. Only the encoded path and
        # query, API-declared headers and body come from FastMCP.
        headers = dict(request.headers)
        headers.pop("host", None)
        if context.authorization:
            headers["Authorization"] = context.authorization
        started = monotonic()
        try:
            response = await client.request(
                request.method,
                request.url.copy_with(query=None).raw_path.decode("ascii"),
                params=request.url.params.multi_items(),
                headers=headers,
                content=request.content,
                follow_redirects=False,
            )
        except (httpx.TimeoutException, httpx2.TimeoutException):
            raise ToolError(
                "TOOL_RESULT_UNKNOWN", "调用超时，执行结果未知，请先查询确认", 504
            ) from None
        except (httpx.HTTPError, httpx2.HTTPError):
            raise ToolError(
                "TOOL_TRANSPORT_ERROR", "无法完成工具请求，请凭请求标识排查", 502
            ) from None
        try:
            body = response.json() if response.content else None
            schema = binding["responses"].get(str(response.status_code))
            if schema is None or not Draft202012Validator(schema).is_valid(body):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            raise ToolError(
                "TOOL_OUTPUT_INVALID",
                "接口返回不符合工具契约，操作可能已执行，请先查询确认",
                502,
            ) from None
        request_id = response.headers.get("X-Request-ID", context.request_id)
        logger.info(
            "Tool completed name=%s operation=%s revision=%s request_id=%s http_status=%s duration_ms=%s",
            name,
            binding["operationId"],
            revision,
            request_id,
            response.status_code,
            round((monotonic() - started) * 1000),
        )
        return {
            "ok": response.is_success,
            "httpStatus": response.status_code,
            "body": body,
            "headers": {
                key: value
                for key, value in response.headers.items()
                if key in RESPONSE_HEADERS
            },
            "requestId": request_id,
            "bundleDigest": revision,
        }
