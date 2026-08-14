from langchain.tools import tool

from chat_models.chat import chat_model


# 定义加法工具
@tool
def add(a: int, b: int) -> int:
    """Add two numbers together."""
    return a + b


# 定义减法工具
@tool
def subtract(a: int, b: int) -> int:
    """Subtract two numbers."""
    return a - b


# 定义乘法工具
@tool
def multiply(a: int, b: int) -> int:
    """Multiply two numbers together."""
    return a * b


@tool
def get_wather(city: str) -> str:
    """Get the weather for a given city."""
    # 这里可以调用一个天气API来获取天气信息
    return f"The weather in {city} is sunny."


tools = [add, subtract, multiply, get_wather]

# 然后接下来的思路就是在接受用户输入的时候，怎么判断要调用你一个工具

# 将工具绑定到模型上
model_with_tools = chat_model.bind_tools(tools)


response = model_with_tools.invoke("深圳的天气怎么样？")
print(response)
