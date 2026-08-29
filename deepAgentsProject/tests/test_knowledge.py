from __future__ import annotations

import time

from fastapi.testclient import TestClient

from apps.platform_api.main import create_app
from packages.runtime.model_gateway import DeterministicModelGateway


def client_for(tmp_path):
    return TestClient(
        create_app(
            str(tmp_path / "platform.db"),
            seed=True,
            model_gateway=DeterministicModelGateway(),
            load_env=False,
        )
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
            f"/knowledge-documents/{prepared['document_id']}/download"
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
        foreign_scope = {"X-Tenant-ID": "tenant_other", "X-Project-ID": "project_other"}
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

        viewer_headers = {"X-User-ID": "viewer", "X-Roles": "viewer"}
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
        agent = client.get("/api/v1/agents").json()["items"][0]
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
        completed = [
            event
            for event in events
            if event["type"] == "tool.completed"
            and event["payload"].get("tool_name") == "knowledge_search"
        ][0]
        assert completed["payload"]["result_count"] > 0
        assert completed["payload"]["citations"][0]["title"] == "production-release.md"


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
            },
        ).json()
        upload = client.put(
            prepared["upload"]["url"],
            content=b"too short",
            headers={"Content-Type": "text/plain"},
        )
        assert upload.status_code == 422
        assert upload.json()["error"]["code"] == "KNOWLEDGE_VALIDATION_ERROR"


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


def test_aliyun_oss_adapter_initializes_without_resolving_secrets(monkeypatch):
    from packages.knowledge.storage.oss import AliyunOSSObjectStorage

    monkeypatch.setenv("ALIYUN_OSS_BUCKET", "jie-agent-file")
    monkeypatch.setenv("ALIYUN_OSS_REGION", "cn-beijing")
    storage = AliyunOSSObjectStorage.from_environment()
    _, client = storage._client()
    assert client.__class__.__name__ == "Client"
    assert storage.canonical_uri("rag/test.txt") == "oss://jie-agent-file/rag/test.txt"
