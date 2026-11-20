"""Run the demo Agent against a Streamable HTTP MCP endpoint."""

import argparse
import asyncio
import logging

import httpx
from mcp import McpError
from openai import APIError, APIStatusError, APITimeoutError

from fastmcp_demo.responses import ResponseRequest, generate_response


def main() -> None:
    parser = argparse.ArgumentParser(description="用模型调用 FastAPI 生成的 MCP 工具")
    parser.add_argument(
        "question", nargs="?", default="查询正在进行的迭代任务，告诉我任务编号和标题。"
    )
    parser.add_argument("--url", help="覆盖默认 MCP 地址")
    args = parser.parse_args()
    question = args.question.strip()
    if not question:
        parser.error("请输入问题。")

    logging.basicConfig(format="%(message)s")
    logging.getLogger("fastmcp_demo").setLevel(logging.INFO)

    try:
        response = asyncio.run(
            generate_response(ResponseRequest(input=question), mcp_url=args.url)
        )
    except APITimeoutError:
        parser.exit(1, "模型供应商请求超时。\n")
    except APIStatusError as error:
        parser.exit(1, f"模型供应商返回 HTTP {error.status_code}。\n")
    except APIError:
        parser.exit(1, "模型供应商请求失败。\n")
    except httpx.HTTPError:
        parser.exit(1, "MCP 服务请求失败。\n")
    except (McpError, RuntimeError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    answer = response.output_text.strip()
    if not answer:
        parser.exit(1, "模型没有返回文本回答。\n")
    print(answer)


if __name__ == "__main__":
    main()
