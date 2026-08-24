from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage
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

            # 使用的是values的时候，模型给出的不是流式输出，而是每一步后的完整state

            for value in graph.stream(
                {"messages": [{"role": "user", "content": user_input}]},
                stream_mode="values",
            ):
                print(f"res: {value}")

                # 输出模型的响应
                last_msg = value["messages"][-1]

                if isinstance(last_msg, AIMessage):
                    print(f"Model: {last_msg.content}")

    except KeyboardInterrupt:
        print("\n服务已终止。")


if __name__ == "__main__":
    main()
