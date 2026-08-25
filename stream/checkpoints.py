from typing import Annotated, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from chat_models.chat import chat_model


class ChatState(TypedDict):
    messages: Annotated[list, add_messages]


config: RunnableConfig = RunnableConfig(
    configurable={
        "thread_id": "user-001",
    }
)


def llm_call(state: ChatState):
    response = chat_model.invoke(state["messages"])
    return {"messages": [response]}


checkpointer = InMemorySaver()


builder = StateGraph(ChatState)
builder.add_node("llm_call", llm_call)
builder.add_edge(START, "llm_call")
builder.add_edge("llm_call", END)
graph = builder.compile(checkpointer=checkpointer)


def main():
    try:
        while True:
            user_input = input("User: ")
            if user_input.lower() == "exit":
                break

            for chunk in graph.stream(
                {"messages": [{"role": "user", "content": user_input}]},
                config=config,
                stream_mode="checkpoints",
            ):
                print(chunk)

    except KeyboardInterrupt:
        print("\n服务已终止。")


if __name__ == "__main__":
    main()
