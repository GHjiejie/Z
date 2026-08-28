"""Consume LangGraph v3 projections and expose them to frontends over SSE."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from interleave.main import build_graph

FrontendEvent = dict[str, Any]
EventConsumer = Callable[[Any], Iterator[FrontendEvent]]


class ChatRequest(BaseModel):
    """One stateless chat turn."""

    model_config = ConfigDict(str_strip_whitespace=True)
    message: str = Field(min_length=1, max_length=30_000)


def message_event_consumer(message_stream: Any) -> Iterator[FrontendEvent]:
    """Translate one message projection into ordered frontend content events."""

    message_id = message_stream.message_id
    common = {
        "message_id": message_id,
        "node": message_stream.node,
        "namespace": message_stream.namespace,
    }

    # Raw content-block iteration preserves reasoning, text and tool-call order.
    for event in message_stream:
        event_name = event.get("event")
        if event_name == "message-start":
            message_id = event.get("id") or message_id
            common["message_id"] = message_id
            yield {"type": "message.started", **common, "role": event.get("role")}
            continue

        if event_name == "content-block-start":
            yield {
                "type": "content.started",
                **common,
                "block_index": event.get("index"),
                "content": event.get("content"),
            }
            continue

        if event_name == "content-block-delta":
            delta = event.get("delta") or {}
            delta_type = delta.get("type")
            payload = {
                **common,
                "block_index": event.get("index"),
                "delta": delta,
            }
            if delta_type == "text-delta":
                yield {
                    "type": "text.delta",
                    **payload,
                    "text": delta.get("text", ""),
                }
            elif delta_type == "reasoning-delta":
                yield {
                    "type": "reasoning.delta",
                    **payload,
                    "text": delta.get("reasoning", ""),
                }
            elif _is_tool_call_delta(delta):
                yield {"type": "tool_call.delta", **payload}
            else:
                yield {"type": "content.delta", **payload}
            continue

        if event_name == "content-block-finish":
            yield {
                "type": "content.completed",
                **common,
                "block_index": event.get("index"),
                "content": event.get("content"),
            }
            continue

        if event_name == "message-finish":
            yield {
                "type": "message.completed",
                **common,
                "usage": event.get("usage"),
                "response_metadata": event.get("response_metadata"),
            }
            continue

        if event_name == "error":
            yield {"type": "message.failed", **common, "error": event.get("message")}


def values_event_consumer(state: Any) -> Iterator[FrontendEvent]:
    """Push a complete graph-state snapshot to the frontend."""

    yield {"type": "state.updated", "state": _json_safe(state)}


def lifecycle_event_consumer(event: Any) -> Iterator[FrontendEvent]:
    """Push graph, subgraph and subagent lifecycle changes."""

    yield {"type": "lifecycle.event", "lifecycle": _json_safe(event)}


def tool_event_consumer(event: Any) -> Iterator[FrontendEvent]:
    """Push tool execution events when the graph exposes a tools projection."""

    data = _json_safe(event)
    phase = data.get("event") if isinstance(data, dict) else None
    event_type = {
        "tool-started": "tool.started",
        "tool-output-delta": "tool.output.delta",
        "tool-finished": "tool.completed",
        "tool-error": "tool.failed",
    }.get(phase, "tool.event")
    yield {"type": event_type, "tool": data}


def subgraph_event_consumer(event: Any) -> Iterator[FrontendEvent]:
    """Announce a nested graph handle without serializing its live channels."""

    yield {
        "type": "subgraph.started",
        "subgraph": {
            "path": list(event.path),
            "graph_name": event.graph_name,
            "status": event.status,
            "trigger_call_id": event.trigger_call_id,
        },
    }


FRONTEND_EVENT_CONSUMERS: dict[str, EventConsumer] = {
    "messages": message_event_consumer,
    "values": values_event_consumer,
    "lifecycle": lifecycle_event_consumer,
    "tools": tool_event_consumer,
    "subgraphs": subgraph_event_consumer,
}


def create_api(*, graph: Any | None = None) -> FastAPI:
    """Create the FastAPI service; graph injection keeps tests offline."""

    resolved_graph = graph or build_graph()
    application = FastAPI(
        title="LangGraph Frontend Event API",
        version="1.0.0",
        description="Streams normalized LangGraph events to browser clients over SSE.",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=_frontend_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @application.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/api/chat/stream")
    def stream_chat(body: ChatRequest) -> StreamingResponse:
        return StreamingResponse(
            _stream_frontend_events(resolved_graph, body.message),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    frontend_dist = Path(__file__).resolve().parent / "frontend" / "dist"
    if frontend_dist.is_dir():
        application.mount(
            "/",
            StaticFiles(directory=frontend_dist, html=True),
            name="frontend",
        )

    return application


def _stream_frontend_events(graph: Any, user_input: str) -> Iterator[str]:
    run_id = f"run_{uuid.uuid4().hex}"
    started_at = time.time()
    sequence = 0

    def send(event: FrontendEvent) -> str:
        nonlocal sequence
        sequence += 1
        envelope = {
            "protocol_version": 1,
            "run_id": run_id,
            "sequence": sequence,
            "timestamp": time.time(),
            **event,
        }
        return _sse(envelope)

    yield send(
        {
            "type": "run.started",
            "input": {"role": "user", "content": user_input},
        }
    )

    try:
        with graph.stream_events(
            {"messages": [{"role": "user", "content": user_input}]},
            version="v3",
        ) as stream:
            event_types = tuple(
                event_type
                for event_type in FRONTEND_EVENT_CONSUMERS
                if event_type in stream.extensions
            )
            for event_type, event in stream.interleave(*event_types):
                consumer = FRONTEND_EVENT_CONSUMERS[event_type]
                for frontend_event in consumer(event):
                    yield send(frontend_event)

            if stream.interrupted:
                yield send(
                    {
                        "type": "run.interrupted",
                        "interrupts": _json_safe(stream.interrupts),
                        "output": _json_safe(stream.output),
                    }
                )
            else:
                yield send(
                    {
                        "type": "run.completed",
                        "duration_ms": round((time.time() - started_at) * 1000),
                        "output": _json_safe(stream.output),
                    }
                )
    except GeneratorExit:
        raise
    except Exception as exc:  # noqa: BLE001
        # The HTTP response has already started, so errors travel inside SSE.
        yield send(
            {
                "type": "run.failed",
                "duration_ms": round((time.time() - started_at) * 1000),
                "error": {"name": type(exc).__name__, "message": str(exc)},
            }
        )


def _is_tool_call_delta(delta: dict[str, Any]) -> bool:
    if delta.get("type") != "block-delta":
        return False
    fields = delta.get("fields") or {}
    return fields.get("type") in {"tool_call_chunk", "server_tool_call_chunk"}


def _json_safe(value: Any) -> Any:
    return jsonable_encoder(value)


def _sse(event: FrontendEvent) -> str:
    return (
        f"event: {event['type']}\n"
        f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


def _frontend_origins() -> list[str]:
    configured = os.getenv("FRONTEND_ORIGINS")
    if configured:
        return [origin.strip() for origin in configured.split(",") if origin.strip()]
    return [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
