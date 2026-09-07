# 运行命令：uv run python -m struct_output.tool_strategy_demo

from typing import Literal

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from pydantic import BaseModel, Field

from chat_models.chat import chat_model


class ProductReview(BaseModel):
    """商品评价分析结果。"""

    rating: int | None = Field(description="1 到 5 分的评分", ge=1, le=5)
    sentiment: Literal["positive", "negative"] = Field(description="评价倾向")
    key_points: list[str] = Field(description="1 到 3 个简短要点")


agent = create_agent(
    model=chat_model,
    tools=[],
    response_format=ToolStrategy(
        schema=ProductReview,
        tool_message_content="商品评价已成功转换为结构化数据。",
        handle_errors="请给出 1 到 5 的有效评分，并补全评价倾向和要点。",
    ),
)

result = agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": "分析这条评价：五星，发货很快，不过价格有点贵。",
            }
        ]
    }
)

review = result["structured_response"]
print(review)
print(type(review))
