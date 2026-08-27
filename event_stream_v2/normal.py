from pprint import pformat
from typing import Annotated, TypedDict

from langchain_core.callbacks.manager import dispatch_custom_event
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from chat_models.chat import chat_model


class ChatState(TypedDict):
    messages: Annotated[list, add_messages]


# astream_events(version="v2") 中常见的标准事件。
# 实际会出现哪些类型，取决于图里使用了模型、工具、检索器还是 Prompt。
V2_EVENT_TYPES = {
    "chain / graph / node": (
        "on_chain_start",
        "on_chain_stream",
        "on_chain_end",
    ),
    "chat model": (
        "on_chat_model_start",
        "on_chat_model_stream",
        "on_chat_model_end",
    ),
    "llm": ("on_llm_start", "on_llm_stream", "on_llm_end"),
    "tool": ("on_tool_start", "on_tool_end", "on_tool_error"),
    "retriever": ("on_retriever_start", "on_retriever_end"),
    "prompt": ("on_prompt_start", "on_prompt_end"),
    "custom (v2)": ("on_custom_event",),
}


def print_v2_event_types() -> None:
    """在终端按 Runnable 类型列出 v2 的常见事件。"""
    print("\nastream_events v2 事件类型")
    for runnable_type, event_types in V2_EVENT_TYPES.items():
        print(f"\n[{runnable_type}]")
        for event_type in event_types:
            print(f"  - {event_type}")


def print_event(event: dict) -> None:
    """按事件类型输出一次运行期间收到的事件。"""
    event_type = event.get("event", "unknown_event")
    print(f"\n===== {event_type} =====")
    print(f"name: {event.get('name', '')}")
    print(f"run_id: {event.get('run_id', '')}")
    print(f"parent_ids: {event.get('parent_ids', [])}")
    print(f"data:\n{pformat(event.get('data'), sort_dicts=False)}")


def llm_call(state: ChatState):
    response = chat_model.invoke(state["messages"])
    return {"messages": [response]}


def check_llm_call(state: ChatState):
    dispatch_custom_event(
        "check_llm_call",
        {"message": "check_llm_call 节点开始执行"},
    )

    return {"messages": state["messages"]}


def check_llm_call_again(state: ChatState):
    dispatch_custom_event(
        "check_llm_call_again",
        {"message": "check_llm_call_again 节点开始执行"},
    )

    return {"messages": state["messages"]}


builder = StateGraph(ChatState)
builder.add_node("llm_call", llm_call)
builder.add_node("check_llm_call", check_llm_call)
builder.add_node("check_llm_call_again", check_llm_call_again)


builder.add_edge(START, "llm_call")
builder.add_edge("llm_call", "check_llm_call")
builder.add_edge("check_llm_call", "check_llm_call_again")
builder.add_edge("check_llm_call_again", END)

graph = builder.compile()


def main():
    print_v2_event_types()
    try:
        while True:
            user_input = input("User: ")
            if user_input.lower() == "exit":
                break

            for event in graph.stream_events(
                input={
                    "messages": [{"role": "user", "content": user_input}],
                },
                version="v2",
            ):
                print(event)
                print()
                # print_event(event)

    except KeyboardInterrupt:
        print("\n服务已终止。")


if __name__ == "__main__":
    main()
