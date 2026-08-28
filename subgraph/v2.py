"""Stream parent-graph and subgraph events with protocol version 2."""

import sys
from typing import Any

from subgraph.graph import ParentState, graph, print_workflows


def namespace_label(namespace: tuple[str, ...]) -> str:
    """Return a readable graph label for a LangGraph namespace."""

    if not namespace:
        return "parent"
    return " > ".join(part.split(":", maxsplit=1)[0] for part in namespace)


def run(topic: str) -> ParentState:
    """Run ``graph.stream`` with v2 and print nested graph events."""

    final_state: ParentState | None = None
    active_message: tuple[str, str] | None = None
    for event in graph.stream(
        {"topic": topic, "learning_note": ""},
        stream_mode=["updates", "messages", "values"],
        subgraphs=True,
        version="v2",
    ):
        event_type = event["type"]
        namespace = event["ns"]
        data: Any = event["data"]
        graph_label = namespace_label(namespace)

        if event_type == "updates":
            if active_message is not None:
                print()
                active_message = None
            node_names = ", ".join(data)
            print(f"[update/{graph_label}] Completed node: {node_names}")
        elif event_type == "messages":
            message_chunk, metadata = data
            text = message_chunk.text
            if text:
                node = metadata.get("langgraph_node", "unknown")
                message_key = (graph_label, node)
                if active_message != message_key:
                    if active_message is not None:
                        print()
                    print(f"[message/{graph_label}/{node}] ", end="", flush=True)
                    active_message = message_key
                print(text, end="", flush=True)
        elif event_type == "values" and not namespace:
            final_state = data

    if active_message is not None:
        print()

    if final_state is None:
        raise RuntimeError("The parent graph completed without a final state.")

    print("\n\nFinal learning note")
    print("===================")
    print(final_state["learning_note"])
    return final_state


def main() -> None:
    print_workflows()
    print('Streaming API: graph.stream(..., subgraphs=True, version="v2")\n')
    topic = " ".join(sys.argv[1:]) or "LangGraph subgraphs"
    run(topic)


if __name__ == "__main__":
    main()
