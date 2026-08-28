"""核心概念 4：Structured output（结构化输出）。"""

import asyncio

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from pydantic import BaseModel, Field

try:
    from .concept import get_chat_model, print_sse_stream
except ImportError:
    from concept import get_chat_model, print_sse_stream


class ContactInfo(BaseModel):
    """从自然语言中提取出的联系人信息。"""

    name: str = Field(description="联系人姓名")
    email: str = Field(description="电子邮箱")
    age: int = Field(description="年龄")


async def run_demo() -> None:
    agent = create_agent(
        model=get_chat_model(),
        tools=[],
        # ToolStrategy 对所有支持 tool calling 的模型都适用。
        response_format=ToolStrategy(ContactInfo),
    )
    await print_sse_stream(
        agent,
        {
            "messages": [
                {
                    "role": "user",
                    "content": "提取联系人：张明，28 岁，邮箱 zhangming@example.com。",
                }
            ]
        }
    )


if __name__ == "__main__":
    asyncio.run(run_demo())
