"""Run OpenAI Responses requests with the demo's generated MCP tools."""

import json
import os
from pathlib import Path
from typing import Literal, cast

from dotenv import dotenv_values
from fastapi import HTTPException
from fastmcp import Client, FastMCP
from openai import APIError, APIStatusError, APITimeoutError, AsyncOpenAI, omit
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
MAX_TOOL_ROUNDS = 5
DEFAULT_INSTRUCTIONS = (
    "你是迭代任务助手。回答迭代任务相关的问题前，先使用提供的工具查询。"
)


class ResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: str | list[EasyInputMessageParam] = Field(min_length=1)
    model: str | None = Field(default=None, min_length=1)
    instructions: str | None = None
    max_output_tokens: int | None = Field(default=None, ge=16)
    stream: Literal[False] = False


def create_openai_client() -> tuple[AsyncOpenAI, str]:
    """Load the demo's namespaced credentials when the Responses route is used."""
    values = dotenv_values(ENV_FILE, interpolate=False) if ENV_FILE.is_file() else {}

    def required(suffix: str) -> str:
        key = f"AGENT_USERS_LLM_{suffix}"
        value = (os.environ.get(key, values.get(key)) or "").strip()
        if not value:
            raise HTTPException(
                status_code=503,
                detail=f"请在 fastmcp_demo/.env 或进程环境中设置 {key}。",
            )
        return value

    model = required("MODEL")
    client = AsyncOpenAI(
        base_url=required("BASE_URL"),
        api_key=required("API_KEY"),
        timeout=60.0,
        max_retries=0,
    )
    return client, model


def tool_error(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


async def run_tool(
    client: Client, call: ResponseFunctionToolCall, allowed_names: set[str]
) -> str:
    if call.name not in allowed_names:
        return tool_error(f"未知工具 {call.name}。")

    try:
        arguments = json.loads(call.arguments)
    except (TypeError, json.JSONDecodeError):
        return tool_error("工具参数必须是 JSON 对象。")
    if not isinstance(arguments, dict):
        return tool_error("工具参数必须是 JSON 对象。")

    result = await client.call_tool(call.name, arguments, raise_on_error=False)
    payload = result.structured_content
    if payload is None or result.is_error:
        payload = {
            "is_error": result.is_error,
            "content": [item.model_dump(mode="json") for item in result.content],
        }
    return json.dumps(payload, ensure_ascii=False)


async def generate_response(request: ResponseRequest, mcp_server: FastMCP) -> Response:
    """Execute the model's MCP calls and return the final SDK Response."""
    input_items: ResponseInputParam = (
        [{"role": "user", "content": request.input}]
        if isinstance(request.input, str)
        else list(request.input)
    )
    openai_client, default_model = create_openai_client()

    try:
        async with openai_client, Client(mcp_server) as mcp_client:
            tools: list[FunctionToolParam] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.inputSchema,
                    "strict": False,
                }
                for tool in await mcp_client.list_tools()
            ]
            allowed_names = {tool["name"] for tool in tools}

            for round_number in range(MAX_TOOL_ROUNDS + 1):
                response = await openai_client.responses.create(
                    model=request.model or default_model,
                    input=input_items,
                    instructions=request.instructions or DEFAULT_INSTRUCTIONS,
                    max_output_tokens=request.max_output_tokens or omit,
                    store=False,
                    tools=tools,
                )
                calls = [
                    item for item in response.output if item.type == "function_call"
                ]
                if not calls:
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
    except APITimeoutError as error:
        raise HTTPException(
            status_code=504, detail="上游 Responses API 请求超时。"
        ) from error
    except APIStatusError as error:
        raise HTTPException(
            status_code=429 if error.status_code == 429 else 502,
            detail=f"上游 Responses API 返回 HTTP {error.status_code}。",
        ) from error
    except APIError as error:
        raise HTTPException(
            status_code=502, detail="上游 Responses API 请求失败。"
        ) from error

    raise HTTPException(status_code=502, detail="模型工具调用达到轮数上限。")
