from __future__ import annotations

import hashlib
import asyncio
import time

from fastapi.testclient import TestClient

from apps.platform_api.main import create_app
from packages.knowledge.embedding import HashEmbeddingProvider
from packages.runtime.executor import ReferenceRuntimeExecutor
from packages.runtime.model_gateway import DeterministicModelGateway
from packages.sandbox.fake_provider import FakeSandboxProvider
from packages.content_security import ContentRejectedError


class RecordingModelGateway(DeterministicModelGateway):
    def __init__(self):
        self.calls = []

    async def complete(self, messages, on_delta=None):
        self.calls.append(messages)
        return await super().complete(messages, on_delta)


def client_for(tmp_path):
    return TestClient(
        create_app(
            str(tmp_path / "platform.db"),
            seed=True,
            model_gateway=DeterministicModelGateway(),
            load_env=False,
            sandbox_providers=[FakeSandboxProvider()],
        )
    )


def reference_agent(client: TestClient):
    return next(
        item
        for item in client.get("/api/v1/agents").json()["items"]
        if item["name"] == "Release Sentinel"
    )


def reference_deployment(client: TestClient):
    return next(
        item
        for item in client.get("/api/v1/agent-deployments").json()["items"]
        if not item["coding_enabled"]
    )


def wait_for_job(client: TestClient, job_id: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/v1/knowledge-ingestion-jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"SUCCEEDED", "FAILED"}:
            return job
        time.sleep(0.04)
    raise AssertionError("Knowledge ingestion job did not finish")


def wait_for_run(client: TestClient, run_id: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/v1/runs/{run_id}").json()
        if run["status"] in {"SUCCEEDED", "FAILED"}:
            return run
        time.sleep(0.04)
    raise AssertionError("Run did not finish")


def wait_for_run_status(client: TestClient, run_id: str, statuses: set[str], timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/v1/runs/{run_id}").json()
        if run["status"] in statuses:
            return run
        time.sleep(0.04)
    raise AssertionError(f"Run did not reach {statuses}")


def create_indexed_knowledge(client: TestClient, *, allowed_roles=None):
    knowledge_base = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "Production handbook", "description": "Release controls"},
    ).json()
    content = (
        "# 生产发布规范\n\n"
        "所有生产环境发布必须先完成自动化测试，并由项目负责人进行人工审批。\n\n"
        "回滚方案需要在发布前验证，审批记录必须保留在运行审计事件中。"
    ).encode("utf-8")
    prepared_response = client.post(
        f"/api/v1/knowledge-bases/{knowledge_base['id']}/documents:prepare-upload",
        json={
            "filename": "production-release.md",
            "content_type": "text/markdown",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "allowed_roles": allowed_roles or [],
        },
    )
    assert prepared_response.status_code == 201
    prepared = prepared_response.json()
    assert prepared["storage"]["canonical_uri"].startswith("local://")
    assert "signature" not in prepared["storage"]["canonical_uri"].lower()
    upload = client.put(
        prepared["upload"]["url"],
        content=content,
        headers={"Content-Type": "text/markdown"},
    )
    assert upload.status_code == 200
    completed = client.post(
        f"/api/v1/knowledge-document-versions/{prepared['document_version_id']}:complete",
        json={},
    )
    assert completed.status_code == 202
    job = wait_for_job(client, completed.json()["id"])
    assert job["status"] == "SUCCEEDED", job
    detail = client.get(f"/api/v1/knowledge-bases/{knowledge_base['id']}").json()
    assert detail["current_revision_id"]
    assert detail["documents"][0]["status"] == "READY"
    return detail, prepared, content


def create_thread_with_knowledge(client: TestClient, revision_id: str, title: str = "RAG"):
    agent = reference_agent(client)
    detail = client.get(f"/api/v1/agents/{agent['id']}").json()
    detail["draft"]["capabilities"]["knowledge_bases"] = [revision_id]
    updated = client.patch(
        f"/api/v1/agents/{agent['id']}/draft",
        json={
            "name": detail["name"],
            "description": detail["description"],
            "draft": detail["draft"],
            "version": detail["version"],
        },
    )
    assert updated.status_code == 200
    published = client.post(f"/api/v1/agents/{agent['id']}/revisions:publish")
    assert published.status_code == 201
    deployment = client.post(
        "/api/v1/agent-deployments",
        json={
            "agent_revision_id": published.json()["revision"]["id"],
            "environment": "development",
            "name": title,
        },
    ).json()
    return client.post(
        "/api/v1/threads",
        json={"agent_deployment_id": deployment["id"], "title": title},
    ).json()


