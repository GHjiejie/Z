"""Evaluation scoring and HTTP orchestration tests with synthetic transports."""

import asyncio
import json
import unittest

import httpx

from .common import FIXTURES, SAMPLE_DATASET, document_ranking, load_dataset, percentile
from .evaluate import evaluate, seed_fixtures
from .load_test import load_test


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    def test_sample_labels_are_complete_and_valid(self):
        records = load_dataset(SAMPLE_DATASET)
        self.assertEqual(len(records), 20)
        self.assertEqual(sum(record["answerable"] for record in records), 16)
        self.assertEqual(
            {source for row in records for source in row["expected_sources"]},
            {path.name for path in FIXTURES.glob("*.md")},
        )
        self.assertEqual(percentile([1, 2, 3, 4, 5], 0.95), 5)
        self.assertIsNone(percentile([], 0.95))

    def test_document_ranking_does_not_promote_chunks_beyond_k(self):
        hits = [{"title": "a.md"}, {"title": "a.md"}, {"title": "b.md"}]
        self.assertEqual(document_ranking(hits, 2), ["a.md"])

    async def test_failed_queries_count_against_recall_and_no_answer_errors_are_visible(
        self,
    ):
        records = [
            {
                "id": "one",
                "query": "partial",
                "answerable": True,
                "expected_sources": ["a.md", "b.md"],
            },
            {
                "id": "two",
                "query": "failure",
                "answerable": True,
                "expected_sources": ["a.md"],
            },
            {
                "id": "three",
                "query": "unknown",
                "answerable": False,
                "expected_sources": [],
            },
        ]

        def transport(request):
            body = json.loads(request.content)
            if body["query"] == "failure":
                return httpx.Response(503, json={"code": "EMBEDDING_UNAVAILABLE"})
            if request.url.path.endswith("/answers"):
                return httpx.Response(200, json={"status": "completed"})
            return httpx.Response(
                200,
                json={
                    "hits": [{"title": "irrelevant.md"}, {"title": "a.md"}],
                    "model_mode": "mock",
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport), base_url="http://test"
        ) as client:
            report = await evaluate(client, records, ["kb"], 3, True)
        self.assertEqual(report["recall_at_k"], 0.25)
        self.assertEqual(report["mrr"], 0.25)
        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["no_answer_accuracy"], 0)
        self.assertEqual(report["no_answer_cases_checked"], 1)
        self.assertEqual(report["model_modes"], ["mock"])

    async def test_seed_uploads_four_sources_and_waits_for_persisted_jobs(self):
        jobs = []

        def transport(request):
            if request.url.path.endswith("/knowledge-bases"):
                return httpx.Response(201, json={"id": "fixture-kb"})
            if request.url.path.endswith("/documents"):
                jobs.append(str(len(jobs) + 1))
                return httpx.Response(
                    202,
                    json={
                        "document_id": "d" + jobs[-1],
                        "version_id": "v" + jobs[-1],
                        "job_id": jobs[-1],
                    },
                )
            return httpx.Response(200, json={"status": "READY"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport), base_url="http://test"
        ) as client:
            kb, manifest = await seed_fixtures(client, FIXTURES, 2)
        self.assertEqual(kb, "fixture-kb")
        self.assertEqual(len(manifest), 4)
        self.assertTrue(all(len(item["sha256"]) == 64 for item in manifest))

    async def test_load_probe_respects_concurrency_and_counts_rate_limits(self):
        active, peak, calls = 0, 0, 0

        async def transport(request):
            nonlocal active, peak, calls
            active += 1
            peak = max(peak, active)
            calls += 1
            status = 429 if calls == 1 else 200
            await asyncio.sleep(0.001)
            active -= 1
            return httpx.Response(status, json={"hits": [], "model_mode": "mock"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport), base_url="http://test"
        ) as client:
            report = await load_test(client, ["kb"], "query", 8, 2, 3)
        self.assertEqual(peak, 2)
        self.assertEqual(report["successes"], 7)
        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["failures"][0]["http_status"], 429)
        self.assertGreater(report["latency_ms"]["p95"], 0)


if __name__ == "__main__":
    unittest.main()
