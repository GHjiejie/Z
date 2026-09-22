"""Authorization, atomic publication and durable idempotency regression coverage."""

import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import event, insert, select, text, update

from llamaindex_service.config import Settings
from llamaindex_service.contracts import AuthContext, ServiceError
from llamaindex_service.persistence import Repository
from llamaindex_service.persistence import models as m


class RepositoryChecks:
    database_url = "sqlite+pysqlite:///:memory:"

    def setUp(self):
        self.repository = Repository(
            Settings(
                database_url=self.database_url,
                environment="test",
                embedding_dimension=8,
                embedding_model="test-embed",
                embedding_revision="1",
            )
        )
        self.repository.create_schema()
        self.ctx = AuthContext(uuid4().hex, "test", "owner", ("staff",))
        self.reader = replace(self.ctx, user_id="reader", roles=())
        for ctx in (self.ctx, self.reader):
            self.repository.ensure_membership(ctx)
        self.kb = self.repository.create_kb(self.ctx, "制度")
        self.repository.set_kb_permissions(
            self.ctx, self.kb["id"], {"owner": "owner", "reader": "reader"}
        )

    def tearDown(self):
        self.repository.engine.dispose()

    def upload(self, document_id=None, key=None):
        return self.repository.upload_version(
            self.ctx,
            self.kb["id"],
            "制度.md",
            uuid4().hex,
            "a" * 64,
            10,
            "text/markdown",
            document_id=document_id,
            idempotency_key=key,
        )

    def publish(self, upload):
        # Claim only the scoped job without touching other integration tests' queue.
        token = uuid4().hex
        with self.repository.engine.begin() as connection:
            connection.execute(
                update(m.jobs)
                .where(m.jobs.c.id == upload["job_id"])
                .values(
                    status="RUNNING",
                    claim_token=token,
                    lease_until=time.time() + 120,
                    attempts=1,
                )
            )
        chunk_id = uuid4().hex
        self.repository.publish_chunks(
            upload["job_id"],
            token,
            [
                {
                    "id": chunk_id,
                    "text": "员工报销需要发票",
                    "lexical_text": "员工 报销 需要 发票",
                    "embedding": [1.0] + [0.0] * 7,
                    "metadata": {"page": "1", "section": "报销"},
                }
            ],
        )
        return chunk_id

    def search(self, ctx=None):
        return self.repository.search_candidates(
            ctx or self.ctx,
            [self.kb["id"]],
            [],
            [1.0] + [0.0] * 7,
            "报销 unrelated",
            top_k=10,
        )

    def test_scope_acl_before_vector_and_lexical(self):
        upload = self.upload()
        chunk_id = self.publish(upload)
        self.assertEqual(self.search(self.reader)["vector"][0]["id"], chunk_id)
        self.assertEqual(self.search(self.reader)["lexical"][0]["id"], chunk_id)
        self.repository.set_document_permissions(
            self.ctx, upload["document_id"], ["owner"]
        )
        self.assertEqual(self.search(self.reader), {"vector": [], "lexical": []})
        other = replace(self.reader, tenant_id="different")
        self.repository.ensure_membership(other)
        with self.assertRaises(ServiceError) as raised:
            self.search(other)
        self.assertEqual(raised.exception.status_code, 404)

    def test_role_acl_uses_fresh_membership(self):
        upload = self.upload()
        chunk_id = self.publish(upload)
        self.repository.set_document_permissions(
            self.ctx, upload["document_id"], allowed_users=[], allowed_roles=["staff"]
        )
        self.assertEqual(self.search()["vector"][0]["id"], chunk_id)
        self.repository.ensure_membership(replace(self.ctx, roles=()))
        self.assertFalse(self.repository.revalidate(self.ctx, [{"chunk_id": chunk_id}]))
        self.assertEqual(self.search()["vector"], [])

    def test_download_rechecks_roles_and_membership_after_request_started(self):
        upload = self.upload()
        self.publish(upload)
        self.repository.set_document_permissions(
            self.ctx, upload["document_id"], allowed_users=[], allowed_roles=["staff"]
        )
        self.repository.get_content(
            self.ctx, upload["document_id"], upload["version_id"]
        )
        self.repository.ensure_membership(replace(self.ctx, roles=()))
        with self.assertRaises(ServiceError) as raised:
            self.repository.get_content(
                self.ctx, upload["document_id"], upload["version_id"]
            )
        self.assertEqual(raised.exception.status_code, 404)
        self.repository.ensure_membership(self.ctx)
        self.revoke_membership()
        with self.assertRaises(ServiceError) as raised:
            self.repository.get_content(
                self.ctx, upload["document_id"], upload["version_id"]
            )
        self.assertEqual(raised.exception.code, "MEMBERSHIP_REVOKED")

    def revoke_membership(self):
        with self.repository.engine.begin() as connection:
            connection.execute(
                update(m.memberships)
                .where(
                    m.memberships.c.tenant_id == self.ctx.tenant_id,
                    m.memberships.c.project_id == self.ctx.project_id,
                    m.memberships.c.user_id == self.ctx.user_id,
                )
                .values(active=False)
            )

    def test_upload_refreshes_roles_and_membership_before_mutation(self):
        upload = self.upload()
        self.repository.set_document_permissions(
            self.ctx, upload["document_id"], allowed_users=[], allowed_roles=["staff"]
        )
        self.repository.ensure_membership(replace(self.ctx, roles=()))
        with self.assertRaises(ServiceError) as raised:
            self.upload(upload["document_id"])
        self.assertEqual(raised.exception.status_code, 404)
        self.revoke_membership()
        with self.assertRaises(ServiceError) as raised:
            self.upload()
        self.assertEqual(raised.exception.code, "MEMBERSHIP_REVOKED")

    def test_membership_revoked_before_no_answer_finalization_is_persisted(self):
        run = self.repository.begin_query(
            self.ctx,
            uuid4().hex,
            {"query": "报销", "knowledge_base_ids": [self.kb["id"]]},
        )
        self.revoke_membership()
        verdict = self.repository.finish_query(
            self.ctx, run["id"], "no_answer", {"answer": "证据不足", "citations": []}
        )
        self.assertEqual(verdict["status"], "failed")
        self.assertEqual(verdict["error_code"], "EVIDENCE_REVOKED")
        with self.repository.engine.connect() as connection:
            persisted = (
                connection.execute(
                    select(m.query_runs).where(m.query_runs.c.id == run["id"])
                )
                .mappings()
                .one()
            )
        self.assertEqual(persisted["status"], "failed")
        self.assertIsNone(persisted["result"])

    def test_knowledge_base_revoked_before_empty_answer_finalization(self):
        run = self.repository.begin_query(
            self.reader,
            uuid4().hex,
            {"query": "报销", "knowledge_base_ids": [self.kb["id"]]},
        )
        self.repository.set_kb_permissions(self.ctx, self.kb["id"], {"owner": "owner"})
        verdict = self.repository.finish_query(
            self.reader, run["id"], "no_answer", {"answer": "证据不足", "citations": []}
        )
        self.assertEqual(
            (verdict["status"], verdict["error_code"]), ("failed", "EVIDENCE_REVOKED")
        )

    def test_deleted_conversation_finalization_keeps_tombstone_and_invalidates(self):
        conversation = self.repository.create_conversation(self.ctx, [self.kb["id"]])
        run = self.repository.begin_query(
            self.ctx,
            uuid4().hex,
            {
                "query": "报销",
                "knowledge_base_ids": [self.kb["id"]],
                "conversation_id": conversation["id"],
            },
        )
        self.repository.delete_conversation(self.ctx, conversation["id"])
        verdict = self.repository.finish_query(
            self.ctx, run["id"], "no_answer", {"answer": "证据不足", "citations": []}
        )
        self.assertEqual(
            (verdict["status"], verdict["error_code"]), ("deleted", "EVIDENCE_REVOKED")
        )
        self.assertIsNone(verdict["result"])
        with self.repository.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    select(m.messages).where(
                        m.messages.c.conversation_id == conversation["id"]
                    )
                ).all(),
                [],
            )

    def test_finalization_authorization_failure_rolls_back_partial_turn(self):
        conversation = self.repository.create_conversation(self.ctx, [self.kb["id"]])
        run = self.repository.begin_query(
            self.ctx,
            uuid4().hex,
            {
                "query": "报销",
                "knowledge_base_ids": [self.kb["id"]],
                "conversation_id": conversation["id"],
            },
        )

        def partial_turn(connection, ctx, conversation_id, question, answer, citations):
            connection.execute(
                insert(m.messages).values(
                    id=uuid4().hex,
                    tenant_id=ctx.tenant_id,
                    project_id=ctx.project_id,
                    created_at=time.time(),
                    conversation_id=conversation_id,
                    role="user",
                    content=question,
                    citations=citations,
                )
            )
            raise ServiceError("NOT_FOUND", "会话已撤销", 404)

        with patch.object(self.repository, "_add_turn", side_effect=partial_turn):
            verdict = self.repository.finish_query(
                self.ctx,
                run["id"],
                "no_answer",
                {"answer": "证据不足", "citations": []},
            )
        self.assertEqual(
            (verdict["status"], verdict["error_code"]), ("failed", "EVIDENCE_REVOKED")
        )
        self.assertEqual(
            self.repository.conversation_history(self.ctx, conversation["id"]), []
        )
        self.assertEqual(
            self.repository.get_query(self.ctx, run["id"])["status"], "failed"
        )

    def test_failed_update_keeps_old_version_and_cas_cancels_stale(self):
        first = self.upload()
        first_chunk = self.publish(first)
        second = self.upload(first["document_id"])
        third = self.upload(first["document_id"])
        self.publish(second)
        self.assertEqual(self.search()["vector"][0]["id"], first_chunk)
        latest_chunk = self.publish(third)
        self.assertEqual(self.search()["vector"][0]["id"], latest_chunk)
        self.assertEqual(
            self.repository.get_job(self.ctx, second["job_id"])["status"], "CANCELLED"
        )

    def test_expired_fence_cannot_publish(self):
        upload = self.upload()
        token = uuid4().hex
        with self.repository.engine.begin() as connection:
            connection.execute(
                update(m.jobs)
                .where(m.jobs.c.id == upload["job_id"])
                .values(
                    status="RUNNING", claim_token=token, lease_until=time.time() - 1
                )
            )
        self.assertFalse(self.repository.heartbeat(upload["job_id"], token))
        with self.assertRaises(ServiceError) as raised:
            self.repository.publish_chunks(upload["job_id"], token, [])
        self.assertEqual(raised.exception.code, "STALE_JOB_CLAIM")

    def test_delete_hides_retrieval_and_historical_result(self):
        upload = self.upload()
        chunk_id = self.publish(upload)
        conversation = self.repository.create_conversation(self.ctx, [self.kb["id"]])
        payload = {
            "query": "报销",
            "knowledge_base_ids": [self.kb["id"]],
            "conversation_id": conversation["id"],
        }
        run = self.repository.begin_query(
            self.ctx, uuid4().hex, payload, idempotency_key="answer"
        )
        citations = [
            {
                "chunk_id": chunk_id,
                "document_id": upload["document_id"],
                "version_id": upload["version_id"],
            }
        ]
        self.repository.finish_query(
            self.ctx,
            run["id"],
            "completed",
            {"answer": "需要发票", "citations": citations},
        )
        self.assertEqual(
            len(self.repository.conversation_history(self.ctx, conversation["id"])), 2
        )
        self.repository.delete_document(self.ctx, upload["document_id"])
        self.assertEqual(self.search(), {"vector": [], "lexical": []})
        self.assertEqual(
            self.repository.conversation_history(self.ctx, conversation["id"]), []
        )
        with self.assertRaises(ServiceError) as raised:
            self.repository.begin_query(
                self.ctx, uuid4().hex, payload, idempotency_key="answer"
            )
        self.assertEqual(raised.exception.code, "EVIDENCE_REVOKED")

    def test_upload_idempotency_and_collision(self):
        first = self.upload(key="same")
        second = self.upload(key="same")
        self.assertEqual(first["version_id"], second["version_id"])
        self.assertTrue(second["replayed"])
        with self.assertRaises(ServiceError) as raised:
            self.repository.upload_version(
                self.ctx,
                self.kb["id"],
                "changed.md",
                "x",
                "b" * 64,
                11,
                "text/markdown",
                idempotency_key="same",
            )
        self.assertEqual(raised.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_query_admission_expiry_idempotency_and_no_answer(self):
        payload = {"query": "报销", "knowledge_base_ids": [self.kb["id"]]}
        first = self.repository.begin_query(
            self.ctx, uuid4().hex, payload, "first", max_concurrent=1
        )
        replay = self.repository.begin_query(
            self.ctx, uuid4().hex, payload, "first", max_concurrent=1
        )
        self.assertTrue(replay["replayed"])
        with self.assertRaises(ServiceError) as raised:
            self.repository.begin_query(
                self.ctx, uuid4().hex, payload, max_concurrent=1
            )
        self.assertEqual(raised.exception.status_code, 429)
        self.repository.finish_query(
            self.ctx, first["id"], "no_answer", {"answer": "证据不足", "citations": []}
        )
        self.assertEqual(
            self.repository.get_query(self.ctx, first["id"])["status"], "no_answer"
        )
        second = self.repository.begin_query(
            self.ctx, uuid4().hex, payload, "second", max_concurrent=1, lease_seconds=-1
        )
        self.assertEqual(
            self.repository.get_query(self.ctx, second["id"])["status"], "interrupted"
        )
        self.repository.begin_query(self.ctx, uuid4().hex, payload, max_concurrent=1)

    def test_model_mismatch_and_metadata_whitelist(self):
        upload = self.upload()
        self.publish(upload)
        with self.assertRaises(ServiceError):
            self.repository.search_candidates(
                self.ctx,
                [self.kb["id"]],
                [],
                [1.0] * 8,
                "报销",
                filters={"tenant_id": "evil"},
            )
        self.repository.model = "different"
        with self.assertRaises(ServiceError) as raised:
            self.search()
        self.assertEqual(raised.exception.code, "INDEX_MODEL_MISMATCH")

    def test_persistent_rate_limit(self):
        self.repository.check_rate_limit(self.ctx, limit=1)
        with self.assertRaises(ServiceError) as raised:
            self.repository.check_rate_limit(self.ctx, limit=1)
        self.assertEqual(raised.exception.status_code, 429)

    def test_generation_build_atomic_switch_and_abort(self):
        upload = self.upload()
        old_chunk = self.publish(upload)
        staged = self.repository.stage_generation(
            self.ctx, self.kb["id"], "test-next", "2", 8
        )
        with self.assertRaises(ServiceError) as raised:
            self.upload()
        self.assertEqual(raised.exception.code, "INDEX_REBUILD_IN_PROGRESS")
        with self.assertRaises(ServiceError) as raised:
            self.repository.activate_generation(
                self.ctx, self.kb["id"], staged["generation_id"]
            )
        self.assertEqual(raised.exception.code, "GENERATION_NOT_READY")
        token = uuid4().hex
        job_id = staged["job_ids"][0]
        with self.repository.engine.begin() as connection:
            connection.execute(
                update(m.jobs)
                .where(m.jobs.c.id == job_id)
                .values(
                    status="RUNNING",
                    claim_token=token,
                    lease_until=time.time() + 120,
                    attempts=1,
                )
            )
        self.repository.model, self.repository.revision = "test-next", "2"
        new_chunk = uuid4().hex
        self.repository.publish_chunks(
            job_id,
            token,
            [
                {
                    "id": new_chunk,
                    "text": "新索引同一原文",
                    "lexical_text": "报销",
                    "embedding": [1.0] + [0.0] * 7,
                    "metadata": {},
                }
            ],
        )
        self.repository.model, self.repository.revision = "test-embed", "1"
        self.assertEqual(self.search()["vector"][0]["id"], old_chunk)
        self.repository.activate_generation(
            self.ctx, self.kb["id"], staged["generation_id"]
        )
        with self.assertRaises(ServiceError) as raised:
            self.search()
        self.assertEqual(raised.exception.code, "INDEX_MODEL_MISMATCH")
        self.repository.model, self.repository.revision = "test-next", "2"
        self.assertEqual(self.search()["vector"][0]["id"], new_chunk)
        self.assertFalse(
            self.repository.revalidate(self.ctx, [{"chunk_id": old_chunk}])
        )
        abandoned = self.repository.stage_generation(
            self.ctx, self.kb["id"], "third", "3", 8
        )
        self.repository.abort_generation(
            self.ctx, self.kb["id"], abandoned["generation_id"]
        )
        self.assertEqual(self.search()["vector"][0]["id"], new_chunk)
        self.upload()

    def test_orphan_protocol_preserves_referenced_and_rejects_old_finalize(self):
        key = "source/" + uuid4().hex
        self.repository.upload_version(
            self.ctx,
            self.kb["id"],
            "f.md",
            key,
            "b" * 64,
            4,
            "text/markdown",
            object_created_at=time.time(),
        )
        deleted = []
        self.assertFalse(self.repository.delete_orphan_object(key, deleted.append))
        self.assertEqual(deleted, [])
        self.assertTrue(
            self.repository.delete_orphan_object("orphan/" + key, deleted.append)
        )
        with self.assertRaises(ServiceError) as raised:
            self.repository.upload_version(
                self.ctx,
                self.kb["id"],
                "old.md",
                "old/" + key,
                "c" * 64,
                4,
                "text/markdown",
                object_created_at=time.time() - 3601,
            )
        self.assertEqual(raised.exception.code, "UPLOAD_EXPIRED")


class SQLiteRepositoryTests(RepositoryChecks, unittest.TestCase):
    pass


@unittest.skipUnless(
    os.getenv("RAG_TEST_DATABASE_URL"),
    "set RAG_TEST_DATABASE_URL for PostgreSQL integration",
)
class PostgreSQLRepositoryTests(RepositoryChecks, unittest.TestCase):
    database_url = os.getenv("RAG_TEST_DATABASE_URL", "sqlite+pysqlite:///:memory:")

    def test_runtime_roles_health_and_permission_queries(self):
        settings = self.repository.settings.model_copy(
            update={"environment": "production"}
        )
        api = Repository(settings)
        worker = Repository(settings)
        self.addCleanup(api.engine.dispose)
        self.addCleanup(worker.engine.dispose)

        @event.listens_for(api.engine, "connect")
        def api_role(connection, _):
            connection.execute("SET ROLE rag_api")
            connection.commit()

        @event.listens_for(worker.engine, "connect")
        def worker_role(connection, _):
            connection.execute("SET ROLE rag_worker")
            connection.commit()

        self.assertTrue(api.health())
        self.assertTrue(worker.health(worker=True))
        with self.assertRaises(ServiceError):
            worker.health()
        with self.assertRaises(ServiceError):
            api.health(worker=True)
        self.assertEqual(api.get_kb(self.ctx, self.kb["id"])["id"], self.kb["id"])
        self.assertEqual(api.list_kbs(replace(self.ctx, tenant_id="other")), [])
        kb = api.create_kb(self.ctx, "runtime role")
        self.assertEqual(api.get_kb(self.ctx, kb["id"])["name"], "runtime role")
        with api.engine.begin() as connection:
            self.assertEqual(
                connection.execute(
                    select(m.knowledge_bases).where(m.knowledge_bases.c.id == kb["id"])
                ).all(),
                [],
            )

    def test_query_admission_across_connections(self):
        payload = {"query": "并发", "knowledge_base_ids": [self.kb["id"]]}

        def submit():
            try:
                self.repository.begin_query(
                    self.ctx, uuid4().hex, payload, max_concurrent=1
                )
                return "accepted"
            except ServiceError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: submit(), range(2)))
        self.assertCountEqual(outcomes, ["accepted", "CONCURRENCY_LIMIT"])

    def test_rls_scope_and_transaction_local_reset(self):
        with self.repository.engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE rag_api"))
            self.assertEqual(
                connection.execute(
                    select(m.knowledge_bases).where(
                        m.knowledge_bases.c.id == self.kb["id"]
                    )
                ).all(),
                [],
            )
            connection.execute(
                text(
                    "SELECT set_config('rag.tenant_id', :tenant, true), set_config('rag.project_id', :project, true)"
                ),
                {"tenant": self.ctx.tenant_id, "project": self.ctx.project_id},
            )
            self.assertEqual(
                len(
                    connection.execute(
                        select(m.knowledge_bases).where(
                            m.knowledge_bases.c.id == self.kb["id"]
                        )
                    ).all()
                ),
                1,
            )
        with self.repository.engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE rag_api"))
            self.assertEqual(
                connection.execute(
                    select(m.knowledge_bases).where(
                        m.knowledge_bases.c.id == self.kb["id"]
                    )
                ).all(),
                [],
            )

    def test_claim_skip_locked_and_fencing(self):
        upload = self.upload()
        # Restrict test queue claim by making its item first, without modifying peers.
        with self.repository.engine.begin() as connection:
            connection.execute(
                update(m.jobs)
                .where(m.jobs.c.id == upload["job_id"])
                .values(available_at=-1000)
            )
        first = self.repository.claim_job("worker-1", 30)
        self.assertEqual(first["id"], upload["job_id"])
        with self.repository.engine.begin() as connection:
            connection.execute(
                update(m.jobs)
                .where(m.jobs.c.id == first["id"])
                .values(lease_until=time.time() - 1)
            )
        second = self.repository.claim_job("worker-2", 30)
        self.assertEqual(second["id"], first["id"])
        self.assertNotEqual(first["claim_token"], second["claim_token"])
        self.assertFalse(self.repository.heartbeat(first["id"], first["claim_token"]))
        self.assertTrue(self.repository.heartbeat(second["id"], second["claim_token"]))
        self.repository.fail_job(
            second["id"], second["claim_token"], "TEST_STOP", "测试完成"
        )


if __name__ == "__main__":
    unittest.main()