def test_oss_shaped_upload_ingestion_search_and_download(tmp_path):
    with client_for(tmp_path) as client:
        knowledge_base, prepared, content = create_indexed_knowledge(client)
        search = client.post(
            "/api/v1/knowledge:search",
            json={
                "query": "生产发布需要谁进行人工审批？",
                "knowledge_base_id": knowledge_base["id"],
                "top_k": 5,
            },
        )
        assert search.status_code == 200
        result = search.json()
        assert result["status"] == "ok"
        assert result["hits"]
        hit = result["hits"][0]
        assert hit["citation_id"] == "cite_01"
        assert hit["source"]["download_url"].endswith(
            f"/knowledge-document-versions/{prepared['document_version_id']}/download"
        )
        assert hit["source"]["canonical_uri"].startswith("local://")
        assert hit["source"]["locator"]["section"] == "生产发布规范"
        assert "人工审批" in hit["text"]
        assert job_chunk_count(client, knowledge_base["id"]) > 0

        downloaded = client.get(hit["source"]["download_url"])
        assert downloaded.status_code == 200
        assert downloaded.content == content
        events = client.get("/api/v1/knowledge-events").json()["items"]
        event_types = {event["type"] for event in events}
        assert "knowledge.ingestion.completed" in event_types
        assert "knowledge.search.completed" in event_types


def test_knowledge_is_tenant_and_role_scoped(tmp_path):
    with client_for(tmp_path) as client:
        knowledge_base, prepared, _ = create_indexed_knowledge(
            client, allowed_roles=["release_manager"]
        )
        foreign_scope = {
            "X-Tenant-ID": "tenant_other",
            "X-Project-ID": "project_other",
            "X-Environment-ID": "env_development",
            "X-User-ID": "foreign_user",
            "X-Roles": "viewer",
        }
        assert (
            client.get(f"/api/v1/knowledge-bases/{knowledge_base['id']}", headers=foreign_scope).status_code
            == 404
        )
        assert (
            client.get(
                f"/api/v1/knowledge-documents/{prepared['document_id']}/download",
                headers=foreign_scope,
            ).status_code
            == 404
        )

        viewer_headers = {
            "X-Tenant-ID": "tenant_demo",
            "X-Project-ID": "project_atlas",
            "X-Environment-ID": "env_development",
            "X-User-ID": "viewer",
            "X-Roles": "viewer",
        }
        hidden = client.post(
            "/api/v1/knowledge:search",
            headers=viewer_headers,
            json={
                "query": "生产发布审批",
                "revision_ids": [knowledge_base["current_revision_id"]],
            },
        ).json()
        assert hidden["status"] == "insufficient_evidence"
        assert hidden["hits"] == []


def test_agent_plan_pins_knowledge_revision_and_runtime_returns_citations(tmp_path):
    with client_for(tmp_path) as client:
        knowledge_base, _, _ = create_indexed_knowledge(client)
        agent = reference_agent(client)
        detail = client.get(f"/api/v1/agents/{agent['id']}").json()
        detail["draft"]["capabilities"]["knowledge_bases"] = [
            knowledge_base["current_revision_id"]
        ]
        updated = client.patch(
            f"/api/v1/agents/{agent['id']}/draft",
            json={
                "name": detail["name"],
                "description": detail["description"],
                "draft": detail["draft"],
                "version": detail["version"],
            },
        )
        assert updated.status_code == 200
        published = client.post(f"/api/v1/agents/{agent['id']}/revisions:publish")
        assert published.status_code == 201
        binding = published.json()["resolved_plan"]["plan"]["knowledge_bindings"][0]
        assert binding["revision_id"] == knowledge_base["current_revision_id"]
        assert len(binding["index_hash"]) == 64
        assert binding["access"] == "read_only"
        builtin_agents = published.json()["resolved_plan"]["plan"][
            "builtin_agent_bindings"
        ]
        assert builtin_agents == [
            {
                "name": "builtin_rag",
                "version": "1.0.0",
                "routing": "auto_evidence",
                "tool": "knowledge_search",
            }
        ]

        deployment = client.post(
            "/api/v1/agent-deployments",
            json={
                "agent_revision_id": published.json()["revision"]["id"],
                "environment": "development",
                "name": "rag-runtime-test",
            },
        ).json()
        thread = client.post(
            "/api/v1/threads",
            json={"agent_deployment_id": deployment["id"], "title": "RAG runtime"},
        ).json()
        run = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            json={"input": "为什么发布流程需要人工审批？"},
        ).json()
        assert wait_for_run(client, run["id"])["status"] == "SUCCEEDED"
        events = client.get(f"/api/v1/runs/{run['id']}/events").json()["items"]
        routes = [event for event in events if event["type"] == "rag.agent.routed"]
        assert routes[-1]["payload"]["route"] == "knowledge"
        completed = [
            event
            for event in events
            if event["type"] == "tool.completed"
            and event["payload"].get("tool_name") == "knowledge_search"
        ][0]
        assert completed["payload"]["result_count"] > 0
        assert completed["payload"]["citations"][0]["title"] == "production-release.md"
        final = client.get(f"/api/v1/runs/{run['id']}").json()
        assert "[cite_01]" in final["output"]


