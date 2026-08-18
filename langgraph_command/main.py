from typing import Literal, NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

Intent = Literal["search_order", "delete_order", "unknown"]


class State(TypedDict):
    messages: list[str]
    intent: NotRequired[Intent]


def recognize_intent(
    state: State,
) -> Command[Literal["search_order", "delete_order", "unknown"]]:
    """识别意图，同时更新 State 并指定下一个节点。"""
    user_message = state["messages"][-1]

    if "搜索" in user_message or "查询" in user_message:
        intent: Intent = "search_order"
    elif "删除" in user_message or "取消" in user_message:
        intent = "delete_order"
    else:
        intent = "unknown"

    return Command(
        # update 会在跳转前合并到图的 State 中。
        update={
            "messages": [*state["messages"], f"识别到的意图是: {intent}"],
            "intent": intent,
        },
        # goto 决定接下来执行哪个节点。
        goto=intent,
    )


def search_order(state: State) -> State:
    return {
        "messages": [*state["messages"], "执行搜索订单操作"],
        "intent": "search_order",
    }


def delete_order(state: State) -> State:
    return {
        "messages": [*state["messages"], "执行删除订单操作"],
        "intent": "delete_order",
    }


def unknown(state: State) -> State:
    return {
        "messages": [*state["messages"], "无法识别意图，请补充订单操作信息"],
        "intent": "unknown",
    }


builder = StateGraph(State)

builder.add_node("recognize_intent", recognize_intent)
builder.add_node("search_order", search_order)
builder.add_node("delete_order", delete_order)
builder.add_node("unknown", unknown)

builder.add_edge(START, "recognize_intent")

# recognize_intent 通过 Command.goto 路由，因此这里不需要 add_conditional_edges。
builder.add_edge("search_order", END)
builder.add_edge("delete_order", END)
builder.add_edge("unknown", END)

graph = builder.compile()


if __name__ == "__main__":
    print("\n--- 图结构 ASCII 可视化 ---")
    
     # 可以看到所有的可能路径
    print(graph.get_graph().draw_ascii())
   
    
    examples = [
        "帮我搜索订单编号 123456",
        "帮我删除订单编号 123456",
        "今天天气怎么样？",
    ]

    for message in examples:
        result = graph.invoke({"messages": [message]})
        print(result)
