"""Use a real chat model to demonstrate LangGraph stream interleaving."""

import sys
from typing import Annotated, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

# test


class ChatState(TypedDict):
    """Merge new messages from every node into the conversation history."""

    messages: Annotated[list[BaseMessage], add_messages]


def build_graph(model: BaseChatModel | None = None):
    """Build the demo graph, optionally with an injected model for tests."""

    if model is None:
        from chat_models.chat import chat_model

        model = chat_model

    def llm_call(state: ChatState) -> dict[str, list[BaseMessage]]:
        """Call the chat model and append its response to the graph state."""

        reply = model.invoke(state["messages"])
        return {"messages": [reply]}

    builder = StateGraph(ChatState)
    builder.add_node("llm_call", llm_call)
    builder.add_edge(START, "llm_call")
    builder.add_edge("llm_call", END)
    return builder.compile(name="interleave_demo")


def message_event_consumer(event) -> None:
    """Consume one event from the messages projection."""

    print(f"[messages/{event.node}] ", end="", flush=True)
    for text in event.text:
        print(text, end="", flush=True)
    print()


def values_event_consumer(event: ChatState) -> None:
    """Consume one event from the values projection."""

    history = " -> ".join(message.type for message in event["messages"])
    print(f"[values] Message history: {history}")


EVENT_CONSUMERS = {
    "messages": message_event_consumer,
    "values": values_event_consumer,
}


def run_once(graph, user_input: str) -> None:
    """Run the graph once and dispatch interleaved projection events."""

    # Interleave subscribes to every requested projection before consuming any
    # events, then yields all events in their actual arrival order.
    with graph.stream_events(
        {"messages": [{"role": "user", "content": user_input}]},
        version="v3",
    ) as stream:
        for event_type, event in stream.interleave(*EVENT_CONSUMERS):
            EVENT_CONSUMERS[event_type](event)

        print(f"[output] Final response: {stream.output['messages'][-1].text}")


def main() -> None:
    graph = build_graph()
    print("\nGraph workflow")
    print("==============")
    print(graph.get_graph().draw_ascii())
    print('Streaming protocol: version="v3"\n')

    # A command-line argument runs the graph once for quick checks and scripts.
    if len(sys.argv) > 1:
        run_once(graph, " ".join(sys.argv[1:]))
        return

    try:
        while True:
            user_input = input("User: ").strip()
            if user_input.lower() == "exit":
                break
            if user_input:
                run_once(graph, user_input)
    except (EOFError, KeyboardInterrupt):
        print("\nThe service has stopped.")


if __name__ == "__main__":
    main()
