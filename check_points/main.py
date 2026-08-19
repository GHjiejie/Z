from operator import add
from typing import Annotated
from typing_extensions import TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver


class State(TypedDict):
    # reducer=add：新列表会追加到旧列表，而不是覆盖
    messages: Annotated[list[str], add]


def uppercase(state: State):
    latest = state["messages"][-1]
    return {"messages": [latest.upper()]}


builder = StateGraph(State)
builder.add_node("uppercase", uppercase)
builder.add_edge(START, "uppercase")
builder.add_edge("uppercase", END)

graph = builder.compile(checkpointer=InMemorySaver())

print("\n--- 图结构 ASCII 可视化 ---")
    
     # 可以看到所有的可能路径
print(graph.get_graph().draw_ascii())


config: RunnableConfig = {
    "configurable": {
        "thread_id": "user-001",
    }
}

result1 = graph.invoke(
    {"messages": ["hello"]},
    config=config,
)

print(result1)
# {'messages': ['hello', 'HELLO']}

# result2 = graph.invoke(
#     {"messages": ["langgraph"]},
#     config=config,
# )
# print(result2)

histry=list(graph.get_state_history(config=config))
print("查看历史状态",histry)


# cp_tuple=graph.get_tuple_history(config=config)
# print("查看历史元组",cp_tuple)
