from typing import Annotated, TypedDict

from langchain_core.messages import AIMessageChunk
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from chat_models.chat import chat_model


class ChatState(TypedDict):
    messages: Annotated[list, add_messages]


def llm_call(state: ChatState):
    response = chat_model.invoke(state["messages"])
    return {"messages": [response]}


builder = StateGraph(ChatState)
builder.add_node("llm_call", llm_call)
builder.add_edge(START, "llm_call")
builder.add_edge("llm_call", END)
graph = builder.compile()


def main():
    try:
        while True:
            user_input = input("User: ")
            if user_input.lower() == "exit":
                break

            # 原因是 LangGraph 的 stream_mode="messages" 返回的每一项不是消息对象本身，而是一个二元组：(message_chunk, metadata)
            # 直接解包
            # 还有一点需要你注意的是，stream_mode也可以接受一个list，如果他是一个list的话，那么模型返回的数据结构第一个元素是mode，此时下方的代码就不再适用
            for message_chunk, _metadata in graph.stream(
                {"messages": [{"role": "user", "content": user_input}]},
                # stream_mode="messages",
                stream_mode="messages",
            ):
                print(message_chunk, _metadata)
                if isinstance(message_chunk, AIMessageChunk):
                    print(message_chunk.text, end="", flush=True)
            print()

    except KeyboardInterrupt:
        print("\n服务已终止。")


if __name__ == "__main__":
    main()
