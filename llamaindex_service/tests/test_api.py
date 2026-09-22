import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from llamaindex_service.app import create_app
from llamaindex_service.config import Settings
from llamaindex_service.contracts import AuthContext
from llamaindex_service.persistence import Repository
from llamaindex_service.workers import Worker


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(
            _env_file=None,
            environment="test",
            database_url=getattr(
                self, "database_url", f"sqlite:///{self.temp.name}/test.db"
            ),
            model_mode="mock",
            embedding_model="api-test-" + uuid4().hex,
            embedding_dimension=32,
            storage_path=Path(self.temp.name) / "objects",
            requests_per_minute=1000,
            dev_tenant_id="test-" + uuid4().hex,
        )
        self.repository = Repository(self.settings)
        self.addCleanup(self.repository.engine.dispose)
        self.repository.create_schema()
        self.app = create_app(self.settings, repository=self.repository)
        self.client = self.enterContext(TestClient(self.app))
        response = self.client.post(
            "/api/v1/knowledge-bases", json={"name": "研发知识库"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.kb = response.json()["id"]
        self.worker = Worker(
            self.settings, self.repository, storage=self.app.state.storage
        )

    def upload(
        self,
        text="Atlas 服务默认超时为 30 秒。重试次数为 3 次。",
        key="sample",
        document_id=None,
    ):
        url = (
            f"/api/v1/documents/{document_id}/versions"
            if document_id
            else f"/api/v1/knowledge-bases/{self.kb}/documents"
        )
        response = self.client.post(
            url,
            files={"file": ("guide.md", text.encode(), "text/markdown")},
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def search(self, query="Atlas 超时", **extra):
        return self.client.post(
            "/api/v1/retrieval/search",
            json={"knowledge_base_ids": [self.kb], "query": query, **extra},
        )

    def test_upload_ingest_query_citation_download_and_delete(self):
        item = self.upload()
        self.assertTrue(self.worker.run_once())
        job = self.client.get(f"/api/v1/jobs/{item['job_id']}").json()
        self.assertEqual(job["status"], "READY", job)
        response = self.search()
        self.assertEqual(response.status_code, 200, response.text)
        citation = response.json()["citations"][0]
        self.assertEqual(citation["document_id"], item["document_id"])
        self.assertEqual(self.client.get(citation["download_url"]).status_code, 200)
        answer = self.client.post(
            "/api/v1/answers",
            json={"knowledge_base_ids": [self.kb], "query": "Atlas 超时"},
        )
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(answer.json()["status"], "completed")
        self.assertIn("[1]", answer.json()["answer"])
        removed = self.client.delete(f"/api/v1/documents/{item['document_id']}")
        self.assertEqual(removed.status_code, 202, removed.text)
        self.assertEqual(self.search().json()["hits"], [])
        self.assertEqual(self.client.get(citation["download_url"]).status_code, 404)
        self.assertTrue(self.worker.run_once())
        self.assertEqual(
            self.client.get(f"/api/v1/jobs/{removed.json()['job_id']}").json()[
                "status"
            ],
            "READY",
        )

    def test_upload_idempotency_and_replacement_visibility(self):
        item = self.upload()
        replay = self.upload()
        self.assertEqual(item["version_id"], replay["version_id"])
        self.worker.run_once()
        new = self.upload(
            "Atlas 服务默认超时为 90 秒。",
            key="replacement",
            document_id=item["document_id"],
        )
        self.assertEqual(
            self.search().json()["citations"][0]["version_id"], item["version_id"]
        )
        self.worker.run_once()
        self.assertEqual(
            self.search().json()["citations"][0]["version_id"], new["version_id"]
        )

    def test_stream_conversation_feedback_and_replay(self):
        self.upload()
        self.worker.run_once()
        conversation = self.client.post(
            "/api/v1/conversations", json={"knowledge_base_ids": [self.kb]}
        ).json()
        payload = {
            "knowledge_base_ids": [self.kb],
            "query": "Atlas 超时",
            "conversation_id": conversation["id"],
            "stream": True,
        }
        response = self.client.post(
            "/api/v1/answers", json=payload, headers={"Idempotency-Key": "answer-1"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("event: citations", response.text)
        self.assertIn("event: done", response.text)
        last = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ][-1]
        replay = self.client.post(
            "/api/v1/answers", json=payload, headers={"Idempotency-Key": "answer-1"}
        )
        self.assertEqual(replay.json()["request_id"], last["request_id"])
        history = self.client.get(f"/api/v1/conversations/{conversation['id']}").json()
        self.assertEqual(len(history["messages"]), 2)
        feedback = self.client.post(
            f"/api/v1/answers/{last['request_id']}/feedback",
            json={"rating": "positive"},
        )
        self.assertEqual(feedback.status_code, 201, feedback.text)

    def test_no_evidence_and_unknown_query_fields(self):
        response = self.client.post(
            "/api/v1/answers", json={"knowledge_base_ids": [self.kb], "query": "未知"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "no_answer")
        self.assertEqual(self.search(tenant_id="other").status_code, 422)

    def test_jwt_scope_and_stored_roles(self):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = (
            private.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )
        settings = self.settings.model_copy(
            update={
                "auth_mode": "jwt",
                "jwt_public_key": public,
                "jwt_issuer": "test",
                "jwt_audience": "rag",
            }
        )
        self.repository.ensure_membership(
            AuthContext("other", "default", "reader", ("reader",))
        )
        claims = {
            "sub": "reader",
            "tenant_id": "other",
            "project_id": "default",
            "iss": "test",
            "aud": "rag",
            "iat": time.time(),
            "exp": time.time() + 60,
            "roles": ["owner"],
        }
        token = jwt.encode(claims, private, algorithm="RS256")
        with TestClient(create_app(settings, self.repository)) as client:
            self.assertEqual(client.get("/api/v1/knowledge-bases").status_code, 401)
            response = client.get(
                f"/api/v1/knowledge-bases/{self.kb}",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(response.status_code, 404, response.text)
            claims["sub"] = "unprovisioned"
            invalid = jwt.encode(claims, private, algorithm="RS256")
            self.assertEqual(
                client.get(
                    "/api/v1/knowledge-bases",
                    headers={"Authorization": f"Bearer {invalid}"},
                ).status_code,
                403,
            )

    def test_production_rejects_dev_modes(self):
        with self.assertRaises(ValueError):
            Settings(_env_file=None, environment="production", model_mode="mock")

    def test_mock_vectors_cannot_share_live_model_identity(self):
        self.assertTrue(self.settings.embedding_model.startswith("mock:"))
        live = Settings(_env_file=None, environment="test", model_mode="live")
        self.assertNotEqual(self.settings.embedding_model, live.embedding_model)

    def test_generation_build_can_be_aborted_and_releases_uploads(self):
        self.upload()
        self.worker.run_once()
        base = f"/api/v1/knowledge-bases/{self.kb}/index-generations"
        response = self.client.post(
            base,
            json={
                "embedding_model": self.settings.embedding_model,
                "embedding_revision": "2",
                "embedding_dimension": 32,
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        generation = response.json()["generation_id"]
        self.assertEqual(self.client.get(f"{base}/{generation}").status_code, 200)
        blocked = self.client.post(
            f"/api/v1/knowledge-bases/{self.kb}/documents",
            files={"file": ("paused.txt", b"content", "text/plain")},
        )
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(
            self.client.post(f"{base}/{generation}/abort").status_code, 200
        )
        self.upload("After cancellation uploads resume", key="after-abort")


if __name__ == "__main__":
    unittest.main()
