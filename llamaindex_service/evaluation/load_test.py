"""Bounded concurrent retrieval probe, reporting observed latency and failures."""

import argparse
import asyncio
import os
import time

import httpx

from .common import percentile, response_json, write_report


async def load_test(
    client: httpx.AsyncClient,
    kb_ids: list[str],
    query: str,
    requests: int,
    concurrency: int,
    top_k: int,
) -> dict:
    if not 1 <= concurrency <= 100 or not 1 <= requests <= 10000:
        raise ValueError("Use 1-100 concurrent requests and 1-10000 total requests")
    semaphore = asyncio.Semaphore(concurrency)
    started = time.monotonic()
    outcomes = []

    async def once(index: int):
        async with semaphore:
            request_started = time.monotonic()
            try:
                response = await client.post(
                    "/api/v1/retrieval/search",
                    json={
                        "knowledge_base_ids": kb_ids,
                        "query": query,
                        "top_k": top_k,
                    },
                )
                if response.is_error:
                    outcomes.append(
                        {
                            "index": index,
                            "ok": False,
                            "http_status": response.status_code,
                        }
                    )
                    return
                result = response_json(response)
                outcomes.append(
                    {
                        "index": index,
                        "ok": True,
                        "latency_ms": (time.monotonic() - request_started) * 1000,
                        "model_mode": result.get("model_mode", "unknown"),
                    }
                )
            except (httpx.HTTPError, ValueError, TypeError, RuntimeError):
                outcomes.append({"index": index, "ok": False, "http_status": None})

    await asyncio.gather(*(once(index) for index in range(requests)))
    duration = time.monotonic() - started
    latencies = [item["latency_ms"] for item in outcomes if item["ok"]]
    errors = [item for item in outcomes if not item["ok"]]
    return {
        "probe_type": "retrieval_load",
        "quality_claim": "Observed probe only; no million-chunk capacity or production SLO claim.",
        "requests": requests,
        "concurrency": concurrency,
        "successes": len(latencies),
        "errors": len(errors),
        "duration_seconds": round(duration, 3),
        "successful_requests_per_second": round(len(latencies) / duration, 3),
        "latency_ms": {
            "p50": percentile(latencies, 0.5),
            "p95": percentile(latencies, 0.95),
            "max": round(max(latencies), 3) if latencies else None,
        },
        "model_modes": sorted({item["model_mode"] for item in outcomes if item["ok"]}),
        "timing_scope": "HTTP retrieval including embedding and optional reranker; excludes local queue waiting",
        "failures": errors,
    }


async def main(args):
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        timeout=args.timeout,
        limits=httpx.Limits(max_connections=args.concurrency),
    ) as client:
        report = await load_test(
            client,
            args.knowledge_base_id,
            args.query,
            args.requests,
            args.concurrency,
            args.top_k,
        )
    write_report(report, args.output)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--knowledge-base-id", action="append", required=True)
    parser.add_argument("--query", default="报销审批需要什么材料？")
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--top-k", type=int, choices=range(1, 21), default=3)
    parser.add_argument("--token", default=os.getenv("RAG_AUTH_TOKEN", ""))
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--output", default="-")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
