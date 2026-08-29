"""Consume interleaved v3 events and expose frontend-friendly feedback."""

import sys
from collections.abc import Callable
from typing import Any, Literal, NotRequired, TypedDict

from langgraph.stream import GraphRunStream, SubgraphRunStream

from subgraph.graph import ParentState, graph, print_workflows


class FrontendEvent(TypedDict):
    """Serializable feedback that a terminal, SSE, or WebSocket UI can render."""

    type: Literal["workflow", "lifecycle", "activity", "output", "state", "result"]
    status: Literal["running", "streaming", "updated", "completed", "failed"]
    scope: str
    message: str
    node: NotRequired[str]
    delta: NotRequired[str]
    output: NotRequired[str]
    state: NotRequired[dict[str, Any]]


FeedbackHandler = Callable[[FrontendEvent], None]


class TerminalFeedback:
    """Render frontend feedback events as readable live terminal output."""

    def __init__(self) -> None:
        self._active_output: tuple[str, str] | None = None

    def _close_output(self) -> None:
        if self._active_output is not None:
            print()
            self._active_output = None

    def __call__(self, event: FrontendEvent) -> None:
        if event["type"] == "output":
            output_key = (event["scope"], event.get("node", "unknown"))
            if self._active_output != output_key:
                self._close_output()
                print(
                    f"[output/{output_key[0]}/{output_key[1]}] ",
                    end="",
                    flush=True,
                )
                self._active_output = output_key
            print(event.get("delta", ""), end="", flush=True)
            return

        self._close_output()
        if event["type"] == "result":
            print("\nFinal learning note")
            print("===================")
            print(event.get("output", ""))
            return

        node = f"/{event['node']}" if event.get("node") else ""
        print(
            f"[{event['type']}/{event['scope']}{node}] "
            f"{event['status']}: {event['message']}"
        )


def scope_label(path: tuple[str, ...] | list[str]) -> str:
    """Convert checkpoint namespaces into stable frontend scope labels."""

    if not path:
        return "parent"
    return " > ".join(part.split(":", maxsplit=1)[0] for part in path)


def consume_lifecycle(event: dict[str, Any], feedback: FeedbackHandler) -> None:
    """Translate a lifecycle projection into start/completion UI feedback."""

    status = event["event"]
    scope = scope_label(event.get("namespace", []))
    graph_name = event.get("graph_name") or scope.rsplit(" > ", maxsplit=1)[-1]
    frontend_status: Literal["running", "completed", "failed"]
    frontend_status = "running" if status == "started" else "completed"
    if status in {"failed", "interrupted", "drained"}:
        frontend_status = "failed"

    feedback(
        {
            "type": "lifecycle",
            "status": frontend_status,
            "scope": scope,
            "message": f"Subgraph {graph_name} {status}.",
        }
    )


def consume_values(
    state: dict[str, Any], scope: str, feedback: FeedbackHandler
) -> None:
    """Expose state availability without dumping private intermediate content."""

    available = [key for key, value in state.items() if value not in (None, "")]
    feedback(
        {
            "type": "state",
            "status": "updated",
            "scope": scope,
            "message": f"Available state: {', '.join(available) or 'none'}.",
            "state": {
                "available_fields": available,
                "has_output": bool(state.get("learning_note")),
            },
        }
    )


def consume_message(message_stream: Any, scope: str, feedback: FeedbackHandler) -> None:
    """Stream one model call as activity and output-delta feedback."""

    node = message_stream.node or "unknown"
    feedback(
        {
            "type": "activity",
            "status": "running",
            "scope": scope,
            "node": node,
            "message": f"Agent started {node}.",
        }
    )
    for text in message_stream.text:
        feedback(
            {
                "type": "output",
                "status": "streaming",
                "scope": scope,
                "node": node,
                "message": f"Agent output from {node}.",
                "delta": text,
            }
        )
    feedback(
        {
            "type": "activity",
            "status": "completed",
            "scope": scope,
            "node": node,
            "message": f"Agent completed {node}.",
        }
    )


def consume_subgraph(
    subgraph_stream: SubgraphRunStream, feedback: FeedbackHandler
) -> None:
    """Attach to a discovered child and recursively consume all its events."""

    child_scope = scope_label(subgraph_stream.path)
    feedback(
        {
            "type": "workflow",
            "status": "running",
            "scope": child_scope,
            "message": (
                f"Attached to {subgraph_stream.graph_name or 'subgraph'} "
                "event stream."
            ),
        }
    )
    consume_run_stream(subgraph_stream, child_scope, feedback)


def consume_run_stream(
    stream: GraphRunStream | SubgraphRunStream,
    scope: str,
    feedback: FeedbackHandler,
) -> None:
    """Consume all native projections in their real arrival order."""

    event_consumers: dict[str, Callable[[Any], None]] = {
        "lifecycle": lambda event: consume_lifecycle(event, feedback),
        "subgraphs": lambda event: consume_subgraph(event, feedback),
        "messages": lambda event: consume_message(event, scope, feedback),
        "values": lambda event: consume_values(event, scope, feedback),
    }

    for event_type, event in stream.interleave(*event_consumers):
        event_consumers[event_type](event)


def run(topic: str, feedback: FeedbackHandler | None = None) -> ParentState:
    """Run v3 and publish interleaved progress suitable for a frontend."""

    publish = feedback or TerminalFeedback()
    publish(
        {
            "type": "workflow",
            "status": "running",
            "scope": "parent",
            "message": "Agent workflow started.",
        }
    )

    with graph.stream_events(
        {"topic": topic, "learning_note": ""},
        version="v3",
    ) as stream:
        consume_run_stream(stream, "parent", publish)
        final_state = stream.output

    if final_state is None:
        raise RuntimeError("The parent graph completed without a final state.")

    publish(
        {
            "type": "result",
            "status": "completed",
            "scope": "parent",
            "message": "Agent workflow completed.",
            "output": final_state["learning_note"],
        }
    )
    return final_state


def main() -> None:
    print_workflows()
    print(
        'Streaming API: graph.stream_events(..., version="v3") '
        '+ stream.interleave(...)\n'
    )
    topic = " ".join(sys.argv[1:]) or "LangGraph subgraphs"
    run(topic)


if __name__ == "__main__":
    main()
