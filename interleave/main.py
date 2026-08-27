"""使用真实聊天模型演示 LangGraph 的 stream.interleave。"""

import sys
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from chat_models.chat import chat_model

# test


class ChatState(TypedDict):
    """add_messages 负责把每个节点产生的新消息合并到历史消息中。"""

    messages: Annotated[list[BaseMessage], add_messages]


def llm_call(state: ChatState) -> dict[str, list[BaseMessage]]:
    """调用聊天模型，并把模型回复写回图状态。"""

    reply = chat_model.invoke(state["messages"])
    return {"messages": [reply]}


def build_graph():
    builder = StateGraph(ChatState)
    builder.add_node("llm_call", llm_call)
    builder.add_edge(START, "llm_call")
    builder.add_edge("llm_call", END)
    return builder.compile(name="interleave_demo")


def run_once(graph, user_input: str) -> None:
    """执行一次图，并在一个循环中同时消费消息流和状态流。"""

    # 单独先消费 stream.messages，再消费 stream.values，后者可能已经错过事件。
    # interleave 会订阅所有指定投影，并按它们的真实到达顺序统一输出。
    with graph.stream_events(
        {"messages": [{"role": "user", "content": user_input}]},
        version="v3",
    ) as stream:
        for projection, item in stream.interleave("messages", "values"):
            if projection == "messages":
                print(f"[messages/{item.node}] ", end="", flush=True)
                for text in item.text:
                    print(text, end="", flush=True)
                print()
            elif projection == "values":
                history = " -> ".join(message.type for message in item["messages"])
                print(f"[values] 消息历史: {history}")

        print(f"[output] 最终回复: {stream.output['messages'][-1].text}")


def main() -> None:
    graph = build_graph()

    # 传入命令行参数时只执行一次，便于快速体验和自动化测试。
    if len(sys.argv) > 1:
        run_once(graph, " ".join(sys.argv[1:]))
        return

    try:
        while True:
            user_input = input("User: ").strip()
            if user_input.lower() == "exit":
                break
            if user_input:
                run_once(graph, user_input)
    except (EOFError, KeyboardInterrupt):
        print("\n服务已终止。")


if __name__ == "__main__":
    main()
