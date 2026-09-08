# 运行命令：uv run python -m struct_output.pydantic_model_demo

from langchain.agents import create_agent
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
    # 直接传入 Pydantic 类型，LangChain 会自动选择输出策略。
    response_format=ContactInfo,
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

# Pydantic schema 返回经过校验的 ContactInfo 实例。
contact = result["structured_response"]
print(contact)
print(type(contact))
