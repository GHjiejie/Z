"""Stream parent-graph and subgraph events with protocol version 3."""

import sys

from subgraph.graph import ParentState, graph, print_workflows


def run(topic: str) -> ParentState:
    """Run ``graph.stream_events`` with v3 and consume subgraph streams."""

    with graph.stream_events(
        {"topic": topic, "learning_note": ""},
        version="v3",
    ) as stream:
        for subgraph_stream in stream.subgraphs:
            path = " > ".join(
                part.split(":", maxsplit=1)[0] for part in subgraph_stream.path
            )
            name = subgraph_stream.graph_name or "subgraph"
            print(f"[subgraph] Started {name} at {path}")

            for message_stream in subgraph_stream.messages:
                print(f"[message/{message_stream.node}] ", end="", flush=True)
                for text in message_stream.text:
                    print(text, end="", flush=True)
                print()

            print(f"[subgraph] Completed {name}")

        final_state = stream.output

    if final_state is None:
        raise RuntimeError("The parent graph completed without a final state.")

    print("\nFinal learning note")
    print("===================")
    print(final_state["learning_note"])
    return final_state


def main() -> None:
    print_workflows()
    print('Streaming API: graph.stream_events(..., version="v3")\n')
    topic = " ".join(sys.argv[1:]) or "LangGraph subgraphs"
    run(topic)


if __name__ == "__main__":
    main()
