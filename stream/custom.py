from time import sleep
from typing import Annotated, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from chat_models.chat import chat_model


class ChatState(TypedDict):
    messages: Annotated[list, add_messages]
    answer: Annotated[str, "answer"]  # 添加一个 answer 字段，用于存储 RAG 检索的结果


def llm_call(state: ChatState):
    response = chat_model.invoke(state["messages"])
    return {"messages": [response]}


def rag_search(state: ChatState):
    writer = get_stream_writer()
    writer({"custom": "rag_search"})
    sleep(1)  # 模拟RAG检索的延迟
    return {"answer": "RAG检索结果"}  # 示例返回RAG检索结果


def check_search_answer(state: ChatState):
    writer = get_stream_writer()
    writer({"custom": "check_search_answer"})
    sleep(1)  # 模拟检查检索结果的延迟
    return {"answer": "dhfhkdvfkhbkh"}  # 返回RAG检索结果


builder = StateGraph(ChatState)
builder.add_node("llm_call", llm_call)
builder.add_node("rag_search", rag_search)
builder.add_node("check_search_answer", check_search_answer)
builder.add_edge(START, "rag_search")
builder.add_edge("rag_search", "check_search_answer")
builder.add_edge("check_search_answer", "llm_call")

builder.add_edge("llm_call", END)
graph = builder.compile()


def main():
    try:
        while True:
            user_input = input("User: ")
            if user_input.lower() == "exit":
                break

            # 使用的是values的时候，模型给出的不是流式输出，而是每一步后的完整state

            for chunk in graph.stream(
                input={
                    "messages": [{"role": "user", "content": user_input}],
                    "answer": "",
                },
                stream_mode="custom",
            ):
                print(chunk)

    except KeyboardInterrupt:
        print("\n服务已终止。")


if __name__ == "__main__":
    main()
