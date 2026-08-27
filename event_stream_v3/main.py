import asyncio
from typing import Annotated, TypedDict

from langchain_core.callbacks.manager import dispatch_custom_event
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.stream import GraphRunStream

from chat_models.chat import chat_model


class ChatState(TypedDict):
    messages: Annotated[list, add_messages]


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

# 下面的代码是有问题的，不会全部消费，而是只会消费message_comsumer
# 后面的消费者都不会有内容输出


def message_comsumer(stream: GraphRunStream):
    for message_stream in stream.messages:
        for text in message_stream.text:
            print(text, end="", flush=True)
        print()


def values_comsumer(stream: GraphRunStream):
    for value in stream.values:
        print(value)
        print()


def subgraphs_comsumer(stream: GraphRunStream):
    for subgraph in stream.subgraphs:
        print(subgraph)
        print()


async def main():

    try:
        while True:
            user_input = input("User: ")
            if user_input.lower() == "exit":
                break

            with graph.stream_events(
                input={
                    "messages": [{"role": "user", "content": user_input}],
                },
                version="v3",
            ) as stream:
                message_comsumer(stream)
                # values_comsumer(stream)
                # subgraphs_comsumer(stream)

    except KeyboardInterrupt:
        print("\n服务已终止。")


if __name__ == "__main__":
    asyncio.run(main())
