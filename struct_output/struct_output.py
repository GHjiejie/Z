from typing import Literal, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from chat_models.chat import chat_model


class State(TypedDict):
    messages: list
    intent: str

class IntentResult(BaseModel):
    intent: Literal["search_order", "delete_order", "unknown"] = Field(..., description="识别到的意图")
    
structured_model=chat_model.with_structured_output(
  IntentResult,
  method='function_calling' 
  )

def llm_call(state:State) -> State:
    messages_str = "\n".join(state["messages"])

    result = structured_model.invoke(
f"""
你是一个意图识别模型，你的任务是根据用户的输入识别用户
用户的输入是: {messages_str}""")
    
    if not isinstance(result, IntentResult):
        raise ValueError(f"Expected result to be of type IntentResult, but got {type(result)}")
    
    return {
        "messages": state["messages"] + [f"识别到的意图是: {result.intent}"],
        "intent": result.intent
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
    "messages": ["帮我删除订单编号为 123456 的订单"],
    "intent": ""
})

print(result)













