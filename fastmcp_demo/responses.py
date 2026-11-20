"""Use the configured Responses API model to discover and call MCP tools."""

import json
import logging
import os
from pathlib import Path
from typing import Literal, cast

from dotenv import dotenv_values
from fastmcp import Client
from openai import AsyncOpenAI, omit
from openai.types.responses import (
    EasyInputMessageParam,
    FunctionToolParam,
    Response,
    ResponseFunctionToolCall,
    ResponseInputItemParam,
    ResponseInputParam,
)
from pydantic import BaseModel, ConfigDict, Field

ENV_FILE = Path(__file__).with_name(".env")
DEFAULT_MCP_URL = "http://127.0.0.1:8020/mcp"
MAX_TOOL_ROUNDS = 5
INSTRUCTIONS = (
    "你是迭代任务助手。回答迭代任务相关问题前，先调用提供的工具查询。"
    "根据工具结果用中文简洁回答。"
)

logger = logging.getLogger(__name__)


class ResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: str | list[EasyInputMessageParam] = Field(min_length=1)
    model: str | None = Field(default=None, min_length=1)
    instructions: str | None = None
    max_output_tokens: int | None = Field(default=None, ge=16)
    store: Literal[False] = False
    stream: Literal[False] = False


def create_openai_client() -> tuple[AsyncOpenAI, str]:
    """Use process variables first, followed by this demo's .env file."""
    values = dotenv_values(ENV_FILE, interpolate=False) if ENV_FILE.is_file() else {}

    def required(suffix: str) -> str:
        key = f"AGENT_USERS_LLM_{suffix}"
        value = (os.environ.get(key, values.get(key)) or "").strip()
        if not value:
            raise ValueError(f"请在 fastmcp_demo/.env 或进程环境中设置 {key}。")
        return value

    model = required("MODEL")
    client = AsyncOpenAI(
        base_url=required("BASE_URL"),
        api_key=required("API_KEY"),
        timeout=60.0,
        max_retries=0,
    )
    return client, model


async def run_tool(
    client: Client, call: ResponseFunctionToolCall, allowed_names: set[str]
) -> str:
    if call.name not in allowed_names:
        return json.dumps({"error": f"未知工具 {call.name}。"}, ensure_ascii=False)

    try:
        arguments = json.loads(call.arguments)
    except json.JSONDecodeError:
        arguments = None
    if not isinstance(arguments, dict):
        return json.dumps({"error": "工具参数必须是 JSON 对象。"}, ensure_ascii=False)

    result = await client.call_tool(call.name, arguments, raise_on_error=False)
    logger.info(
        "MCP tools/call %s · %s", call.name, "失败" if result.is_error else "成功"
    )
    payload = result.structured_content
    if payload is None or result.is_error:
        payload = {
            "is_error": result.is_error,
            "content": [item.model_dump(mode="json") for item in result.content],
        }
    return json.dumps(payload, ensure_ascii=False)


async def generate_response(
    request: ResponseRequest, *, mcp_url: str | None = None
) -> Response:
    """Run the model's function calls through MCP and return its final Response."""
    model_client, default_model = create_openai_client()
    endpoint = mcp_url or os.environ.get("FASTMCP_DEMO_MCP_URL", DEFAULT_MCP_URL)
    input_items: ResponseInputParam = (
        [{"role": "user", "content": request.input}]
        if isinstance(request.input, str)
        else list(request.input)
    )

    async with model_client, Client(endpoint, timeout=30.0) as mcp_client:
        discovered_tools = await mcp_client.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP 服务未提供工具。")

        tools: list[FunctionToolParam] = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description or tool.name,
                "parameters": tool.inputSchema,
                "strict": False,
            }
            for tool in discovered_tools
        ]
        allowed_names = {tool.name for tool in discovered_tools}
        logger.info(
            "MCP tools/list · %s", ", ".join(tool.name for tool in discovered_tools)
        )

        for round_number in range(MAX_TOOL_ROUNDS + 1):
            response = await model_client.responses.create(
                model=request.model or default_model,
                input=input_items,
                instructions=request.instructions or INSTRUCTIONS,
                max_output_tokens=request.max_output_tokens or omit,
                store=request.store,
                tools=tools,
            )
            calls = [item for item in response.output if item.type == "function_call"]
            if not calls or response.status in {"incomplete", "failed", "cancelled"}:
                return response
            if round_number == MAX_TOOL_ROUNDS:
                break

            input_items.extend(
                cast(
                    ResponseInputItemParam,
                    item.model_dump(mode="json", exclude_none=True),
                )
                for item in response.output
            )
            for call in calls:
                output = await run_tool(mcp_client, call, allowed_names)
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": output,
                    }
                )

    raise RuntimeError("模型工具调用达到轮数上限。")
