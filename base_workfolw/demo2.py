from typing import TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import StateGraph


class State(TypedDict):
    messages: list[BaseMessage]
    user: str


# 用户身份校验函数
def verify_user(user: str) -> bool:
    # 这里可以实现用户身份验证逻辑，例如检查用户名和密码是否匹配
    # 这里只是一个示例，实际应用中应使用更安全的验证方法
    return user == "admin"


# 提示用户权限不足的消息
def insufficient_permissions_message() -> str:
    return "您没有权限执行此操作，请联系管理员。"


# 执行文件输出操作
def execute_file_output(state: State) -> str:

    if not verify_user(state["user"]):
        return "用户身份验证失败，无法执行文件输出操作。"
    else:
        return "文件输出操作已成功执行。"


builder = StateGraph(State)

builder.add_node("verify_user", verify_user)

# 设置条件边
