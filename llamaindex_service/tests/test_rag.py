"""Offline behavior/security tests; these are not semantic quality benchmarks."""

import asyncio
import sys
import unittest
from typing import ClassVar
from unittest.mock import patch

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from llama_index.core.base.llms.types import ChatMessage
from pydantic import PrivateAttr

from llamaindex_service.config import Settings
from llamaindex_service.generation.models import AsyncLangChainLLM, content_text
from llamaindex_service.generation.service import (
    NO_ANSWER,
    RAGService,
    bounded_hits,
    validate_citation_ids,
)
from llamaindex_service.retrieval.embeddings import (
    EmbeddingError,
    GatewayEmbedding,
    create_embedding,
)
from llamaindex_service.retrieval.lexical import lexical_text, tokenize
from llamaindex_service.retrieval.retriever import reciprocal_rank_fusion


def settings(**kwargs):
    return Settings(
        _env_file=None,
        model_mode="mock",
        environment="test",
        embedding_dimension=32,
        embedding_model="hash-test",
        **kwargs,
    )


def hit(
    chunk="chunk-1",
    text="报销审批需要发票。Expense approval requires invoices.",
    score=0.9,
):
    return {
        "id": chunk,
        "chunk_id": chunk,
        "document_id": "doc-1",
        "version_id": "v-1",
        "generation_id": "gen-1",
        "kb_id": "kb-1",
        "text": text,
        "metadata": {"page": 2, "section": "Expenses"},
        "title": "Policy",
        "score": score,
    }


class Repository:
    def __init__(self, hits=None):
        self.hits = [hit()] if hits is None else hits
        self.valid = True
        self.queries = []

    def search_candidates(self, *args, **kwargs):
        self.queries.append((args, kwargs))
        return {"vector": self.hits, "lexical": self.hits}

    def revalidate(self, ctx, citations):
        return self.valid


class Generator:
    def __init__(self, parts=None, error=False, repo=None):
        self.parts = ["发票需要审批。 [1]"] if parts is None else parts
        self.error, self.repo = error, repo
        self.called, self.closed = False, False
        self.messages = []

    async def stream(self, messages):
        self.called, self.messages = True, messages
        try:
            if self.error:
                raise RuntimeError("secret upstream failure text")
            for index, part in enumerate(self.parts):
                if self.repo is not None and index == 1:
                    self.repo.valid = False
                yield {
                    "delta": part,
                    "usage": {
                        "input_tokens": 30,
                        "output_tokens": 5,
                        "total_tokens": 35,
                    },
                }
        finally:
            self.closed = True


class ResponsesModel(BaseChatModel):
    """LangChain Responses-shaped output; synchronous calls deliberately fail."""

    _closed: bool = PrivateAttr(default=False)

    @property
    def _llm_type(self):
        return "responses-test"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise AssertionError("Blocking synchronous adapter used")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content=[
                            {"type": "text", "text": "Answer [1]"},
                            {"type": "reasoning", "summary": [{"text": "private"}]},
                        ],
                        usage_metadata={
                            "input_tokens": 10,
                            "output_tokens": 3,
                            "total_tokens": 13,
                        },
                    )
                )
            ]
        )

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        try:
            yield ChatGenerationChunk(
                message=AIMessageChunk(content=[{"type": "text", "text": "Answer "}])
            )
            await asyncio.sleep(0)
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content=[{"type": "output_text", "text": "[1]"}],
                    usage_metadata={
                        "input_tokens": 10,
                        "output_tokens": 3,
                        "total_tokens": 13,
                    },
                )
            )
        finally:
            self._closed = True


