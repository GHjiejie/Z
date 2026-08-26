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


def check_llm_call(state: ChatState):

    return {"messages": state["messages"]}


builder = StateGraph(ChatState)
builder.add_node("llm_call", llm_call)
# builder.add_node("check_llm_call", check_llm_call)
builder.add_edge(START, "llm_call")
builder.add_edge("llm_call", END)
# builder.add_edge("llm_call", "check_llm_call")
# builder.add_edge("check_llm_call", END)
graph = builder.compile()


def main():
    try:
        while True:
            user_input = input("User: ")
            if user_input.lower() == "exit":
                break

            # 使用的是values的时候，模型给出的不是流式输出，而是每一步后的完整state

            for part in graph.stream(
                input={"messages": [{"role": "user", "content": user_input}]},
                stream_mode=["messages"],
                version="v2",
            ):
                if part["type"] != "messages":
                    continue

                message_chunk, _metadata = part["data"]
                if isinstance(message_chunk, AIMessageChunk):
                    print(message_chunk.text, end="", flush=True)
                # 换行
            print()

    except KeyboardInterrupt:
        print("\n服务已终止。")


if __name__ == "__main__":
    main()
