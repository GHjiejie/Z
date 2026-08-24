from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from chat_models.chat import chat_model


class ChatState(TypedDict):
    messages: Annotated[list, add_messages]


def llm_call(state: ChatState):
    response = chat_model.invoke(state["messages"])
    return {"messages": [response]}


def before_llm_call(state: ChatState):
    return state


def after_llm_call(state: ChatState):
    return state


builder = StateGraph(ChatState)
builder.add_node("llm_call", llm_call)
builder.add_node("before_llm_call", before_llm_call)
builder.add_node("after_llm_call", after_llm_call)
builder.add_edge(START, "before_llm_call")
builder.add_edge("before_llm_call", "llm_call")
builder.add_edge("llm_call", "after_llm_call")
builder.add_edge("after_llm_call", END)
graph = builder.compile()


def main():
    try:
        while True:
            user_input = input("User: ")
            if user_input.lower() == "exit":
                break

            # 如果使用的是updates的时候，模型给出的不是每一步后的完整state，而是每一步的更新内容，就是我们可以看到每一步状态的变化

            for state in graph.stream(
                {"messages": [{"role": "user", "content": user_input}]},
                stream_mode="updates",
            ):
                print(state)

    except KeyboardInterrupt:
        print("\n服务已终止。")


if __name__ == "__main__":
    main()
