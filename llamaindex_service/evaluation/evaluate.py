"""Evaluate labeled retrieval through the service API; optionally seed fixtures."""

import argparse
import asyncio
import hashlib
import os
import time
import uuid
from pathlib import Path

import httpx

from .common import (
    FIXTURES,
    SAMPLE_DATASET,
    document_ranking,
    load_dataset,
    percentile,
    ranking_metrics,
    response_json,
    write_report,
)


async def seed_fixtures(
    client: httpx.AsyncClient, fixtures: Path, wait_seconds: float
) -> tuple[str, list[dict]]:
    sources = sorted(fixtures.glob("*.md"))
    if not sources:
        raise ValueError("No Markdown fixtures were found")
    kb = response_json(
        await client.post(
            "/api/v1/knowledge-bases",
            json={
                "name": f"Synthetic RAG evaluation {uuid.uuid4().hex[:8]}",
                "description": "Synthetic fixtures, no production data; created by evaluation CLI.",
            },
        )
    )
    manifest = []
    for path in sources:
        data = path.read_bytes()
        uploaded = response_json(
            await client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/documents",
                files={"file": (path.name, data, "text/markdown")},
                headers={
                    "Idempotency-Key": f"eval-upload-{path.name}-{hashlib.sha256(data).hexdigest()}"
                },
            )
        )
        manifest.append(
            {
                "filename": path.name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "document_id": uploaded["document_id"],
                "version_id": uploaded["version_id"],
                "job_id": uploaded["job_id"],
            }
        )
    pending = {item["job_id"] for item in manifest}
    deadline = time.monotonic() + wait_seconds
    while pending:
        for job_id in list(pending):
            job = response_json(await client.get(f"/api/v1/jobs/{job_id}"))
            if job["status"] == "READY":
                pending.remove(job_id)
            elif job["status"] in {"FAILED", "CANCELLED"}:
                raise RuntimeError(
                    f"Fixture ingestion {job_id} stopped: {job.get('error_code') or job['status']}"
                )
        if pending:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Fixture ingestion timed out; run the Worker, then resume with --knowledge-base-id {kb['id']}"
                )
            await asyncio.sleep(min(0.5, max(0, deadline - time.monotonic())))
    return kb["id"], manifest


async def evaluate(
    client: httpx.AsyncClient,
    records: list[dict],
    kb_ids: list[str],
    top_k: int,
    check_no_answer: bool,
) -> dict:
    results, latencies, modes = [], [], set()
    for record in records:
        item = {
            "id": record["id"],
            "query": record["query"],
            "answerable": record["answerable"],
            "expected_sources": record["expected_sources"],
        }
        started = time.monotonic()
        try:
            result = response_json(
                await client.post(
                    "/api/v1/retrieval/search",
                    json={
                        "knowledge_base_ids": kb_ids,
                        "query": record["query"],
                        "top_k": top_k,
                    },
                )
            )
            latency = (time.monotonic() - started) * 1000
            latencies.append(latency)
            modes.add(result.get("model_mode", "unknown"))
            ranking = document_ranking(result["hits"], top_k)
            item.update(
                retrieved_sources=ranking,
                latency_ms=round(latency, 3),
                status="ok",
                index_generation_ids=result.get("index_generation_ids", []),
            )
            if record["answerable"]:
                item["recall"], item["reciprocal_rank"] = ranking_metrics(
                    record["expected_sources"], ranking
                )
            if check_no_answer and not record["answerable"]:
                answer = response_json(
                    await client.post(
                        "/api/v1/answers",
                        json={
                            "knowledge_base_ids": kb_ids,
                            "query": record["query"],
                            "stream": False,
                        },
                        headers={
                            "Idempotency-Key": f"eval-no-answer-{uuid.uuid4().hex}"
                        },
                    )
                )
                item["no_answer_correct"] = answer.get("status") == "no_answer"
        except (httpx.HTTPError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            # No exception string is retained: SDK URLs may contain credentials.
            item.update(status="error", error_type=type(exc).__name__)
            if record["answerable"]:
                item.update(recall=0.0, reciprocal_rank=0.0)
            elif check_no_answer:
                item["no_answer_correct"] = False
        results.append(item)
    labeled = [item for item in results if item["answerable"]]
    unanswerable = [item for item in results if "no_answer_correct" in item]
    return {
        "evaluation_type": "synthetic_smoke"
        if all(item.get("split") == "synthetic_smoke" for item in records)
        else "user_labeled_retrieval",
        "quality_claim": "No semantic answer-support or production acceptance claim. Mock models only verify plumbing.",
        "model_modes": sorted(modes),
        "knowledge_base_ids": kb_ids,
        "top_k": top_k,
        "cases": len(results),
        "errors": sum(item["status"] == "error" for item in results),
        "recall_at_k": round(sum(item["recall"] for item in labeled) / len(labeled), 4)
        if labeled
        else None,
        "mrr": round(sum(item["reciprocal_rank"] for item in labeled) / len(labeled), 4)
        if labeled
        else None,
        "no_answer_accuracy": round(
            sum(item["no_answer_correct"] for item in unanswerable) / len(unanswerable),
            4,
        )
        if unanswerable
        else None,
        "no_answer_cases_checked": len(unanswerable),
        "search_latency_ms": {
            "p50": percentile(latencies, 0.5),
            "p95": percentile(latencies, 0.95),
            "successful_requests": len(latencies),
        },
        "ranking_unit": "unique source filenames within returned chunk top-k",
        "results": results,
    }


async def main(args: argparse.Namespace) -> int:
    records = load_dataset(args.dataset)
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"), headers=headers, timeout=args.timeout
    ) as client:
        manifest = []
        kb_ids = args.knowledge_base_id or []
        if args.seed:
            kb_id, manifest = await seed_fixtures(
                client, args.fixtures_directory, args.wait_seconds
            )
            kb_ids = [kb_id]
        report = await evaluate(
            client, records, kb_ids, args.top_k, args.check_no_answer
        )
    report["dataset_sha256"] = hashlib.sha256(args.dataset.read_bytes()).hexdigest()
    report["source_manifest"] = manifest
    write_report(report, args.output)
    return 1 if report["errors"] else 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--base-url", default="http://127.0.0.1:8000")
    target = result.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--seed",
        action="store_true",
        help="Create a KB, upload fixtures and wait for an independently running Worker",
    )
    target.add_argument(
        "--knowledge-base-id", action="append", help="Existing KB; may be repeated"
    )
    result.add_argument("--dataset", type=Path, default=SAMPLE_DATASET)
    result.add_argument("--fixtures-directory", type=Path, default=FIXTURES)
    result.add_argument("--top-k", type=int, choices=range(1, 21), default=3)
    result.add_argument(
        "--check-no-answer",
        action="store_true",
        help="Also call /answers for unanswerable labels; live models may incur cost",
    )
    result.add_argument(
        "--token",
        default=os.getenv("RAG_AUTH_TOKEN", ""),
        help="Prefer RAG_AUTH_TOKEN environment variable",
    )
    result.add_argument("--timeout", type=float, default=120)
    result.add_argument("--wait-seconds", type=float, default=180)
    result.add_argument("--output", default="-", help="JSON path, or - for stdout")
    return result


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parser().parse_args())))
