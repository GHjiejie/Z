"""FastAPI adapter for the checkpoint conversation service."""

from __future__ import annotations

import json
import os
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

ThreadId = Annotated[
    str,
    ApiPath(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="会话 ID",
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
                    "last_message": _last_message(snapshot),
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
            _serialize_checkpoint(index, snapshot)
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
        "messages": [_serialize_message(message) for message in messages],
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


def _serialize_message(message: BaseMessage) -> dict[str, object]:
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
    return result


def _last_message(snapshot: StateSnapshot) -> dict[str, object] | None:
    messages = snapshot.values.get("messages", [])
    return _serialize_message(messages[-1]) if messages else None


def _serialize_checkpoint(index: int, snapshot: StateSnapshot) -> dict[str, object]:
    messages = snapshot.values.get("messages", [])
    metadata = snapshot.metadata or {}
    return {
        "index": index,
        "checkpoint_id": checkpoint_id(snapshot),
        "created_at": snapshot.created_at,
        "next": list(snapshot.next),
        "message_count": len(messages),
        "turn_count": snapshot.values.get("turn_count", 0),
        "last_message": _serialize_message(messages[-1]) if messages else None,
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
        yield _sse({"type": "start", "thread_id": thread_id})
        chunked_message_ids: set[str] = set()
        try:
            for mode, data in chat.stream(thread_id, graph_input):
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
                content = _chunk_text(message.content)
                if content:
                    yield _sse({"type": "token", "content": content})
            yield _sse(
                {
                    "type": "state",
                    "state": _serialize_state(thread_id, chat.state(thread_id)),
                }
            )
        except Exception as exc:  # noqa: BLE001 - report started stream failure in-band
            yield _sse({"type": "error", "detail": f"模型或图执行失败: {exc}"})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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


def _sse(payload: dict[str, object]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


app = create_api()
