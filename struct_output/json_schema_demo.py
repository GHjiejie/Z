# 运行命令：uv run python -m struct_output.json_schema_demo

from langchain.agents import create_agent
from langchain.agents.structured_output import ProviderStrategy

from chat_models.chat import chat_model

contact_info_schema = {
    "title": "ContactInfo",
    "description": "一个人的联系方式。",
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "姓名"},
        "email": {"type": "string", "description": "电子邮箱"},
        "phone": {"type": "string", "description": "电话号码"},
    },
    "required": ["name", "email", "phone"],
    "additionalProperties": False,
}

agent = create_agent(
    model=chat_model,
    tools=[],
    # JSON Schema 字典不能直接传给 response_format，必须显式指定策略。
    response_format=ProviderStrategy(contact_info_schema),
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

print(result)
contact = result["structured_response"]
print(contact)
print(type(contact))
