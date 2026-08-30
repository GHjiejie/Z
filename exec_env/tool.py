"""DeepAgent 核心概念：Tools（工具）。

`deepagents.create_deep_agent` 与 `langchain.agents.create_agent` 一样,
把工具以 ``tools=[...]`` 的形式传给图构建器;工具本身由 ``langchain_core.tools``
里的 ``@tool`` 装饰器声明,这也是 deepagents 在内部(例如
``packages/adapters/harness/deepagents/coding_factory.py``)使用的写法。

运行前:

    pip install deepagents==0.7.11

并确保 ``.env`` 里设置了 ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` /
``MODEL``(参见 ``chat_models/chat.py``)。
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel, Field

try:
    from deepagents import create_deep_agent
except (
    ImportError
):  # pragma: no cover - demo runs in environments with deepagents installed
    create_deep_agent = None  # type: ignore[assignment]

try:
    from chat_models.chat import chat_model
except ImportError:  # pragma: no cover - allow standalone import
    chat_model = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 1) 最简工具:函数名 = 工具名,docstring = 工具描述(LLM 用来决定何时调用)。
# ---------------------------------------------------------------------------
@tool
def get_current_time() -> str:
    """返回服务器当前的本地时间,格式为 ``YYYY-MM-DD HH:MM:SS``。"""

    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------------------------------------------------------------------------
# 2) 带类型注解的工具:类型注解会被 LangChain 转换成 JSON Schema,
#    让模型知道要传什么参数;``Annotated[..., Field(description=...)]``
#    进一步给字段写说明,提升模型选参准确率。
# ---------------------------------------------------------------------------
@tool
def calculate_order_total(
    unit_price: Annotated[float, Field(description="商品单价,单位:元")],
    quantity: Annotated[int, Field(description="购买数量,必须为正整数")],
) -> str:
    """根据商品单价和数量计算订单总价。"""

    total = unit_price * quantity
    return f"{total:.2f} 元"


# ---------------------------------------------------------------------------
# 3) 用 Pydantic 模型声明参数的工具:适合参数多、需要嵌套字段或约束的场景,
#    把 ``args_schema`` 显式传给 ``@tool`` 即可。
# ---------------------------------------------------------------------------
class WeatherQuery(BaseModel):
    city: str = Field(description="要查询的城市中文名,例如 '北京'")
    unit: str = Field(
        default="celsius", description="温度单位,可选 'celsius' 或 'fahrenheit'"
    )


@tool("get_weather", args_schema=WeatherQuery, return_direct=False)
def get_weather(city: str, unit: str = "celsius") -> dict[str, Any]:
    """查询指定城市的当前天气。

    这是一个演示用的伪实现,真实场景里请替换为天气 API 调用。
    """

    sample = {"北京": 22, "上海": 27, "深圳": 30, "杭州": 25}
    temp = sample.get(city, 20)
    if unit == "fahrenheit":
        temp = temp * 9 / 5 + 32
        symbol = "°F"
    else:
        symbol = "°C"
    return {"city": city, "temperature": f"{temp}{symbol}", "unit": unit}


# ---------------------------------------------------------------------------
# 4) 返回结构化 dict 的工具:模型会把整个 dict 当作观察结果,
#    适合需要多字段上下文的下游推理。
# ---------------------------------------------------------------------------
@tool
def search_kb(
    keyword: Annotated[str, Field(description="知识库检索关键字")],
) -> dict[str, Any]:
    """在内部知识库里检索关键字,返回最相关的若干条记录。"""

    knowledge = {
        "refund": "退款流程:用户在订单页点击申请退款,平台审核后 1-3 个工作日内原路退回。",
        "shipping": "发货时效:现货商品 24 小时内发出,预售商品以详情页公示为准。",
        "invoice": "发票申请:订单完成后 30 天内,在订单详情页提交开票信息即可。",
    }
    hits = [
        {"keyword": k, "snippet": v}
        for k, v in knowledge.items()
        if k in keyword.lower()
    ]
    return {"hits": hits or [{"keyword": keyword, "snippet": "(无匹配,转人工)"}]}


# ---------------------------------------------------------------------------
# 把上面的工具汇总成 deepagents 期望的列表。
# ---------------------------------------------------------------------------
DEMO_TOOLS = [
    get_current_time,
    calculate_order_total,
    get_weather,
    search_kb,
]


def _summarize_tool_calls(messages: list[Any]) -> list[dict[str, Any]]:
    """从 agent 输出里抽出 ``AIMessage.tool_calls`` 和对应的 ``ToolMessage``,便于打印。"""

    calls: list[dict[str, Any]] = []
    tool_results: dict[str, str] = {}
    for msg in messages:
        if isinstance(msg, ToolMessage):
            tool_results[msg.tool_call_id] = (
                msg.content if isinstance(msg.content, str) else str(msg.content)
            )
    for msg in messages:
        if getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                calls.append(
                    {
                        "name": tc["name"],
                        "args": tc["args"],
                        "result": tool_results.get(tc["id"], "(no tool result)"),
                    }
                )
    return calls


async def run_demo() -> None:
    """构建一个 deepagent,让它一次性用到上面定义的多个工具。"""

    if create_deep_agent is None:
        raise RuntimeError(
            "未安装 deepagents。请先 `pip install deepagents==0.7.11`,"
            "然后再运行本 demo。"
        )
    if chat_model is None:
        raise RuntimeError(
            "需要先在 chat_models/chat.py 里配置好 OPENAI_API_KEY 等环境变量。"
        )

    agent = create_deep_agent(
        model=chat_model,
        tools=DEMO_TOOLS,
        system_prompt=(
            "你是一个电商客服助理,回答前优先调用合适的工具获取事实,"
            "不要凭印象作答;如果工具返回'无匹配',告诉用户转人工。"
        ),
    )

    question = (
        "现在几点了?商品单价 89 元买 3 件总共多少?另外帮我查一下'退款'政策,"
        "再告诉我深圳现在的天气。"
    )
    result = await agent.ainvoke({"messages": [HumanMessage(content=question)]})

    print("=" * 60)
    print(f"用户问题: {question}")
    print("-" * 60)
    print("工具调用链:")
    for call in _summarize_tool_calls(result["messages"]):
        print(f"  • {call['name']}{call['args']}")
        print(f"      ↳ {call['result']}")
    print("-" * 60)
    final = result["messages"][-1]
    print(
        f"最终回复: {final.content if isinstance(final.content, str) else json.dumps(final.content, ensure_ascii=False)}"
    )
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_demo())
