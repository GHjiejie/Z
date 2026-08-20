from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from chat_models.chat import chat_model


class ChatState(TypedDict):
    user_msg: Annotated[list[BaseMessage], add_messages]


db_path = "./check_points/time_travel.sqlite"


def llm_call(state: ChatState):
    response = chat_model.invoke(state["user_msg"])
    return {"user_msg": [response]}


builder = StateGraph(ChatState)
builder.add_node("llm_call", llm_call)
builder.add_edge(START, "llm_call")
builder.add_edge("llm_call", END)


def main():
    print("🤖 Chat bot started! (按 Ctrl+C 退出)")
    config: RunnableConfig = {"configurable": {"thread_id": "session_1"}}

    # 核心修改：在主函数中开启数据库连接，并在整个对话期间保持打开
    with SqliteSaver.from_conn_string(db_path) as checkpointer:
        graph = builder.compile(checkpointer=checkpointer)

        try:
            while True:
                user_input = input("\nuser: ")

                if not user_input.strip():
                    continue

                events = graph.stream(
                    {"user_msg": [HumanMessage(content=user_input)]},
                    config=config,
                    stream_mode="values",
                )

                for event in events:
                    latest_message = event["user_msg"][-1]
                    if isinstance(latest_message, AIMessage):
                        print(f"bot: {latest_message.content}")
                        # 找到最新的一条 AI 回复后退出当前事件的循环
                        break

        except KeyboardInterrupt:
            print("\n\n👋 收到退出指令，聊天结束。再见！")


if __name__ == "__main__":
    main()
