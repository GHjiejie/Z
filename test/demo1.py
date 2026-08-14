from langchain.tools import tool

from chat import chat_model


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


# 然后接下来的思路就是在接受用户输入的时候，怎么判断要调用你一个工具

# 将工具绑定到模型上
tools = [add, subtract, multiply]

model_with_tools = chat_model.bind_tools(tools)

response = model_with_tools.invoke("告诉我1+7等于多少？")
print(response)
