# 运行命令：uv run python -m struct_output.typed_dict_demo

from typing import TypedDict

from langchain.agents import create_agent

from chat_models.chat import chat_model


class ContactInfo(TypedDict):
    """一个人的联系方式。"""

    name: str  # 姓名
    email: str  # 电子邮箱
    phone: str  # 电话号码


agent = create_agent(
    model=chat_model,
    tools=[],
    # 直接传入 TypedDict 类型，LangChain 会自动选择输出策略。
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

# TypedDict schema 返回普通 dict。
contact = result["structured_response"]
print(contact)
print(type(contact))
