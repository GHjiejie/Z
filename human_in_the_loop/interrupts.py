from typing import Annotated, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt

from chat_models.chat import chat_model

dp_path = "./human_in_the_loop/human_in_the_loop.sqlite"


class ChatState(TypedDict):
    messages: Annotated[list, add_messages]


def llm_call(state: ChatState):
    response = chat_model.invoke(state["messages"])
    return {"messages": [response]}


def approve_node(state: ChatState):

    approved = interrupt(
        "Please approve the model answer the question. Type 'yes' to approve or 'no' to reject."
    )

    if approved.lower() == "yes":
        print("Response approved.")
        return Command(goto="llm_call")

    else:
        print("Response rejected. Please provide feedback.")
        return Command(goto="END")


builder = StateGraph(ChatState)
builder.add_node("llm_call", llm_call)
builder.add_node("approve_node", approve_node)
builder.add_edge(START, "approve_node")
builder.add_edge("llm_call", END)


config: RunnableConfig = {"configurable": {"thread_id": "session_human_in_loop_3"}}


def main():
    with SqliteSaver.from_conn_string(dp_path) as checkpointer:
        graph = builder.compile(checkpointer=checkpointer)

        stream_input: ChatState | Command

        while True:
            state = graph.get_state(config=config)

            interrupts = [
                interrupt_item
                for task in state.tasks
                for interrupt_item in task.interrupts
            ]
            if interrupts:
                interrupt_msg = interrupts[0]

                human_input = input(
                    f"\n[System] {interrupt_msg.value}\napproval: "
                ).strip()

                stream_input = Command(resume=human_input)
            else:
                user_input = input("\nuser: ")
                if not user_input.strip():
                    continue
                stream_input = {"messages": [{"role": "user", "content": user_input}]}

            for chunk in graph.stream(
                input=stream_input,
                config=config,
                stream_mode=["messages"],
            ):
                print(chunk, end="", flush=True)
                print("\n---\n")


if __name__ == "__main__":
    main()
