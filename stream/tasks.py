from typing import Annotated, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from chat_models.chat import chat_model


class ChatState(TypedDict):
    messages: Annotated[list, add_messages]


def llm_call(state: ChatState):
    response = chat_model.invoke(state["messages"])
    return {"messages": [response]}


checkpointer = InMemorySaver()

config = RunnableConfig(
    configurable={
        "thread_id": "user-001",
    }
)


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
            # 原因是 LangGraph 的 stream_mode="messages" 返回的每一项不是消息对象本身，而是一个二元组：(message_chunk, metadata)
            # 直接解包
            for task in graph.stream(
                {"messages": [{"role": "user", "content": user_input}]},
                config=config,
                stream_mode="tasks",
            ):
                print(task)

    except KeyboardInterrupt:
        print("\n服务已终止。")


if __name__ == "__main__":
    main()