class RAGUnitTests(unittest.TestCase):
    def test_mixed_language_terms_and_identifiers(self):
        terms = tokenize("报销审批 API /v1/documents ABC-123")
        self.assertIn("报销", terms)
        self.assertIn("审批", terms)
        self.assertIn("abc-123", terms)
        self.assertIn("documents", terms)
        self.assertEqual(lexical_text("知识库"), "知识 识库")

    def test_rrf_deduplicates_one_vote_per_branch(self):
        fused = reciprocal_rank_fusion(
            {"vector": [hit(), hit()], "lexical": [hit()]}, 10
        )
        self.assertEqual(len(fused), 1)
        self.assertAlmostEqual(fused[0]["score"], 2 / 61)
        self.assertEqual(set(fused[0]["scores"]), {"vector", "lexical"})

    def test_context_bound_is_respected_without_invalid_unicode(self):
        selected = bounded_hits([hit(text="中文" * 10000), hit("c2", "other")], 8, 1000)
        self.assertLessEqual(
            sum(len(item["text"].encode()) + 128 for item in selected) + 256, 1000
        )
        self.assertNotIn("�", selected[0]["text"])

    def test_reference_numbers_do_not_imply_semantic_support(self):
        citations = [{"citation_id": "1"}, {"citation_id": "2"}]
        self.assertTrue(validate_citation_ids("Supported [1,2]", citations))
        self.assertFalse(validate_citation_ids("Fabrication [777]", citations))
        self.assertFalse(validate_citation_ids("No references", citations))

    def test_mock_is_deterministic_and_does_not_import_chat_credentials(self):
        previous = "chat_models.chat" in sys.modules
        model = create_embedding(settings())
        self.assertEqual(
            model.get_query_embedding("报销审批"), model.get_text_embedding("报销审批")
        )
        self.assertEqual(len(model.get_text_embedding("text")), 32)
        self.assertEqual("chat_models.chat" in sys.modules, previous)

    def test_embedding_response_indices_dimensions_and_nan_rejected(self):
        model = GatewayEmbedding(
            model_name="m", dimension=2, base_url="https://example.invalid/v1"
        )
        for data in [
            [{"index": 0, "embedding": [1]}],
            [{"index": 1, "embedding": [1, 2]}],
            [{"index": 0, "embedding": [0, 0]}],
        ]:
            response = httpx.Response(
                200,
                json={"data": data},
                request=httpx.Request("POST", "https://example.invalid"),
            )
            with self.assertRaises(EmbeddingError):
                model._decode(response, 1)
        response = httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0, 1]},
                    {"index": 0, "embedding": [1, 0]},
                ]
            },
            request=httpx.Request("POST", "https://example.invalid"),
        )
        self.assertEqual(model._decode(response, 2), [[1, 0], [0, 1]])

    def test_only_visible_responses_text_survives(self):
        self.assertEqual(
            content_text(
                [
                    {"type": "reasoning", "text": "secret"},
                    {"type": "tool_call", "text": "danger"},
                    {"type": "output_text", "text": "visible"},
                ]
            ),
            "visible",
        )


