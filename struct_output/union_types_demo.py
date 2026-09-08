# 运行命令：uv run python -m struct_output.union_types_demo

from typing import Literal

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from pydantic import BaseModel, Field

from chat_models.chat import chat_model


class ProductReview(BaseModel):
    """普通商品评价。"""

    rating: int | None = Field(description="1 到 5 分的评分", ge=1, le=5)
    sentiment: Literal["positive", "negative"] = Field(description="评价倾向")
    key_points: list[str] = Field(description="评价要点")


class CustomerComplaint(BaseModel):
    """需要处理的客户投诉。"""

    issue_type: Literal["product", "service", "shipping", "billing"] = Field(
        description="问题类型"
    )
    severity: Literal["low", "medium", "high"] = Field(description="严重程度")
    description: str = Field(description="问题简述")


agent = create_agent(
    model=chat_model,
    tools=[],
    # Union 是 ToolStrategy 特有的能力，模型会选择最匹配的 schema。
    response_format=ToolStrategy(ProductReview | CustomerComplaint),
)

result = agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": (
                    "分析这条反馈：包裹已经晚到两周，客服也一直不回复，"
                    "我要求尽快处理。"
                ),
            }
        ]
    }
)

feedback = result["structured_response"]
print(feedback)
print(type(feedback))