def test_upload_size_mismatch_is_rejected_before_ingestion(tmp_path):
    with client_for(tmp_path) as client:
        knowledge_base = client.post(
            "/api/v1/knowledge-bases", json={"name": "Validation base"}
        ).json()
        prepared = client.post(
            f"/api/v1/knowledge-bases/{knowledge_base['id']}/documents:prepare-upload",
            json={
                "filename": "bad.txt",
                "content_type": "text/plain",
                "size_bytes": 100,
                "sha256": "0" * 64,
            },
        ).json()
        upload = client.put(
            prepared["upload"]["url"],
            content=b"too short",
            headers={"Content-Type": "text/plain"},
        )
        assert upload.status_code == 422
        assert upload.json()["error"]["code"] == "KNOWLEDGE_VALIDATION_ERROR"


def test_rejected_content_fails_durable_validation_and_cannot_be_downloaded(tmp_path):
    class RejectScanner:
        name = "reject-test"

        def scan(self, content, *, object_name):
            raise ContentRejectedError("Content was rejected by malware policy")

    with client_for(tmp_path) as client:
        client.app.state.services.knowledge.content_scanner = RejectScanner()
        knowledge_base = client.post(
            "/api/v1/knowledge-bases", json={"name": "Quarantine test"}
        ).json()
        content = b"untrusted fixture"
        prepared = client.post(
            f"/api/v1/knowledge-bases/{knowledge_base['id']}/documents:prepare-upload",
            json={
                "filename": "fixture.txt", "content_type": "text/plain",
                "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
            },
        ).json()
        uploaded = client.put(
            prepared["upload"]["url"], content=content,
            headers={"Content-Type": "text/plain"},
        )
        assert uploaded.status_code == 200
        completed = client.post(
            f"/api/v1/knowledge-document-versions/{prepared['document_version_id']}:complete",
            json={},
        )
        assert completed.status_code == 202
        failed = wait_for_job(client, completed.json()['id'])
        assert failed['status'] == 'FAILED'
        assert 'malware' in failed['error_message']
        assert client.app.state.services.db.fetch_one(
            "SELECT COUNT(*) AS count FROM knowledge_ingestion_jobs"
        )["count"] == 1
        assert client.app.state.services.db.fetch_one('SELECT COUNT(*) AS n FROM knowledge_chunks')['n'] == 0
        download = client.get(
            f"/api/v1/knowledge-document-versions/{prepared['document_version_id']}/download"
        )
        assert download.status_code == 409


def job_chunk_count(client: TestClient, knowledge_base_id: str) -> int:
    events = client.get("/api/v1/knowledge-events").json()["items"]
    completed = next(
        event
        for event in events
        if event["knowledge_base_id"] == knowledge_base_id
        and event["type"] == "knowledge.ingestion.completed"
    )
    job = client.get(
        f"/api/v1/knowledge-ingestion-jobs/{completed['ingestion_job_id']}"
    ).json()
    return job["chunk_count"]


