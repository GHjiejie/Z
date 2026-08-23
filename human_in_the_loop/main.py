from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt

from chat_models.chat import chat_model

dp_path = "./human_in_the_loop/human_in_the_loop.sqlite"


class ChatState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    approved: bool


def llm_call(state: ChatState):
    response = chat_model.invoke(state["messages"])
    return {"messages": [response]}


def approve_node(state: ChatState):

    approved = interrupt(
        "Please approve the response. Type 'yes' to approve or 'no' to reject."
    )

    if approved.lower() == "yes":
        print("Response approved.")
        return {
            "approved": True,
        }
    else:
        print("Response rejected. Please provide feedback.")
        return {
            "approved": False,
        }


def check_approval(state: ChatState):
    if state.get("approved") is True:
        return "llm_call"

    else:
        return "END"


builder = StateGraph(ChatState)
builder.add_node("llm_call", llm_call)
builder.add_node("approve_node", approve_node)
builder.add_edge(START, "approve_node")
builder.add_edge("llm_call", END)
builder.add_conditional_edges(
    "approve_node",
    check_approval,
    {
        "llm_call": "llm_call",
        "END": END,
    },
)


config: RunnableConfig = {"configurable": {"thread_id": "session_human_in_loop"}}


def main():
    with SqliteSaver.from_conn_string(dp_path) as checkpointer:
        graph = builder.compile(checkpointer=checkpointer)

        while True:
            state = graph.get_state(config=config)

            if state.next:
                interrupt_msg = state.tasks[0].interrupts[0].value

                human_feedback = input(f"\n[System] {interrupt_msg}\napproval: ")

                stream_input = Command(resume=human_feedback)

            else:
                user_input = input("\nuser: ")
                if not user_input.strip():
                    continue
                stream_input = {"messages": [HumanMessage(content=user_input)]}

            for part in graph.stream(
                stream_input,
                config=config,
                stream_mode="messages",
                version="v2",
            ):
                if part["type"] != "messages":
                    continue

                message_chunk, metadata = part["data"]

                if metadata.get("langgraph_node") not in ["llm_call", "approve_node"]:
                    continue

                text = str(message_chunk.text)
                if text:
                    print(text, end="", flush=True)


if __name__ == "__main__":
    main()
