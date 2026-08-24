from typing import Annotated, TypedDict

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
builder.compile()


def main():
    try:
        while True:
            user_input = input("User: ")
            if user_input.lower() == "exit":
                break

            res = chat_model.invoke([{"role": "user", "content": user_input}])
            print(f"res: {res}")
    except KeyboardInterrupt:
        print("\n服务已终止。")


if __name__ == "__main__":
    main()