def test_run_metadata_cannot_override_authenticated_principal(tmp_path):
    with client_for(tmp_path) as client:
        deployment = reference_deployment(client)
        thread = client.post(
            "/api/v1/threads",
            json={"agent_deployment_id": deployment["id"], "title": "principal"},
        ).json()
        shared = client.put(f"/api/v1/threads/{thread['id']}/access", json={
            "version": 1, "visibility": "project", "reason": "Test shared-thread metadata isolation",
        })
        assert shared.status_code == 200
        response = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            headers={
                "X-Tenant-ID": "tenant_demo",
                "X-Project-ID": "project_atlas",
                "X-Environment-ID": "env_development",
                "X-User-ID": "viewer",
                "X-Roles": "member",
            },
            json={"input": "hello", "metadata": {"user_id": "attacker", "roles": ["owner"]}},
        )
        assert response.status_code == 409
        assert "reserved identity fields" in response.json()["error"]["message"]


def test_builtin_rag_agent_routes_creative_request_to_model_only(tmp_path):
    with client_for(tmp_path) as client:
        knowledge_base, _, _ = create_indexed_knowledge(client)
        thread = create_thread_with_knowledge(
            client, knowledge_base["current_revision_id"], "creative-route"
        )
        created = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            json={"input": "写一首关于春天的诗"},
        ).json()
        run = wait_for_run(client, created["id"])
        events = client.get(f"/api/v1/runs/{run['id']}/events").json()["items"]
        route = next(event for event in events if event["type"] == "rag.agent.routed")
        assert route["payload"]["route"] == "model_only"
        assert route["payload"]["reason"] == "request_is_model_native"
        assert not [
            event
            for event in events
            if event["type"] == "tool.completed"
            and event["payload"].get("tool_name") == "knowledge_search"
        ]
        assert run["usage"]["tool_calls"] == 0


def test_builtin_rag_agent_contextualizes_short_follow_up(tmp_path):
    with client_for(tmp_path) as client:
        knowledge_base, _, _ = create_indexed_knowledge(client)
        thread = create_thread_with_knowledge(
            client, knowledge_base["current_revision_id"], "follow-up-route"
        )
        first = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            json={"input": "为什么发布流程需要人工审批？"},
        ).json()
        assert wait_for_run(client, first["id"])["status"] == "SUCCEEDED"

        follow_up = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            json={"input": "再详细一点"},
        ).json()
        assert wait_for_run(client, follow_up["id"])["status"] == "SUCCEEDED"
        events = client.get(f"/api/v1/runs/{follow_up['id']}/events").json()["items"]
        route = next(event for event in events if event["type"] == "rag.agent.routed")
        assert route["payload"]["route"] == "knowledge"
        assert route["payload"]["reason"] == "reliable_knowledge_evidence_found"


def test_builtin_rag_agent_falls_back_when_knowledge_is_irrelevant(tmp_path):
    with client_for(tmp_path) as client:
        knowledge_base, _, _ = create_indexed_knowledge(client)
        thread = create_thread_with_knowledge(
            client, knowledge_base["current_revision_id"], "irrelevant-route"
        )
        created = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            json={"input": "月球表面的平均温度是多少？"},
        ).json()
        run = wait_for_run(client, created["id"])
        assert run["status"] == "SUCCEEDED"
        events = client.get(f"/api/v1/runs/{run['id']}/events").json()["items"]
        route = next(event for event in events if event["type"] == "rag.agent.routed")
        assert route["payload"]["route"] == "model_only"
        assert route["payload"]["reason"] == "no_reliable_knowledge_evidence"
        assert run["usage"]["tool_calls"] == 1


