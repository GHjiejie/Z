"""Real PostgreSQL/pgvector RAG checks; fixtures use an isolated tenant per test.

Run with RAG_TEST_DATABASE_URL pointing to a dedicated test database. Models are
deterministic mocks: these checks verify retrieval/ACL wiring, not answer quality.
"""

import asyncio
import hashlib
import os
import time
import unittest
import uuid

from sqlalchemy import delete, update

from llamaindex_service.config import Settings
from llamaindex_service.contracts import AuthContext, ServiceError
from llamaindex_service.generation.service import RAGService
from llamaindex_service.persistence import Repository
from llamaindex_service.persistence import models as m
from llamaindex_service.retrieval.embeddings import create_embedding
from llamaindex_service.retrieval.lexical import lexical_text


@unittest.skipUnless(
    os.getenv("RAG_TEST_DATABASE_URL"),
    "Set RAG_TEST_DATABASE_URL for real PostgreSQL checks",
)
class PostgresRAGTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = Settings(
            _env_file=None,
            environment="test",
            model_mode="mock",
            embedding_dimension=16,
            embedding_model="pg-rag-test",
            database_url=os.environ["RAG_TEST_DATABASE_URL"],
        )
        self.repository = Repository(self.settings)
        self.assertTrue(self.repository.postgres, "These checks require PostgreSQL")
        self.repository.create_schema()
        self.tenant = "rag-test-" + uuid.uuid4().hex
        self.addCleanup(self.cleanup_scope)
        self.owner = AuthContext(self.tenant, "project", "owner")
        self.reader = AuthContext(self.tenant, "project", "reader")
        self.outsider = AuthContext(self.tenant, "other-project", "reader")
        for context in (self.owner, self.reader, self.outsider):
            self.repository.ensure_membership(context)
        self.kb = self.repository.create_kb(self.owner, "RAG integration fixture")
        self.repository.set_kb_permissions(
            self.owner, self.kb["id"], {"owner": "owner", "reader": "reader"}
        )
        self.embedding = create_embedding(self.settings)
        self.service = RAGService(self.settings, self.repository)
        self.request = {
            "knowledge_base_ids": [self.kb["id"]],
            "query": "报销审批需要什么材料？",
        }
        self.original = "报销审批需要发票，费用上限为 300 元。"
        self.uploaded = self.upload(self.original)
        self.publish(self.uploaded, self.original)

    def cleanup_scope(self):
        try:
            with self.repository.engine.begin() as connection:
                for table in reversed(m.metadata.sorted_tables):
                    if "tenant_id" in table.c:
                        connection.execute(
                            delete(table).where(table.c.tenant_id == self.tenant)
                        )
        finally:
            self.repository.engine.dispose()

    def upload(self, text, document_id=None):
        return self.repository.upload_version(
            self.owner,
            self.kb["id"],
            "policy.md",
            "objects/" + uuid.uuid4().hex + ".md",
            hashlib.sha256(text.encode()).hexdigest(),
            len(text.encode()),
            "text/markdown",
            document_id=document_id,
        )

    def publish(self, uploaded, text):
        # Claim only our fixture job directly. A global worker claim could consume
        # another test's pending work when suites share the test database.
        token = uuid.uuid4().hex
        with self.repository.engine.begin() as connection:
            connection.execute(
                update(m.jobs)
                .where(
                    m.jobs.c.id == uploaded["job_id"],
                    m.jobs.c.tenant_id == self.tenant,
                )
                .values(
                    status="RUNNING",
                    claim_token=token,
                    lease_until=time.time() + 120,
                    attempts=1,
                )
            )
        result = self.repository.publish_chunks(
            uploaded["job_id"],
            token,
            [
                {
                    "text": text,
                    "lexical_text": lexical_text(text),
                    "embedding": self.embedding.get_text_embedding(text),
                    "metadata": {"page": 1, "section": "Expenses"},
                }
            ],
        )
        self.assertEqual(result["status"], "READY")

    async def test_hybrid_chinese_retrieval_and_cited_answer(self):
        result = await self.service.search(self.reader, self.request)
        self.assertTrue(result["hits"])
        self.assertIn("lexical", result["hits"][0]["scores"])
        self.assertIn("vector", result["hits"][0]["scores"])
        self.assertEqual(result["citations"][0]["page"], 1)
        self.assertEqual(
            result["citations"][0]["version_id"], self.uploaded["version_id"]
        )
        events = [
            event
            async for event in self.service.stream_answer(self.reader, self.request)
        ]
        self.assertEqual(events[-1]["event"], "done")
        self.assertEqual(events[-1]["data"]["status"], "completed")
        self.assertIn("[1]", events[-1]["data"]["answer"])

    async def test_cross_project_scope_is_rejected(self):
        with self.assertRaises(ServiceError) as error:
            await self.service.search(self.outsider, self.request)
        self.assertEqual(error.exception.status_code, 404)

    async def test_committed_acl_revocation_stops_next_stream_token(self):
        owner, repository, document_id = (
            self.owner,
            self.repository,
            self.uploaded["document_id"],
        )

        class Revoker:
            async def stream(self, messages):
                yield {"delta": "允许的首段 [1]"}
                await asyncio.to_thread(
                    repository.set_document_permissions,
                    owner,
                    document_id,
                    allowed_users=["owner"],
                    allowed_roles=[],
                )
                yield {"delta": "禁止送出的后续内容"}

        service = RAGService(self.settings, self.repository, generator=Revoker())
        events = [
            event async for event in service.stream_answer(self.reader, self.request)
        ]
        self.assertEqual(events[-1]["data"]["code"], "EVIDENCE_CHANGED")
        self.assertNotIn("禁止送出", str(events))
        self.assertEqual(
            (await self.service.search(self.reader, self.request))["hits"], []
        )

    async def test_replacement_publication_and_deletion(self):
        previous = await self.service.search(self.owner, self.request)
        replacement_text = "报销审批需要发票，费用上限为 900 元。"
        replacement = self.upload(replacement_text, self.uploaded["document_id"])
        during = await self.service.search(self.owner, self.request)
        self.assertEqual(
            during["citations"][0]["version_id"], self.uploaded["version_id"]
        )
        self.publish(replacement, replacement_text)
        after = await self.service.search(self.owner, self.request)
        self.assertEqual(after["citations"][0]["version_id"], replacement["version_id"])
        self.assertFalse(self.repository.revalidate(self.owner, previous["citations"]))
        self.repository.delete_document(self.owner, self.uploaded["document_id"])
        self.assertEqual(
            (await self.service.search(self.owner, self.request))["hits"], []
        )


if __name__ == "__main__":
    unittest.main()
