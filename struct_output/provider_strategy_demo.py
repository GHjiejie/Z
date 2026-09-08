# 运行命令：uv run python -m struct_output.provider_strategy_demo

from langchain.agents import create_agent
from langchain.agents.structured_output import ProviderStrategy
from pydantic import BaseModel, Field

from chat_models.chat import chat_model


class ContactInfo(BaseModel):
    """一个人的联系方式。"""

    name: str = Field(description="姓名")
    email: str = Field(description="电子邮箱")
    phone: str = Field(description="电话号码")


agent = create_agent(
    model=chat_model,
    tools=[],
    # 显式使用模型提供商原生的结构化输出。
    # strict=True 需要 langchain >= 1.2，且提供商支持严格模式。
    response_format=ProviderStrategy(schema=ContactInfo, strict=True),
)

result = agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": (
                    "提取联系人信息：张三，邮箱 zhangsan@example.com，"
                    "电话 138-0000-1234。"
                ),
            }
        ]
    }
)

contact = result["structured_response"]
print(contact)
print(type(contact))