def test_tool_binding_and_zero_budget_prevent_rag_execution(tmp_path):
    with client_for(tmp_path) as client:
        knowledge_base, _, _ = create_indexed_knowledge(client)
        agent = reference_agent(client)
        detail = client.get(f"/api/v1/agents/{agent['id']}").json()
        detail["draft"]["capabilities"]["knowledge_bases"] = [
            knowledge_base["current_revision_id"]
        ]
        detail["draft"]["capabilities"]["tools"] = []
        detail["draft"]["limits"]["max_tool_calls"] = 0
        client.patch(
            f"/api/v1/agents/{agent['id']}/draft",
            json={
                "name": detail["name"],
                "description": detail["description"],
                "draft": detail["draft"],
                "version": detail["version"],
            },
        ).raise_for_status()
        published = client.post(f"/api/v1/agents/{agent['id']}/revisions:publish")
        assert published.status_code == 201
        assert published.json()["resolved_plan"]["plan"]["builtin_agent_bindings"] == []
        deployment = client.post(
            "/api/v1/agent-deployments",
            json={"agent_revision_id": published.json()["revision"]["id"]},
        ).json()
        thread = client.post(
            "/api/v1/threads",
            json={"agent_deployment_id": deployment["id"], "title": "no-rag-tool"},
        ).json()
        created = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            json={"input": "生产发布规范是什么？"},
        ).json()
        run = wait_for_run(client, created["id"])
        events = client.get(f"/api/v1/runs/{run['id']}/events").json()["items"]
        assert run["usage"]["tool_calls"] == 0
        assert not [event for event in events if event["type"] == "tool.requested"]


def test_embedding_model_pin_and_chunk_integrity_are_enforced(tmp_path):
    class OtherEmbedding:
        model_revision = "other-model-with-same-dimensions"
        dimensions = 256

        def embed_documents(self, texts):
            return [[0.0] * self.dimensions for _ in texts]

        def embed_query(self, text):
            return [0.0] * self.dimensions

    with client_for(tmp_path) as client:
        knowledge_base, _, _ = create_indexed_knowledge(client)
        services = client.app.state.services
        services.knowledge.embedding = OtherEmbedding()
        mismatch = client.post(
            "/api/v1/knowledge:search",
            json={
                "query": "生产发布规范是什么？",
                "revision_ids": [knowledge_base["current_revision_id"]],
            },
        )
        assert mismatch.status_code == 409
        services.knowledge.embedding = HashEmbeddingProvider()
        chunk = services.db.fetch_one(
            "SELECT id FROM knowledge_chunks WHERE knowledge_base_id=? LIMIT 1",
            (knowledge_base["id"],),
        )
        services.db.execute(
            "UPDATE knowledge_chunks SET text='tampered chunk' WHERE id=?", (chunk["id"],)
        )
        tampered = client.post(
            "/api/v1/knowledge:search",
            json={
                "query": "生产发布规范是什么？",
                "revision_ids": [knowledge_base["current_revision_id"]],
            },
        )
        assert tampered.status_code == 409
        assert "integrity check failed" in tampered.json()["error"]["message"]


def test_hitl_resume_retrieves_again_and_preserves_safe_rag_context(tmp_path):
    gateway = RecordingModelGateway()
    with TestClient(
        create_app(
            str(tmp_path / "platform.db"),
            seed=True,
            model_gateway=gateway,
            load_env=False,
            sandbox_providers=[FakeSandboxProvider()],
        )
    ) as client:
        knowledge_base, _, _ = create_indexed_knowledge(client)
        thread = create_thread_with_knowledge(
            client, knowledge_base["current_revision_id"], "rag-hitl"
        )
        created = client.post(
            f"/api/v1/threads/{thread['id']}/runs",
            json={"input": "根据生产发布规范，deploy to production 前需要谁审批？"},
        ).json()
        waiting = wait_for_run_status(
            client, created["id"], {"WAITING_FOR_APPROVAL"}
        )
        assert waiting["checkpoint"]["rag"]["route"] == "knowledge"
        interrupt = client.get("/api/v1/interrupts?status=PENDING").json()["items"][0]
        action = interrupt["actions"][0]
        client.post(
            f"/api/v1/interrupts/{interrupt['id']}/decisions",
            headers={"If-Match": str(interrupt["version"])},
            json={"decisions": [{"action_id": action["action_id"], "type": "approve"}]},
        ).raise_for_status()
        finished = wait_for_run(client, created["id"])
        assert "[cite_01]" in finished["output"]
        events = client.get(f"/api/v1/runs/{created['id']}/events").json()["items"]
        knowledge_routes = [
            event
            for event in events
            if event["type"] == "rag.agent.routed"
            and event["payload"]["route"] == "knowledge"
        ]
        assert len(knowledge_routes) == 2
        assert finished["usage"]["tool_calls"] == 3
        reference_messages = [
            message
            for message in gateway.calls[-1]
            if "untrusted reference data" in message["content"]
        ]
        assert reference_messages and reference_messages[0]["role"] == "user"
        assert not [
            message
            for message in gateway.calls[-1]
            if message["role"] == "system" and "生产发布规范" in message["content"]
        ]


