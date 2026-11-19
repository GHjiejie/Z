"""Call the sprint task server over stdio or Streamable HTTP."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport


async def run_demo(client: Client) -> None:
    """Discover and use the server's tools, resources, and prompt."""
    async with client:
        tools = await client.list_tools()
        resources = await client.list_resources()
        templates = await client.list_resource_templates()
        prompts = await client.list_prompts()
        print("从 OpenAPI 生成的工具及输入 Schema")
        for tool in tools:
            print(f"  {tool.name}", json.dumps(tool.inputSchema, ensure_ascii=False))
        print("\n资源", ", ".join(str(item.uri) for item in resources))
        print("资源模板", ", ".join(item.uriTemplate for item in templates))
        print("提示模板", ", ".join(prompt.name for prompt in prompts))

        summary = await client.call_tool("summarize_tasks")
        print("\n任务统计", json.dumps(summary.structured_content, ensure_ascii=False))

        active = await client.call_tool("list_tasks", {"status": "in_progress"})
        # FastMCP wraps a top-level array in the MCP structured result object.
        tasks = active.structured_content["result"]
        print("进行中的任务", json.dumps(tasks, ensure_ascii=False))

        guide = await client.read_resource("tasks://status-guide")
        print("\n状态说明\n" + guide[0].text)

        if tasks:
            detail = await client.call_tool("get_task", {"task_id": tasks[0]["id"]})
            print(
                "\n任务详情\n"
                + json.dumps(detail.structured_content, ensure_ascii=False, indent=2)
            )

        brief = await client.get_prompt(
            "sprint_brief", {"goal": "完成通知中心体验优化"}
        )
        print("\n迭代简报提示\n" + brief.messages[0].content.text)


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 FastMCP OpenAPI 工具生成示例")
    parser.add_argument("--url", help="已启动的 Streamable HTTP 服务地址")
    args = parser.parse_args()

    if args.url:
        transport = StreamableHttpTransport(url=args.url)
    else:
        transport = StdioTransport(
            command=sys.executable,
            args=["-m", "fastmcp_demo.server"],
            cwd=Path(__file__).resolve().parents[1],
        )

    asyncio.run(run_demo(Client(transport)))


if __name__ == "__main__":
    main()
