"""核心概念 2：Tools（工具）。"""

import asyncio
from decimal import Decimal

from langchain.agents import create_agent
from langchain.tools import tool

try:
    from .concept import get_chat_model, print_sse_stream
except ImportError:
    from concept import get_chat_model, print_sse_stream


@tool
def calculate_order_total(unit_price: float, quantity: int) -> str:
    """根据商品单价和数量计算订单总价。"""

    total = Decimal(str(unit_price)) * quantity
    return f"{total:.2f} 元"


async def run_demo() -> None:
    agent = create_agent(
        model=get_chat_model(),
        tools=[calculate_order_total],
        system_prompt="涉及订单总价时必须调用工具，不能自行心算。",
    )
    await print_sse_stream(
        agent,
        {
            "messages": [
                {
                    "role": "user",
                    "content": "商品单价 19.9 元，买 6 件，请计算订单总价。",
                }
            ]
        }
    )


if __name__ == "__main__":
    asyncio.run(run_demo())
