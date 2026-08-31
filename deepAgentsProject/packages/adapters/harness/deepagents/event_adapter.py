from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from packages.coding.redaction import redact_text
from packages.runtime.event_emitter import EventEmitter


@dataclass
class DeepAgentsEventAdapter:
    """Translate real LangGraph stream parts into the platform event contract."""

    events: EventEmitter
    run: dict[str, Any]
    model_identity: dict[str, Any]
    output_parts: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    subagent_calls: int = 0
    interrupt: dict[str, Any] | None = None
    _model_ids: set[str] = field(default_factory=set)
    _completed_model_ids: set[str] = field(default_factory=set)
    _tool_call_ids: set[str] = field(default_factory=set)
    _tool_result_ids: set[str] = field(default_factory=set)
    _nodes: set[str] = field(default_factory=set)

    def consume(self, part: dict[str, Any]) -> None:
        part_type = part.get("type")
        namespace = tuple(part.get("ns") or ())
        if part_type == "messages":
            message, metadata = part.get("data") or (None, {})
            self._consume_message(message, metadata or {}, namespace)
        elif part_type == "updates":
            self._consume_update(part.get("data") or {}, namespace)

    def _consume_message(
        self, message: Any, metadata: dict[str, Any], namespace: tuple[str, ...]
    ) -> None:
        node = str(metadata.get("langgraph_node") or "agent")
        path = self._path(namespace, node)
        if node not in self._nodes:
            self._nodes.add(node)
            self._emit(
                "graph.node.started",
                {"node_id": node, "node_name": node},
                span_id=f"span_{_safe(node)}",
                execution_path=path,
            )

        if isinstance(message, (AIMessage, AIMessageChunk)):
            message_id = str(message.id or f"model-{len(self._model_ids) + 1}")
            if message_id not in self._model_ids:
                self._model_ids.add(message_id)
                self.model_calls += 1
                self._emit(
                    "model.started",
                    {**self.model_identity, "message_id": message_id, "node": node},
                    span_id=f"span_model_{_safe(message_id)}",
                    execution_path=path,
                )
            text = _message_text(message.content)
            if text:
                text = redact_text(text)
                self.output_parts.append(text)
                self._emit(
                    "model.delta",
                    {"delta": text, "message_id": message_id},
                    span_id=f"span_model_{_safe(message_id)}",
                    execution_path=path,
                )
            for call in getattr(message, "tool_calls", []) or []:
                call_id = str(call.get("id") or "")
                if not call_id or call_id in self._tool_call_ids:
                    continue
                self._tool_call_ids.add(call_id)
                self.tool_calls += 1
                name = str(call.get("name") or "unknown")
                if name == "task":
                    self.subagent_calls += 1
                    self._emit(
                        "subagent.started",
                        {"tool_call_id": call_id, "arguments": _safe_arguments(call.get("args"))},
                        span_id=f"span_tool_{_safe(call_id)}",
                        execution_path=path,
                    )
                self._emit(
                    "tool.requested",
                    {
                        "tool_call_id": call_id,
                        "tool_name": name,
                        "arguments": _safe_arguments(call.get("args")),
                    },
                    span_id=f"span_tool_{_safe(call_id)}",
                    execution_path=path,
                )
            usage = getattr(message, "usage_metadata", None) or {}
            self.input_tokens += int(usage.get("input_tokens") or 0)
            self.output_tokens += int(usage.get("output_tokens") or 0)
            response_metadata = getattr(message, "response_metadata", None) or {}
            finished = (
                (isinstance(message, AIMessage) and not isinstance(message, AIMessageChunk))
                or response_metadata.get("finish_reason") is not None
                or getattr(message, "chunk_position", None) == "last"
            )
            if finished and message_id not in self._completed_model_ids:
                self._completed_model_ids.add(message_id)
                self._emit(
                    "model.completed",
                    {
                        **self.model_identity,
                        "message_id": message_id,
                        "input_tokens": int(usage.get("input_tokens") or 0),
                        "output_tokens": int(usage.get("output_tokens") or 0),
                        "finish_reason": response_metadata.get("finish_reason"),
                    },
                    span_id=f"span_model_{_safe(message_id)}",
                    execution_path=path,
                )
            return

        if isinstance(message, ToolMessage):
            call_id = str(message.tool_call_id or message.id or "")
            if call_id in self._tool_result_ids:
                return
            self._tool_result_ids.add(call_id)
            status = getattr(message, "status", None) or "success"
            name = str(message.name or "unknown")
            content = _message_text(message.content)
            content = redact_text(content)
            self._emit(
                "tool.failed" if status == "error" else "tool.completed",
                {
                    "tool_call_id": call_id,
                    "tool_name": name,
                    "status": status,
                    "result_preview": content[:2000],
                    "truncated": len(content) > 2000,
                },
                span_id=f"span_tool_{_safe(call_id)}",
                execution_path=path,
            )
            if name == "task":
                self._emit(
                    "subagent.completed" if status != "error" else "subagent.failed",
                    {"tool_call_id": call_id, "status": status, "result_preview": content[:1000]},
                    span_id=f"span_tool_{_safe(call_id)}",
                    execution_path=path,
                )
            if name == "write_todos" and content:
                self._emit(
                    "todo.updated",
                    {"source": "write_todos", "result_preview": content[:2000]},
                    execution_path=path,
                )

    def _consume_update(
        self, update: dict[str, Any], namespace: tuple[str, ...]
    ) -> None:
        raw_interrupts = update.get("__interrupt__")
        if raw_interrupts:
            first = raw_interrupts[0]
            value = getattr(first, "value", None) or {}
            self.interrupt = {
                "langgraph_interrupt_id": getattr(first, "id", None),
                "action_requests": list(value.get("action_requests") or []),
                "review_configs": list(value.get("review_configs") or []),
            }
            return
        for node, data in update.items():
            if node.startswith("__"):
                continue
            self._emit(
                "graph.node.completed",
                {"node_id": node, "status": "completed", "has_update": data is not None},
                span_id=f"span_{_safe(node)}",
                execution_path=self._path(namespace, node),
            )

    @property
    def output(self) -> str:
        return "".join(self.output_parts).strip()

    def restore_output(self, messages: list[Any], checkpoint_id: str) -> None:
        """Recover a completed answer without inventing another model call."""
        if self.output_parts:
            return
        for message in reversed(messages):
            if isinstance(message, AIMessage):
                if message.tool_calls:
                    return
                text = redact_text(_message_text(message.content))
                if text:
                    self.output_parts.append(text)
                    self._emit("model.output.restored", {"checkpoint_id": checkpoint_id,
                                                         "message_id": message.id})
                return

    def _emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        span_id: str | None = None,
        execution_path: list[str] | None = None,
    ) -> None:
        self.events.append(
            self.run["id"],
            event_type,
            payload,
            span_id=span_id,
            parent_span_id="span_main" if span_id != "span_main" else None,
            execution_path=execution_path or ["main"],
        )

    @staticmethod
    def _path(namespace: tuple[str, ...], node: str) -> list[str]:
        segments = [str(part).split(":", 1)[0] for part in namespace]
        return ["main", *segments, node]


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return ""


def _safe_arguments(arguments: Any) -> Any:
    if not isinstance(arguments, dict):
        return arguments
    safe = {}
    for key, value in arguments.items():
        if key in {"content", "old_string", "new_string"} and isinstance(value, str):
            safe[key] = {
                "characters": len(value),
                "sha256": __import__("hashlib").sha256(value.encode()).hexdigest(),
            }
        elif isinstance(value, str):
            safe[key] = redact_text(value)
        else:
            safe[key] = value
    # Ensure unusual provider values cannot break event serialization.
    json.dumps(safe, default=str)
    return safe


def _safe(value: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in value)[:80]