class AsyncRAGTests(unittest.IsolatedAsyncioTestCase):
    request: ClassVar[dict] = {
        "query": "报销审批需要什么？",
        "knowledge_base_ids": ["kb-1"],
    }

    async def events(self, repo, generator=None, request=None):
        service = RAGService(settings(), repo, generator=generator)
        return [
            event
            async for event in service.stream_answer("auth", request or self.request)
        ]

    async def test_search_propagates_authorized_scope_to_repository(self):
        repo = Repository()
        request = {**self.request, "document_ids": ["doc-1"], "filters": {"page": 2}}
        result = await RAGService(settings(), repo).search("auth", request)
        self.assertEqual(repo.queries[0][0][:3], ("auth", ["kb-1"], ["doc-1"]))
        self.assertEqual(repo.queries[0][1]["filters"], {"page": 2})
        self.assertEqual(
            result["citations"][0]["download_url"],
            "/api/v1/documents/doc-1/versions/v-1/content",
        )
        self.assertEqual(result["citations"][0]["page"], 2)

    async def test_empty_evidence_never_calls_generator(self):
        generator = Generator()
        events = await self.events(Repository([]), generator)
        self.assertFalse(generator.called)
        self.assertEqual(events[-1]["event"], "done")
        self.assertEqual(events[-1]["data"]["status"], "no_answer")
        self.assertEqual(events[-1]["data"]["citations"], [])

    async def test_unrelated_hash_match_is_not_evidence(self):
        generator = Generator()
        events = await self.events(
            Repository([hit(text="Completely unrelated manual")]), generator
        )
        self.assertFalse(generator.called)
        self.assertEqual(events[-1]["data"]["status"], "no_answer")

    async def test_success_events_and_usage(self):
        generator = Generator()
        events = await self.events(Repository(), generator)
        self.assertEqual(
            [event["event"] for event in events],
            ["retrieval", "citations", "delta", "done"],
        )
        self.assertEqual(events[-1]["data"]["usage"]["total_tokens"], 35)
        self.assertTrue(generator.closed)

    async def test_model_can_explicitly_abstain_despite_candidate_evidence(self):
        events = await self.events(Repository(), Generator([NO_ANSWER]))
        self.assertEqual(events[-1]["event"], "done")
        self.assertEqual(events[-1]["data"]["status"], "no_answer")
        self.assertEqual(events[-1]["data"]["citations"], [])

    async def test_pure_search_honors_top_k_beyond_answer_context_limit(self):
        repo = Repository([hit(f"c{i}", f"报销审批证据 {i}") for i in range(10)])
        service = RAGService(settings(), repo)
        result = await service.search("auth", {**self.request, "top_k": 10})
        self.assertEqual(len(result["hits"]), 10)

    async def test_generation_timeout_is_error_and_closes_upstream(self):
        class WaitingGenerator:
            closed = False

            async def stream(self, messages):
                try:
                    await asyncio.sleep(60)
                    yield {"delta": "Never returned"}
                finally:
                    self.closed = True

        generator = WaitingGenerator()
        service = RAGService(
            settings(generation_timeout_seconds=0.01), Repository(), generator=generator
        )
        events = [event async for event in service.stream_answer("auth", self.request)]
        self.assertEqual(events[-1]["data"]["code"], "MODEL_TIMEOUT")
        self.assertTrue(generator.closed)

    async def test_fake_citation_invalidates_streamed_draft(self):
        events = await self.events(Repository(), Generator(["Fabricated [777]"]))
        self.assertEqual(events[-1]["event"], "error")
        self.assertEqual(events[-1]["data"]["code"], "INVALID_CITATIONS")
        self.assertFalse(any(event["event"] == "done" for event in events))

    async def test_revoke_after_model_wait_stops_remaining_tokens(self):
        repo = Repository()
        generator = Generator(["visible [1]", "must never be delivered"], repo=repo)
        events = await self.events(repo, generator)
        self.assertEqual(events[-1]["data"]["code"], "EVIDENCE_CHANGED")
        self.assertEqual(
            [event["data"]["text"] for event in events if event["event"] == "delta"],
            ["visible [1]"],
        )
        self.assertTrue(generator.closed)

    async def test_failure_does_not_become_no_answer_or_leak_upstream_details(self):
        events = await self.events(Repository(), Generator(error=True))
        self.assertEqual(events[-1]["data"]["code"], "GENERATION_FAILED")
        self.assertNotIn("secret", str(events))

    async def test_revoked_before_search_never_returns_content(self):
        repo = Repository()
        repo.valid = False
        events = await self.events(repo)
        self.assertEqual([event["event"] for event in events], ["error"])
        self.assertNotIn("发票", str(events))

    async def test_async_langchain_adapter_keeps_text_usage_and_closes_upstream(self):
        model = ResponsesModel()
        adapter = AsyncLangChainLLM(llm=model)
        result = await adapter.achat([ChatMessage(role="user", content="q")])
        self.assertEqual(result.message.content, "Answer [1]")
        self.assertEqual(result.additional_kwargs["usage"]["total_tokens"], 13)
        stream = await adapter.astream_chat([ChatMessage(role="user", content="q")])
        chunks = [chunk async for chunk in stream]
        self.assertEqual("".join(chunk.delta or "" for chunk in chunks), "Answer [1]")
        usage = [
            chunk.additional_kwargs["usage"]
            for chunk in chunks
            if chunk.additional_kwargs.get("usage")
        ]
        self.assertEqual(usage[-1]["total_tokens"], 13)
        self.assertTrue(model._closed)

    async def test_async_adapter_cancellation_closes_upstream(self):
        model = ResponsesModel()
        stream = await AsyncLangChainLLM(llm=model).astream_chat(
            [ChatMessage(role="user", content="q")]
        )
        await anext(stream)
        await stream.aclose()
        await asyncio.sleep(0)
        self.assertTrue(model._closed)

    async def test_reranker_failure_degrades_to_rrf(self):
        service = RAGService(
            settings(rerank_base_url="https://example.invalid/v1"), Repository()
        )
        with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("offline")):
            result = await service.search("auth", self.request)
        self.assertEqual(result["degraded"], ["reranker_unavailable"])
        self.assertEqual(len(result["hits"]), 1)

    async def test_reranker_cannot_forge_or_repeat_candidate_indices(self):
        service = RAGService(
            settings(rerank_base_url="https://example.invalid/v1"), Repository()
        )
        response = httpx.Response(
            200,
            json={"results": [{"index": 999, "relevance_score": 1.0}]},
            request=httpx.Request("POST", "https://example.invalid/v1/rerank"),
        )
        with patch("httpx.AsyncClient.post", return_value=response):
            result = await service.search("auth", self.request)
        self.assertEqual(result["degraded"], ["reranker_unavailable"])
        self.assertEqual(result["hits"][0]["chunk_id"], "chunk-1")

    async def test_reranker_uses_only_authorized_candidates_and_scores(self):
        service = RAGService(
            settings(rerank_base_url="https://example.invalid/v1"), Repository()
        )
        response = httpx.Response(
            200,
            json={"results": [{"index": 0, "relevance_score": 0.98}]},
            request=httpx.Request("POST", "https://example.invalid/v1/rerank"),
        )
        with patch("httpx.AsyncClient.post", return_value=response) as send:
            result = await service.search("auth", self.request)
        self.assertEqual(send.call_args.kwargs["json"]["documents"], [hit()["text"]])
        self.assertEqual(result["hits"][0]["rerank_score"], 0.98)
        self.assertFalse(result["degraded"])


if __name__ == "__main__":
    unittest.main()