def test_two_workers_cannot_claim_the_same_ingestion_job(tmp_path):
    from packages.domain.models import TenantContext
    from packages.knowledge.models import KnowledgeBaseCreate, UploadComplete, UploadPrepare
    from packages.knowledge.service import KnowledgeService
    from packages.knowledge.storage.local import LocalObjectStorage
    from packages.persistence import Database

    async def scenario():
        db = Database(str(tmp_path / "claim.db"))
        db.initialize()
        storage = LocalObjectStorage(tmp_path / "objects")
        first = KnowledgeService(db, storage, HashEmbeddingProvider())
        second = KnowledgeService(db, storage, HashEmbeddingProvider())
        context = TenantContext(tenant_id="tenant", project_id="project")
        knowledge_base = first.create_knowledge_base(
            KnowledgeBaseCreate(name="Claim safety"), context
        )
        content = b"single ingestion payload"
        prepared = first.prepare_upload(
            knowledge_base["id"],
            UploadPrepare(
                filename="claim.txt",
                content_type="text/plain",
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            ),
            context,
        )
        first.upload_content(
            prepared["document_version_id"], content, "text/plain", context
        )
        job = await first.complete_upload(
            prepared["document_version_id"], UploadComplete(), context
        )
        await asyncio.gather(
            first._process_job(job["id"]), second._process_job(job["id"])
        )
        final = db.fetch_one(
            "SELECT * FROM knowledge_ingestion_jobs WHERE id=?", (job["id"],)
        )
        events = db.fetch_all(
            "SELECT type FROM knowledge_events WHERE ingestion_job_id=?", (job["id"],)
        )
        db.close()
        return final, events

    final, events = asyncio.run(scenario())
    assert final["status"] == "SUCCEEDED"
    assert final["attempts"] == 1
    assert sum(event["type"] == "knowledge.ingestion.started" for event in events) == 1
    assert sum(event["type"] == "knowledge.ingestion.completed" for event in events) == 1


def test_upload_requires_sha256_and_rejects_wrong_digest_during_ingestion(tmp_path):
    with client_for(tmp_path) as client:
        knowledge_base = client.post(
            "/api/v1/knowledge-bases", json={"name": "Digest validation"}
        ).json()
        missing = client.post(
            f"/api/v1/knowledge-bases/{knowledge_base['id']}/documents:prepare-upload",
            json={
                "filename": "missing.txt",
                "content_type": "text/plain",
                "size_bytes": 4,
            },
        )
        assert missing.status_code == 422
        content = b"real content"
        prepared = client.post(
            f"/api/v1/knowledge-bases/{knowledge_base['id']}/documents:prepare-upload",
            json={
                "filename": "wrong.txt",
                "content_type": "text/plain",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(b"different content").hexdigest(),
            },
        ).json()
        client.put(
            prepared["upload"]["url"],
            content=content,
            headers={"Content-Type": "text/plain"},
        ).raise_for_status()
        job = client.post(
            f"/api/v1/knowledge-document-versions/{prepared['document_version_id']}:complete",
            json={},
        ).json()
        failed = wait_for_job(client, job["id"])
        assert failed["status"] == "FAILED"
        assert "SHA-256" in failed["error_message"]


def test_aliyun_oss_adapter_initializes_without_resolving_secrets(monkeypatch):
    from packages.knowledge.storage.oss import AliyunOSSObjectStorage

    monkeypatch.setenv("ALIYUN_OSS_BUCKET", "jie-agent-file")
    monkeypatch.setenv("ALIYUN_OSS_REGION", "cn-beijing")
    storage = AliyunOSSObjectStorage.from_environment()
    _, client = storage._client()
    assert client.__class__.__name__ == "Client"
    assert storage.canonical_uri("rag/test.txt") == "oss://jie-agent-file/rag/test.txt"


def test_output_limit_preserves_verified_knowledge_citation():
    output = ReferenceRuntimeExecutor._limit_and_validate_output(
        "证据说明" * 1000,
        {"hits": [{"citation_id": "cite_01"}]},
        1024,
    )
    assert len(output.encode("utf-8")) <= 1024
    assert "[Response truncated by run limit]" in output
    assert output.endswith("Sources: [cite_01]")
