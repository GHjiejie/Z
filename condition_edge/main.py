from langgraph.graph import StateGraph,START,END

from chat_models.chat import chat_model


from typing import TypedDict

class State(TypedDict):
    messages: list
    intent: str


def llm_call(state:State) -> State:
    # 将消息列表转换为字符串
    messages_str = "\n".join(state["messages"])
    
    prompt=f"""
    你是一个意图识别模型，你的任务是根据用户的输入识别用户
    
    用户的输入是: {messages_str}
    
    请你根据用户的输入，识别用户的意图，只可以返回以下意图之一，不要添加任何其他的值:
    
    search_order, delete_order,unknown
    
    """
    
    response=chat_model.invoke(prompt)
    
    # 不可以直接写
    # 因为 chat_model.invoke() 返回的通常是 AIMessage，而 AIMessage.content 在 LangChain 的类型定义中不保证一定是字符串：
    # intent=response.content.strip() 
    
    content = response.content
    if isinstance(content, str):
        intent = content.strip()
    elif isinstance(content, list):
        # 部分模型会将推理和最终回答拆成多个内容块。
        intent = "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict)
        ).strip()
    else:
        intent = str(content).strip()

    # 仅保留已定义的路由值，避免模型附带解释导致路由失败。
    if intent not in {"search_order", "delete_order", "unknown"}:
        intent = "unknown"
    
    return {
        "messages": state["messages"] + [f"识别到的意图是: {intent}"],
        "intent": intent
    }



    


# 搜索agent
def search_order(state:State) -> State:
    return {
      "messages": state["messages"] + ["执行搜索订单操作"],
      "intent": "search_order"
    }
  
# 删除agent
def delete_order(state:State) -> State:
    return {
      "messages": state["messages"] + ["执行删除订单操作"],
      "intent": "delete_order"
    }



# 开始定义工作流
builder=StateGraph(State)

builder.add_node("llm_call", llm_call)
builder.add_node("search_order", search_order)
builder.add_node("delete_order", delete_order)

builder.add_edge(START, "llm_call")

builder.add_conditional_edges(
  "llm_call",
  lambda state: state["intent"],
  {
    "search_order": "search_order",
    "delete_order": "delete_order",
    "unknown": "delete_order"
  }
)

builder.add_edge("search_order", END)

builder.add_edge("delete_order", END)


# 将工作流编译成图
graph=builder.compile()


result = graph.invoke({
    "messages": ["帮我搜索订单编号为 123456 的订单"],
    "intent": ""
})

print(result)



















