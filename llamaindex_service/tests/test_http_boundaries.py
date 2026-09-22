"""Exercise real ASGI disconnect and ingress behavior without a database/model."""

import asyncio
import hashlib
import json
import threading
import time
import unittest

import httpx

from llamaindex_service.app import create_app
from llamaindex_service.config import Settings
from llamaindex_service.contracts import ServiceError
from llamaindex_service.generation.service import EvidenceChanged
from llamaindex_service.retrieval.embeddings import EmbeddingError
from llamaindex_service.storage.objects import StoredObject


class BoundaryRepository:
    def __init__(self):
        self.finished = []
        self.membership_calls = 0
        self.rate_limited = False
        self.replayed = False
        self.revalidated_replay = False
        self.final_verdict = None

    def ensure_membership(self, context):
        self.membership_calls += 1

    def check_rate_limit(self, context, limit):
        if self.rate_limited:
            raise ServiceError("RATE_LIMITED", "请求过于频繁", 429)

    def begin_query(self, context, request_id, payload, **kwargs):
        return {
            "request_id": request_id,
            "status": "running",
            "replayed": self.replayed,
        }

    def get_query(self, context, request_id):
        self.revalidated_replay = True
        raise ServiceError("NOT_FOUND", "资源不存在或不可访问", 404)

    def finish_query(self, context, request_id, status, result, **kwargs):
        self.finished.append(
            {"request_id": request_id, "status": status, "result": result}
        )
        return self.final_verdict or {
            "request_id": request_id,
            "status": status,
            "result": result,
            "error_code": None,
        }


class SlowRAG:
    def __init__(self):
        self.started, self.closed = asyncio.Event(), asyncio.Event()

    async def stream_answer(self, context, payload, history):
        try:
            self.started.set()
            await asyncio.Future()
            yield {"event": "delta", "data": {"text": "must not appear"}}
        finally:
            self.closed.set()


def boundary_app(repository=None, rag=None, **updates):
    config = Settings(_env_file=None, environment="test", model_mode="mock", **updates)
    repository = repository or BoundaryRepository()
    rag = rag or SlowRAG()
    app = create_app(config, repository=repository, storage=object(), rag=rag)
    # ASGITransport does not trigger lifespan. Injected doubles avoid requiring
    # the real database/storage while retaining all routes and middleware.
    app.state.repository, app.state.rag, app.state.storage = repository, rag, object()
    return app, repository, rag


class HttpBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_upload_does_not_delete_object_needed_by_db_thread(self):
        loop = asyncio.get_running_loop()
        started, committed = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        storage = UploadStorage()

        class DelayedRepository(BoundaryRepository):
            committed_object = None

            def assert_kb_write(self, context, kb_id):
                return None

            def upload_version(
                self, context, kb_id, filename, object_key, *args, **kwargs
            ):
                loop.call_soon_threadsafe(started.set)
                if not release.wait(timeout=2):
                    raise TimeoutError("Fixture database commit was never released")
                self.committed_object = object_key
                loop.call_soon_threadsafe(committed.set)
                return {
                    "document_id": "document",
                    "version_id": "version",
                    "job_id": "job",
                }

            def get_content(self, context, document_id, version_id):
                return {"object_key": self.committed_object}

        repository = DelayedRepository()
        app, _, _ = boundary_app(repository=repository)
        app.state.storage = storage
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            request = asyncio.create_task(
                client.post(
                    "/api/v1/knowledge-bases/kb/documents",
                    files={"file": ("fixture.md", b"durable source", "text/markdown")},
                )
            )
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                request.cancel()
                # Allow cancellation to reach the awaited database thread before
                # simulating its delayed commit. Native task cancellation cannot
                # guarantee that an already running thread rolls back.
                await asyncio.sleep(0)
                self.assertEqual(storage.deleted, [])
                release.set()
                await asyncio.wait_for(committed.wait(), timeout=2)
                await asyncio.wait_for(
                    asyncio.gather(request, return_exceptions=True), timeout=2
                )
            finally:
                release.set()
                if not request.done():
                    request.cancel()
                    await asyncio.gather(request, return_exceptions=True)
        self.assertEqual(repository.committed_object, storage.object_key)
        self.assertEqual(storage.objects[storage.object_key], b"durable source")
        self.assertEqual(storage.deleted, [])

    async def test_uncertain_database_error_leaves_upload_for_reference_aware_sweep(
        self,
    ):
        storage = UploadStorage()

        class UncertainRepository(BoundaryRepository):
            def assert_kb_write(self, context, kb_id):
                return None

            def upload_version(self, *args, **kwargs):
                # A connection error may occur after PostgreSQL commits but
                # before the caller receives confirmation.
                raise ConnectionError("Commit outcome unknown")

        app, _, _ = boundary_app(repository=UncertainRepository())
        app.state.storage = storage
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/knowledge-bases/kb/documents",
                files={"file": ("fixture.md", b"uncertain source", "text/markdown")},
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(storage.objects[storage.object_key], b"uncertain source")
        self.assertEqual(storage.deleted, [])

    async def test_disconnect_cancels_silent_upstream_and_persists_interrupted(self):
        for stream in (False, True):
            for spec in ("2.0", "2.4"):
                with self.subTest(stream=stream, asgi=spec):
                    app, repository, rag = boundary_app()
                    disconnect = asyncio.Event()
                    sent = []
                    first = True
                    body = json.dumps(
                        {
                            "knowledge_base_ids": ["kb"],
                            "query": "test",
                            "stream": stream,
                        }
                    ).encode()

                    async def receive(body=body, disconnect=disconnect):
                        nonlocal first
                        if first:
                            first = False
                            return {
                                "type": "http.request",
                                "body": body,
                                "more_body": False,
                            }
                        await disconnect.wait()
                        return {"type": "http.disconnect"}

                    async def send(message, sent=sent):
                        sent.append(message)

                    scope = {
                        "type": "http",
                        "asgi": {"version": "3.0", "spec_version": spec},
                        "http_version": "1.1",
                        "method": "POST",
                        "scheme": "http",
                        "path": "/api/v1/answers",
                        "raw_path": b"/api/v1/answers",
                        "query_string": b"",
                        "root_path": "",
                        "headers": [(b"content-type", b"application/json")],
                        "client": ("127.0.0.1", 12345),
                        "server": ("localhost", 80),
                    }
                    running = asyncio.create_task(app(scope, receive, send))
                    try:
                        await asyncio.wait_for(rag.started.wait(), timeout=2)
                        disconnect.set()
                        await asyncio.wait_for(running, timeout=2)
                    finally:
                        if not running.done():
                            running.cancel()
                            await asyncio.gather(running, return_exceptions=True)
                    self.assertTrue(rag.closed.is_set())
                    self.assertEqual(
                        [run["status"] for run in repository.finished], ["interrupted"]
                    )
                    self.assertNotIn("must not appear", str(sent))

    async def test_dev_identity_rejects_non_loopback_client(self):
        app, repository, _ = boundary_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app, client=("192.0.2.1", 1234)),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/answers", json={"knowledge_base_ids": ["kb"], "query": "test"}
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "LOCAL_AUTH_ONLY")
        self.assertEqual(repository.membership_calls, 0)

    async def test_rate_limit_returns_retry_after(self):
        app, repository, _ = boundary_app()
        repository.rate_limited = True
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/answers", json={"knowledge_base_ids": ["kb"], "query": "test"}
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "60")
        self.assertEqual(response.json()["code"], "RATE_LIMITED")

    async def test_content_length_limit_rejects_before_identity(self):
        app, repository, _ = boundary_app(max_upload_bytes=16)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/answers", content=b"x", headers={"Content-Length": "99999999"}
            )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(repository.membership_calls, 0)

    async def test_chunked_body_limit_returns_413(self):
        app, _, _ = boundary_app(max_upload_bytes=16)

        async def body():
            yield b"x" * 180_000
            yield b"x" * 180_000

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/answers",
                content=body(),
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(response.json()["code"], "UPLOAD_TOO_LARGE")

    async def test_replay_rechecks_permission_before_returning_result(self):
        app, repository, _ = boundary_app()
        repository.replayed = True
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/answers",
                json={"knowledge_base_ids": ["kb"], "query": "test"},
                headers={"Idempotency-Key": "existing"},
            )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(repository.revalidated_replay)

    async def test_commit_time_revocation_does_not_return_completed_answer(self):
        class CompletedRAG:
            async def stream_answer(self, context, payload, history):
                yield {
                    "event": "done",
                    "data": {
                        "status": "completed",
                        "answer": "private-source-final-answer",
                        "citations": [],
                    },
                }

        for stream in (False, True):
            with self.subTest(stream=stream):
                app, repository, _ = boundary_app(rag=CompletedRAG())
                repository.final_verdict = {
                    "status": "failed",
                    "result": None,
                    "error_code": "EVIDENCE_REVOKED",
                }
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://test"
                ) as client:
                    response = await client.post(
                        "/api/v1/answers",
                        json={
                            "knowledge_base_ids": ["kb"],
                            "query": "test",
                            "stream": stream,
                        },
                    )
                self.assertNotIn("private-source-final-answer", response.text)
                if stream:
                    self.assertIn("event: error", response.text)
                    self.assertNotIn("event: done", response.text)
                else:
                    self.assertEqual(response.status_code, 502)
                self.assertIn("EVIDENCE_REVOKED", response.text)

    async def test_search_errors_are_typed_and_redact_upstream_details(self):
        class FailingSearch:
            def __init__(self, error):
                self.error = error

            async def search(self, context, request):
                raise self.error

        for error, status, code in (
            (EmbeddingError("secret-provider-details"), 503, "EMBEDDING_UNAVAILABLE"),
            (EvidenceChanged("private-doc-title"), 409, "EVIDENCE_CHANGED"),
        ):
            with self.subTest(code=code):
                app, _, _ = boundary_app(rag=FailingSearch(error))
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://test"
                ) as client:
                    response = await client.post(
                        "/api/v1/retrieval/search",
                        json={"knowledge_base_ids": ["kb"], "query": "test"},
                    )
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["code"], code)
                self.assertNotIn(str(error), response.text)


class UploadStorage:
    """An immutable object survives until explicit reference-aware cleanup."""

    def __init__(self):
        self.object_key = "objects/" + "a" * 32 + ".md"
        self.objects = {}
        self.deleted = []

    def write(self, source, filename, content_type):
        data = source.read()
        self.objects[self.object_key] = data
        return StoredObject(
            object_key=self.object_key,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            content_type=content_type,
            filename=filename,
            created_at=time.time(),
        )

    def delete(self, object_key):
        self.deleted.append(object_key)
        self.objects.pop(object_key, None)


if __name__ == "__main__":
    unittest.main()
