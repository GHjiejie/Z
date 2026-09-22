"""HTTP service. Start explicitly after database migrations."""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

import anyio
from fastapi import Depends, FastAPI, File, Header, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from llamaindex_service.api.middleware import BodyTooLarge, RequestSizeLimit
from llamaindex_service.api.schemas import (
    AnswerRequest,
    ConversationCreate,
    DocumentPermissions,
    FeedbackCreate,
    GenerationCreate,
    KnowledgeBaseCreate,
    KnowledgeBaseUpdate,
    KnowledgePermissions,
    SearchRequest,
)
from llamaindex_service.auth import authenticate
from llamaindex_service.config import Settings
from llamaindex_service.contracts import AuthContext, ServiceError

logger = logging.getLogger("llamaindex_service")


def public_job(job: dict) -> dict:
    return {
        k: v
        for k, v in job.items()
        if k not in {"claim_token", "object_key", "worker_id"}
    }


def create_app(
    settings: Settings | None = None, repository=None, storage=None, rag=None
) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal repository, storage, rag
        from llamaindex_service.generation.service import RAGService
        from llamaindex_service.persistence import Repository
        from llamaindex_service.storage import build_storage

        owned_repository = repository is None
        repository = repository if repository is not None else Repository(settings)
        if settings.auto_migrate:
            await run_in_threadpool(repository.create_schema)
        if settings.environment == "production" and not await run_in_threadpool(
            repository.health
        ):
            raise RuntimeError("Production database schema or role is not ready")
        storage = storage if storage is not None else build_storage(settings)
        rag = rag if rag is not None else RAGService(settings, repository)
        app.state.repository, app.state.storage, app.state.rag = (
            repository,
            storage,
            rag,
        )
        try:
            yield
        finally:
            if owned_repository:
                await run_in_threadpool(repository.engine.dispose)

    app = FastAPI(title="LlamaIndex 企业知识库", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(
        RequestSizeLimit, max_bytes=settings.max_upload_bytes + 256 * 1024
    )

    @app.middleware("http")
    async def request_trace(request: Request, call_next):
        request.state.request_id = str(uuid4())
        started = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        # Paths may contain resource identifiers, never queries or request bodies.
        route = request.scope.get("route")
        logger.info(
            "http request_id=%s route=%s status=%s duration_ms=%.1f",
            request.state.request_id,
            getattr(route, "path", "unmatched"),
            response.status_code,
            (time.monotonic() - started) * 1000,
        )
        return response

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        headers = {"Retry-After": "60"} if exc.status_code == 429 else None
        return JSONResponse(
            status_code=exc.status_code,
            headers=headers,
            content={
                "code": exc.code,
                "message": exc.message,
                "request_id": request.state.request_id,
                "details": exc.details,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Pydantic errors can contain the original user input; do not echo it.
        return JSONResponse(
            status_code=422,
            content={
                "code": "INVALID_REQUEST",
                "message": "请求参数无效",
                "request_id": request.state.request_id,
                "details": [
                    {"location": e["loc"], "type": e["type"]} for e in exc.errors()
                ],
            },
        )

    @app.exception_handler(BodyTooLarge)
    async def body_error(request: Request, exc: BodyTooLarge):
        return JSONResponse(
            status_code=413,
            content={
                "code": "UPLOAD_TOO_LARGE",
                "message": "请求内容超过大小限制",
                "request_id": request.state.request_id,
                "details": None,
            },
        )

    async def identity(request: Request) -> AuthContext:
        context = await authenticate(request)
        await run_in_threadpool(
            app.state.repository.check_rate_limit, context, settings.requests_per_minute
        )
        return context

    Context = Annotated[AuthContext, Depends(identity)]
    IdempotencyKey = Annotated[
        str | None, Header(alias="Idempotency-Key", min_length=1, max_length=200)
    ]
    prefix = "/api/v1"

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        try:
            healthy = await run_in_threadpool(app.state.repository.health)
        except Exception:  # noqa: BLE001 - readiness never exposes infrastructure errors
            healthy = False
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={"status": "ready" if healthy else "unavailable"},
        )

    @app.post(prefix + "/knowledge-bases", status_code=201)
    async def create_kb(body: KnowledgeBaseCreate, ctx: Context):
        return await run_in_threadpool(
            app.state.repository.create_kb, ctx, **body.model_dump()
        )

    @app.get(prefix + "/knowledge-bases")
    async def list_kbs(
        ctx: Context, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)
    ):
        items = await run_in_threadpool(
            app.state.repository.list_kbs, ctx, limit=limit, offset=offset
        )
        return {"items": items, "limit": limit, "offset": offset}

    @app.get(prefix + "/knowledge-bases/{kb_id}")
    async def get_kb(kb_id: str, ctx: Context):
        return await run_in_threadpool(app.state.repository.get_kb, ctx, kb_id)

    @app.patch(prefix + "/knowledge-bases/{kb_id}")
    async def update_kb(kb_id: str, body: KnowledgeBaseUpdate, ctx: Context):
        return await run_in_threadpool(
            app.state.repository.update_kb,
            ctx,
            kb_id,
            **body.model_dump(exclude_none=True),
        )

    @app.get(prefix + "/knowledge-bases/{kb_id}/permissions")
    async def kb_permissions(kb_id: str, ctx: Context):
        return {
            "members": await run_in_threadpool(
                app.state.repository.get_kb_permissions, ctx, kb_id
            )
        }

    @app.put(prefix + "/knowledge-bases/{kb_id}/permissions")
    async def set_kb_permissions(kb_id: str, body: KnowledgePermissions, ctx: Context):
        return await run_in_threadpool(
            app.state.repository.set_kb_permissions, ctx, kb_id, body.members
        )

    @app.post(prefix + "/knowledge-bases/{kb_id}/index-generations", status_code=202)
    async def stage_generation(kb_id: str, body: GenerationCreate, ctx: Context):
        return await run_in_threadpool(
            app.state.repository.stage_generation, ctx, kb_id, **body.model_dump()
        )

    @app.get(prefix + "/knowledge-bases/{kb_id}/index-generations/{generation_id}")
    async def get_generation(kb_id: str, generation_id: str, ctx: Context):
        return await run_in_threadpool(
            app.state.repository.get_generation, ctx, kb_id, generation_id
        )

    @app.post(
        prefix + "/knowledge-bases/{kb_id}/index-generations/{generation_id}/activate"
    )
    async def activate_generation(kb_id: str, generation_id: str, ctx: Context):
        return await run_in_threadpool(
            app.state.repository.activate_generation, ctx, kb_id, generation_id
        )

    @app.post(
        prefix + "/knowledge-bases/{kb_id}/index-generations/{generation_id}/abort"
    )
    async def abort_generation(kb_id: str, generation_id: str, ctx: Context):
        return await run_in_threadpool(
            app.state.repository.abort_generation, ctx, kb_id, generation_id
        )

    async def upload(ctx, kb_id, file, key, document_id=None):
        # Authorize before persisting bytes. Replacement also requires document access.
        await run_in_threadpool(app.state.repository.assert_kb_write, ctx, kb_id)
        stored = await run_in_threadpool(
            app.state.storage.write,
            file.file,
            file.filename or "upload",
            file.content_type,
        )
        try:
            result = await run_in_threadpool(
                app.state.repository.upload_version,
                ctx,
                kb_id,
                stored.filename,
                stored.object_key,
                stored.sha256,
                stored.size_bytes,
                stored.content_type,
                document_id=document_id,
                idempotency_key=key,
                object_created_at=stored.created_at,
            )
        finally:
            # A cancelled thread or disconnected database may still commit.
            # Leave uncertain objects to the reference-aware orphan sweeper.
            await file.close()
        # Idempotency may return a prior upload; its canonical object must win.
        canonical = await run_in_threadpool(
            app.state.repository.get_content,
            ctx,
            result["document_id"],
            result["version_id"],
        )
        if canonical["object_key"] != stored.object_key:
            await run_in_threadpool(app.state.storage.delete, stored.object_key)
        return result

    @app.post(prefix + "/knowledge-bases/{kb_id}/documents", status_code=202)
    async def upload_document(
        kb_id: str,
        ctx: Context,
        file: Annotated[UploadFile, File()],
        idempotency_key: IdempotencyKey = None,
    ):
        return await upload(ctx, kb_id, file, idempotency_key)

    @app.get(prefix + "/knowledge-bases/{kb_id}/documents")
    async def list_documents(
        kb_id: str,
        ctx: Context,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        items = await run_in_threadpool(
            app.state.repository.list_documents, ctx, kb_id, limit=limit, offset=offset
        )
        return {"items": items, "limit": limit, "offset": offset}

    @app.post(prefix + "/documents/{document_id}/versions", status_code=202)
    async def replace_document(
        document_id: str,
        ctx: Context,
        file: Annotated[UploadFile, File()],
        idempotency_key: IdempotencyKey = None,
    ):
        document = await run_in_threadpool(
            app.state.repository.get_document, ctx, document_id
        )
        return await upload(ctx, document["kb_id"], file, idempotency_key, document_id)

    @app.patch(prefix + "/documents/{document_id}/permissions")
    async def document_permissions(
        document_id: str, body: DocumentPermissions, ctx: Context
    ):
        return await run_in_threadpool(
            app.state.repository.set_document_permissions,
            ctx,
            document_id,
            **body.model_dump(),
        )

    @app.delete(prefix + "/documents/{document_id}", status_code=202)
    async def delete_document(document_id: str, ctx: Context):
        return await run_in_threadpool(
            app.state.repository.delete_document, ctx, document_id
        )

    @app.get(prefix + "/documents/{document_id}/versions/{version_id}/content")
    async def download(document_id: str, version_id: str, ctx: Context):
        content = await run_in_threadpool(
            app.state.repository.get_content, ctx, document_id, version_id
        )

        def chunks():
            with app.state.storage.open(content["object_key"]) as source:
                while data := source.read(64 * 1024):
                    app.state.repository.get_content(ctx, document_id, version_id)
                    yield data

        from urllib.parse import quote

        return StreamingResponse(
            chunks(),
            media_type=content["content_type"],
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(content["filename"], safe=""),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    @app.get(prefix + "/jobs/{job_id}")
    async def get_job(job_id: str, ctx: Context):
        return public_job(
            await run_in_threadpool(app.state.repository.get_job, ctx, job_id)
        )

    @app.post(prefix + "/jobs/{job_id}/retry", status_code=202)
    async def retry_job(
        job_id: str, ctx: Context, idempotency_key: IdempotencyKey = None
    ):
        return public_job(
            await run_in_threadpool(
                app.state.repository.retry_job,
                ctx,
                job_id,
                idempotency_key=idempotency_key,
            )
        )

    @app.post(prefix + "/retrieval/search")
    async def search(body: SearchRequest, ctx: Context):
        from llamaindex_service.generation.service import EvidenceChanged
        from llamaindex_service.retrieval.embeddings import EmbeddingError

        try:
            return await app.state.rag.search(ctx, body.model_dump())
        except EmbeddingError as exc:
            raise ServiceError(
                "EMBEDDING_UNAVAILABLE", "向量模型暂不可用", 503
            ) from exc
        except EvidenceChanged as exc:
            raise ServiceError(
                "EVIDENCE_CHANGED", "文档权限或版本已变化，请重试", 409
            ) from exc

    @app.post(prefix + "/conversations", status_code=201)
    async def create_conversation(body: ConversationCreate, ctx: Context):
        return await run_in_threadpool(
            app.state.repository.create_conversation,
            ctx,
            body.knowledge_base_ids,
            title=body.title,
        )

    @app.get(prefix + "/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str, ctx: Context):
        return await run_in_threadpool(
            app.state.repository.get_conversation, ctx, conversation_id
        )

    @app.delete(prefix + "/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str, ctx: Context):
        return await run_in_threadpool(
            app.state.repository.delete_conversation, ctx, conversation_id
        )

    @app.post(prefix + "/answers")
    async def answer(
        body: AnswerRequest,
        request: Request,
        ctx: Context,
        idempotency_key: IdempotencyKey = None,
    ):
        history = []
        if body.conversation_id:
            conversation = await run_in_threadpool(
                app.state.repository.get_conversation, ctx, body.conversation_id
            )
            if set(body.knowledge_base_ids) != set(conversation["kb_ids"]):
                raise ServiceError(
                    "CONVERSATION_SCOPE", "会话绑定的知识库范围不匹配", 409
                )
            history = conversation.get("messages", [])
        payload = body.model_dump(exclude={"stream"})
        run = await run_in_threadpool(
            app.state.repository.begin_query,
            ctx,
            request.state.request_id,
            payload,
            idempotency_key=idempotency_key,
            conversation_id=body.conversation_id,
            max_concurrent=settings.max_concurrent_answers,
            lease_seconds=int(settings.generation_timeout_seconds) + 120,
        )
        request_id = run["request_id"]
        if run.get("replayed"):
            run = await run_in_threadpool(
                app.state.repository.get_query, ctx, request_id
            )
            return JSONResponse(
                status_code=202 if run["status"] == "running" else 200, content=run
            )

        async def events():
            finished = False
            iterator = app.state.rag.stream_answer(ctx, payload, history)
            pending = None

            async def disconnected():
                # The request body has already been consumed by FastAPI.
                # Await receive directly: is_disconnected() uses a cancelled
                # scope that may never reach a middleware-wrapped transport.
                while True:
                    message = await request.receive()
                    if message["type"] == "http.disconnect":
                        return

            disconnect = asyncio.create_task(disconnected())
            try:
                while True:
                    pending = asyncio.create_task(anext(iterator))
                    completed, _ = await asyncio.wait(
                        {pending, disconnect}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if disconnect in completed:
                        break
                    try:
                        event = pending.result()
                    except StopAsyncIteration:
                        break
                    data = {**event["data"], "request_id": request_id}
                    if event["event"] in {"done", "error"}:
                        status = data.get("status", "failed")
                        verdict = await run_in_threadpool(
                            app.state.repository.finish_query,
                            ctx,
                            request_id,
                            status,
                            data,
                            error_code=data.get("code"),
                        )
                        finished = True
                        if verdict["status"] != status:
                            event = {"event": "error"}
                            data = {
                                "code": verdict.get("error_code")
                                or "ANSWER_INVALIDATED",
                                "message": "答案提交时权限或执行状态已变化，草稿不可用。",
                                "request_id": request_id,
                                "status": verdict["status"],
                                "retryable": False,
                            }
                        else:
                            data = {
                                **(verdict.get("result") or data),
                                "request_id": request_id,
                            }
                    yield {"event": event["event"], "data": data}
            finally:
                # StreamingResponse cancels its task group on disconnect. Shield
                # both upstream closure and the persistent interrupted marker.
                with anyio.move_on_after(10, shield=True):
                    disconnect.cancel()
                    if pending is not None and not pending.done():
                        pending.cancel()
                    await asyncio.gather(
                        disconnect,
                        *([pending] if pending is not None else []),
                        return_exceptions=True,
                    )
                    await iterator.aclose()
                    if not finished:
                        await run_in_threadpool(
                            app.state.repository.finish_query,
                            ctx,
                            request_id,
                            "interrupted",
                            {"request_id": request_id, "status": "interrupted"},
                        )

        if body.stream:

            async def stream():
                index = 0
                async for event in events():
                    index += 1
                    yield f"id: {index}\nevent: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"

            return StreamingResponse(
                stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-store",
                    "X-Accel-Buffering": "no",
                    "X-Request-ID": request_id,
                },
            )
        result = None
        async for event in events():
            if event["event"] in {"done", "error"}:
                result = event
        if result is None:
            raise ServiceError("INTERRUPTED", "问答已中断", 409)
        return JSONResponse(
            status_code=502 if result["event"] == "error" else 200,
            content=result["data"],
        )

    @app.get(prefix + "/answers/{request_id}")
    async def get_answer(request_id: str, ctx: Context):
        return await run_in_threadpool(app.state.repository.get_query, ctx, request_id)

    @app.post(prefix + "/answers/{request_id}/feedback", status_code=201)
    async def feedback(request_id: str, body: FeedbackCreate, ctx: Context):
        return await run_in_threadpool(
            app.state.repository.add_feedback, ctx, request_id, **body.model_dump()
        )

    return app


app = create_app()
