"""List visible tools or request deletion with confirmation in the terminal."""

import argparse
import asyncio
import json

from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult

from fastmcp_minimal_demo.hitl_server import MCP_PORT
from fastmcp_minimal_demo.settings import DELETE_TOKEN_ENV, TOKEN_ENV, load_token


async def confirm_deletion(message, response_type, params, context):
    try:
        answer = await asyncio.to_thread(input, f"{message} 输入 yes 确认，回车取消 ")
    except EOFError:
        return ElicitResult(action="cancel")
    if answer.strip().lower() == "yes":
        return {"confirm": True}
    return ElicitResult(action="decline")


async def run(record_id: str | None, user_id: str | None) -> None:
    is_delete = record_id is not None or user_id is not None
    token = load_token(DELETE_TOKEN_ENV if is_delete else TOKEN_ENV)
    async with Client(
        f"http://127.0.0.1:{MCP_PORT}/mcp",
        auth=token,
        mode="2026-07-28",
        elicitation_handler=confirm_deletion,
    ) as client:
        print("可用工具", ", ".join(tool.name for tool in await client.list_tools()))
        if record_id is not None:
            result = await client.call_tool("delete_record", {"record_id": record_id})
        elif user_id is not None:
            result = await client.call_tool("delete_record_user", {"user_id": user_id})
        else:
            return
        print(json.dumps(result.structured_content, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="查看可用工具或人工确认删除")
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument("record_id", nargs="?", help="需要删除的记录 ID")
    targets.add_argument("--user-id", help="按此用户 ID 删除记录")
    args = parser.parse_args()
    asyncio.run(run(args.record_id, args.user_id))


if __name__ == "__main__":
    main()
