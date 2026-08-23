"""FastAPI adapter for the checkpoint conversation service."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Request, status
from fastapi import Path as ApiPath
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langgraph.types import Command, StateSnapshot
from pydantic import BaseModel, ConfigDict, Field

from checkpoint_project.graph import (
    CheckpointChatApp,
    checkpoint_id,
    iter_interrupts,
)
from checkpoint_project.model import build_model

STREAM_HEARTBEAT_SECONDS = 1.0

ThreadId = Annotated[
    str,
    ApiPath(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="会话 ID",
    ),
]

ArtifactId = Annotated[
    str,
    ApiPath(
        min_length=5,
        max_length=64,
        pattern=r"^art_[A-Fa-f0-9]+$",
        description="Artifact ID",
    ),
]


class SessionCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    thread_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )


class MessageCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    content: str = Field(min_length=1, max_length=30_000)


class ApprovalDecision(BaseModel):
    approved: bool


class ForkCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    checkpoint_id: str = Field(min_length=1, max_length=128)
    new_thread_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )


def create_api(
    *,
    model: BaseChatModel | None = None,
    db_path: Path | None = None,
    workspace: Path | None = None,
) -> FastAPI:
    """Create an API app; injectable arguments keep integration tests offline."""
    project_dir = Path(__file__).resolve().parent
    resolved_db = db_path or Path(
        os.getenv(
            "CHECKPOINT_DB",
            project_dir / "data" / "checkpoints.sqlite",
        )
    )
    resolved_workspace = workspace or Path(
        os.getenv(
            "CHECKPOINT_WORKSPACE",
            project_dir / "workspace",
        )
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        chat = CheckpointChatApp(
            model or build_model(),
            db_path=resolved_db,
            workspace=resolved_workspace,
        )
        application.state.chat = chat
        try:
            yield
        finally:
            chat.close()

    application = FastAPI(
        title="Checkpoint Studio API",
        version="1.0.0",
        description="SQLite-backed LangGraph conversations, approvals and forks.",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.exception_handler(RuntimeError)
    async def runtime_error_handler(
        _request: Request, exc: RuntimeError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": str(exc)},
        )

    @application.get("/api/health")
    def health(request: Request) -> dict[str, object]:
        chat = _chat(request)
        return {
            "status": "ok",
            "database": str(chat.db_path),
            "workspace": str(chat.files.root),
        }

    @application.get("/api/sessions")
    def list_sessions(request: Request) -> list[dict[str, object]]:
        chat = _chat(request)
        result: list[dict[str, object]] = []
        for session in reversed(chat.sessions.list()):
            snapshot = chat.state(session.thread_id)
            result.append(
                {
                    "thread_id": session.thread_id,
                    "created_at": session.created_at,
                    "source_thread_id": session.source_thread_id,
                    "source_checkpoint_id": session.source_checkpoint_id,
                    "message_count": len(snapshot.values.get("messages", [])),
                    "status": _state_status(snapshot),
                    "last_message": _last_message(snapshot, session.thread_id),
                }
            )
        return result

    @application.post("/api/sessions", status_code=status.HTTP_201_CREATED)
    def create_session(body: SessionCreate, request: Request) -> dict[str, object]:
        chat = _chat(request)
        thread_id = body.thread_id or _new_thread_id()
        if chat.sessions.exists(thread_id):
            raise HTTPException(status_code=409, detail="会话 ID 已存在")
        chat.sessions.ensure(thread_id)
        return _serialize_state(thread_id, chat.state(thread_id))

    @application.get("/api/sessions/{thread_id}")
    def get_session(thread_id: ThreadId, request: Request) -> dict[str, object]:
        chat = _chat(request)
        _require_session(chat, thread_id)
        return _serialize_state(thread_id, chat.state(thread_id))

    @application.post("/api/sessions/{thread_id}/messages")
    def send_message(
        thread_id: ThreadId,
        body: MessageCreate,
        request: Request,
    ) -> dict[str, object]:
        chat = _chat(request)
        _require_session(chat, thread_id)
        current = chat.state(thread_id)
        if list(iter_interrupts(current)):
            raise HTTPException(status_code=409, detail="请先处理待审批操作")
        if current.next:
            raise HTTPException(status_code=409, detail="会话有待恢复任务，请先重试")
        try:
            chat.invoke(
                thread_id,
                {"messages": [HumanMessage(content=body.content)]},
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"模型或图执行失败: {exc}",
            ) from exc
        return _serialize_state(thread_id, chat.state(thread_id))

    @application.post("/api/sessions/{thread_id}/messages/stream")
    def stream_message(
        thread_id: ThreadId,
        body: MessageCreate,
        request: Request,
    ) -> StreamingResponse:
        chat = _chat(request)
        _require_session(chat, thread_id)
        current = chat.state(thread_id)
        if list(iter_interrupts(current)):
            raise HTTPException(status_code=409, detail="请先处理待审批操作")
        if current.next:
            raise HTTPException(status_code=409, detail="会话有待恢复任务，请先重试")
        return _stream_response(
            chat,
            thread_id,
            {"messages": [HumanMessage(content=body.content)]},
        )

    @application.post("/api/sessions/{thread_id}/approval")
    def decide_approval(
        thread_id: ThreadId,
        body: ApprovalDecision,
        request: Request,
    ) -> dict[str, object]:
        chat = _chat(request)
        _require_session(chat, thread_id)
        if not list(iter_interrupts(chat.state(thread_id))):
            raise HTTPException(status_code=409, detail="当前没有待审批操作")
        try:
            chat.resume(thread_id, body.approved)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"恢复审批失败: {exc}") from exc
        return _serialize_state(thread_id, chat.state(thread_id))

    @application.post("/api/sessions/{thread_id}/approval/stream")
    def stream_approval(
        thread_id: ThreadId,
        body: ApprovalDecision,
        request: Request,
    ) -> StreamingResponse:
        chat = _chat(request)
        _require_session(chat, thread_id)
        if not list(iter_interrupts(chat.state(thread_id))):
            raise HTTPException(status_code=409, detail="当前没有待审批操作")
        return _stream_response(
            chat,
            thread_id,
            Command(resume={"approved": body.approved}),
        )

    @application.post("/api/sessions/{thread_id}/retry")
    def retry(thread_id: ThreadId, request: Request) -> dict[str, object]:
        chat = _chat(request)
        _require_session(chat, thread_id)
        snapshot = chat.state(thread_id)
        if list(iter_interrupts(snapshot)):
            raise HTTPException(status_code=409, detail="待审批操作不能通过重试跳过")
        if not snapshot.next:
            raise HTTPException(status_code=409, detail="当前没有待恢复任务")
        try:
            chat.invoke(thread_id, None)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"重试失败: {exc}") from exc
        return _serialize_state(thread_id, chat.state(thread_id))

    @application.post("/api/sessions/{thread_id}/retry/stream")
    def stream_retry(thread_id: ThreadId, request: Request) -> StreamingResponse:
        chat = _chat(request)
        _require_session(chat, thread_id)
        snapshot = chat.state(thread_id)
        if list(iter_interrupts(snapshot)):
            raise HTTPException(status_code=409, detail="待审批操作不能通过重试跳过")
        if not snapshot.next:
            raise HTTPException(status_code=409, detail="当前没有待恢复任务")
        return _stream_response(chat, thread_id, None)

    @application.get("/api/sessions/{thread_id}/checkpoints")
    def list_checkpoints(
        thread_id: ThreadId,
        request: Request,
    ) -> list[dict[str, object]]:
        chat = _chat(request)
        _require_session(chat, thread_id)
        return [
            _serialize_checkpoint(index, snapshot, thread_id)
            for index, snapshot in enumerate(chat.history(thread_id))
        ]

    @application.post(
        "/api/sessions/{thread_id}/fork",
        status_code=status.HTTP_201_CREATED,
    )
    def fork_session(
        thread_id: ThreadId,
        body: ForkCreate,
        request: Request,
    ) -> dict[str, object]:
        chat = _chat(request)
        _require_session(chat, thread_id)
        new_thread_id = body.new_thread_id or _new_thread_id("branch")
        try:
            snapshot = chat.fork(thread_id, body.checkpoint_id, new_thread_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _serialize_state(new_thread_id, snapshot)

    @application.get("/api/sessions/{thread_id}/artifacts")
    def list_artifacts(
        thread_id: ThreadId,
        request: Request,
    ) -> list[dict[str, object]]:
        chat = _chat(request)
        _require_session(chat, thread_id)
        return [
            _serialize_artifact_ref(artifact.public_ref(), thread_id)
            for artifact in chat.artifacts.list_for_session(thread_id)
        ]

    @application.get("/api/sessions/{thread_id}/artifacts/{artifact_id}")
    def get_artifact(
        thread_id: ThreadId,
        artifact_id: ArtifactId,
        request: Request,
    ) -> dict[str, object]:
        chat = _chat(request)
        _require_session(chat, thread_id)
        artifact = chat.artifacts.get(thread_id, artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="Artifact 不存在")
        return {
            **artifact.public_record(),
            "content_url": _artifact_content_url(thread_id, artifact.artifact_id),
        }

    frontend_dist = project_dir / "frontend" / "dist"
    if frontend_dist.is_dir():
        application.mount(
            "/",
            StaticFiles(directory=frontend_dist, html=True),
            name="frontend",
        )

    return application


def _chat(request: Request) -> CheckpointChatApp:
    return request.app.state.chat


def _require_session(chat: CheckpointChatApp, thread_id: str) -> None:
    if not chat.sessions.exists(thread_id):
        raise HTTPException(status_code=404, detail="会话不存在")


def _new_thread_id(prefix: str = "session") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _state_status(snapshot: StateSnapshot) -> str:
    if list(iter_interrupts(snapshot)):
        return "waiting_approval"
    if snapshot.next:
        return "recoverable"
    return "idle"


def _serialize_state(thread_id: str, snapshot: StateSnapshot) -> dict[str, object]:
    messages = snapshot.values.get("messages", [])
    return {
        "thread_id": thread_id,
        "checkpoint_id": (
            checkpoint_id(snapshot) if snapshot.metadata is not None else None
        ),
        "messages": [_serialize_message(message, thread_id) for message in messages],
        "turn_count": snapshot.values.get("turn_count", 0),
        "next": list(snapshot.next),
        "status": _state_status(snapshot),
        "pending_approvals": [
            {
                "id": getattr(item, "id", None),
                "payload": getattr(item, "value", item),
            }
            for item in iter_interrupts(snapshot)
        ],
    }


def _serialize_message(
    message: BaseMessage,
    thread_id: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "id": message.id,
        "type": message.type,
        "content": message.content,
    }
    if isinstance(message, AIMessage):
        result["tool_calls"] = message.tool_calls
    if isinstance(message, ToolMessage):
        result.update(
            {
                "tool_call_id": message.tool_call_id,
                "name": message.name,
                "tool_status": message.status,
            }
        )
        if isinstance(message.artifact, dict):
            artifact = _serialize_artifact_ref(message.artifact, thread_id)
            if artifact:
                result["artifact"] = artifact
    return result


def _last_message(
    snapshot: StateSnapshot,
    thread_id: str | None = None,
) -> dict[str, object] | None:
    messages = snapshot.values.get("messages", [])
    return _serialize_message(messages[-1], thread_id) if messages else None


def _serialize_checkpoint(
    index: int,
    snapshot: StateSnapshot,
    thread_id: str,
) -> dict[str, object]:
    messages = snapshot.values.get("messages", [])
    metadata = snapshot.metadata or {}
    return {
        "index": index,
        "checkpoint_id": checkpoint_id(snapshot),
        "created_at": snapshot.created_at,
        "next": list(snapshot.next),
        "message_count": len(messages),
        "turn_count": snapshot.values.get("turn_count", 0),
        "last_message": (
            _serialize_message(messages[-1], thread_id) if messages else None
        ),
        "has_interrupt": bool(list(iter_interrupts(snapshot))),
        "step": metadata.get("step"),
        "source": metadata.get("source"),
    }


def _stream_response(
    chat: CheckpointChatApp,
    thread_id: str,
    graph_input: object,
) -> StreamingResponse:
    """Convert LangGraph message chunks and the final state to SSE events."""

    def generate() -> Iterator[str]:
        run_id = f"run_{uuid.uuid4().hex}"
        started_at = time.monotonic()
        yield _sse(
            {
                "type": "start",
                "protocol_version": 2,
                "run_id": run_id,
                "thread_id": thread_id,
            }
        )
        phase = "accepted"
        phase_message = "后端已接收请求，正在调用模型…"
        yield _sse(
            _progress_event(
                run_id,
                phase,
                phase_message,
                started_at,
            )
        )
        last_progress_at = time.monotonic()
        chunked_message_ids: set[str] = set()
        inside_thought = False
        active_tool = ""
        graph_events: queue.Queue[tuple[str, object]] = queue.Queue()

        def run_graph() -> None:
            try:
                for item in chat.stream(thread_id, graph_input):
                    graph_events.put(("event", item))
            except BaseException as exc:  # noqa: BLE001 - forward to SSE thread
                graph_events.put(("error", exc))
            finally:
                graph_events.put(("done", None))

        threading.Thread(
            target=run_graph,
            name=f"checkpoint-stream-{run_id[-8:]}",
            daemon=True,
        ).start()

        try:
            while True:
                try:
                    event_kind, payload = graph_events.get(
                        timeout=STREAM_HEARTBEAT_SECONDS
                    )
                except queue.Empty:
                    yield _sse(
                        _progress_event(
                            run_id,
                            phase,
                            phase_message,
                            started_at,
                            heartbeat=True,
                        )
                    )
                    last_progress_at = time.monotonic()
                    continue

                if event_kind == "error":
                    if isinstance(payload, BaseException):
                        raise payload
                    raise RuntimeError("未知图执行错误")
                if event_kind == "done":
                    break
                if not isinstance(payload, tuple) or len(payload) != 2:
                    continue
                mode, data = payload
                if mode == "custom":
                    event = _serialize_custom_event(data, thread_id, run_id)
                    if event:
                        yield _sse(event)
                        phase = "finalizing"
                        phase_message = "实时预览已生成，正在同步会话…"
                        yield _sse(
                            _progress_event(
                                run_id,
                                phase,
                                phase_message,
                                started_at,
                            )
                        )
                        last_progress_at = time.monotonic()
                    continue
                if mode != "messages":
                    continue
                message, _metadata = data
                if isinstance(message, AIMessageChunk):
                    if message.id:
                        chunked_message_ids.add(message.id)
                elif not isinstance(message, AIMessage) or (
                    message.id and message.id in chunked_message_ids
                ):
                    continue

                tool_names: list[str] = []
                has_tool_activity = False
                if isinstance(message, AIMessageChunk):
                    has_tool_activity = bool(message.tool_call_chunks)
                    tool_names.extend(
                        str(chunk.get("name"))
                        for chunk in message.tool_call_chunks
                        if chunk.get("name")
                    )
                else:
                    has_tool_activity = bool(message.tool_calls)
                    tool_names.extend(
                        str(call.get("name"))
                        for call in message.tool_calls
                        if call.get("name")
                    )
                if tool_names:
                    active_tool = tool_names[-1]
                    phase, phase_message = _tool_progress(active_tool)
                elif has_tool_activity and active_tool:
                    phase, phase_message = _tool_progress(active_tool)

                content = _chunk_text(message.content)
                if content:
                    lowered = content.lower()
                    if "<think>" in lowered:
                        inside_thought = True
                    if "</think>" in lowered:
                        inside_thought = False
                        after_thought = lowered.rsplit("</think>", 1)[-1].strip()
                        if not after_thought and not tool_names:
                            phase = "organizing"
                            phase_message = "分析完成，正在组织回答…"
                    elif inside_thought:
                        phase = "thinking"
                        phase_message = "模型正在分析你的请求…"
                    elif not tool_names:
                        phase = "responding"
                        phase_message = "模型正在生成回答…"

                now = time.monotonic()
                if now - last_progress_at >= STREAM_HEARTBEAT_SECONDS or tool_names:
                    yield _sse(
                        _progress_event(
                            run_id,
                            phase,
                            phase_message,
                            started_at,
                            heartbeat=now - last_progress_at
                            >= STREAM_HEARTBEAT_SECONDS,
                        )
                    )
                    last_progress_at = now

                if content:
                    yield _sse(
                        {
                            "type": "token",
                            "run_id": run_id,
                            "content": content,
                        }
                    )
            phase = "finalizing"
            phase_message = "模型响应完成，正在保存 checkpoint…"
            yield _sse(
                _progress_event(
                    run_id,
                    phase,
                    phase_message,
                    started_at,
                )
            )
            yield _sse(
                {
                    "type": "state",
                    "run_id": run_id,
                    "state": _serialize_state(thread_id, chat.state(thread_id)),
                }
            )
        except Exception as exc:  # noqa: BLE001 - report started stream failure in-band
            yield _sse(
                {
                    "type": "error",
                    "run_id": run_id,
                    "code": "GRAPH_EXECUTION_FAILED",
                    "detail": f"模型或图执行失败: {exc}",
                }
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _progress_event(
    run_id: str,
    phase: str,
    message: str,
    started_at: float,
    *,
    heartbeat: bool = False,
) -> dict[str, object]:
    return {
        "type": "progress",
        "run_id": run_id,
        "phase": phase,
        "message": message,
        "elapsed_ms": max(0, round((time.monotonic() - started_at) * 1000)),
        "heartbeat": heartbeat,
    }


def _tool_progress(tool_name: str) -> tuple[str, str]:
    if tool_name == "render_html":
        return "preparing_preview", "正在生成实时预览内容…"
    if tool_name in {"write_file", "delete_file"}:
        return "preparing_action", "正在准备文件操作…"
    return "preparing_action", "正在准备可执行操作…"


def _chunk_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def _serialize_custom_event(
    data: object,
    thread_id: str,
    run_id: str,
) -> dict[str, object] | None:
    """Whitelist custom graph events before exposing them to the browser."""
    if not isinstance(data, dict) or data.get("type") != "artifact_ready":
        return None
    artifact = data.get("artifact")
    if not isinstance(artifact, dict):
        return None
    reference = _serialize_artifact_ref(artifact, thread_id)
    if not reference:
        return None
    return {
        "type": "artifact_ready",
        "run_id": run_id,
        "artifact": reference,
    }


def _serialize_artifact_ref(
    value: dict[str, object],
    thread_id: str | None,
) -> dict[str, object]:
    """Return only the public artifact metadata understood by the frontend."""
    artifact_id = value.get("artifact_id")
    kind = value.get("kind")
    title = value.get("title")
    if not all(isinstance(item, str) and item for item in (artifact_id, kind, title)):
        return {}
    result = {
        key: value.get(key)
        for key in (
            "artifact_id",
            "kind",
            "mime_type",
            "title",
            "byte_size",
            "parent_artifact_id",
            "created_at",
        )
    }
    if thread_id:
        result["content_url"] = _artifact_content_url(thread_id, artifact_id)
    return result


def _artifact_content_url(thread_id: str, artifact_id: str) -> str:
    return f"/api/sessions/{thread_id}/artifacts/{artifact_id}"


def _sse(payload: dict[str, object]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


app = create_api()
