"""Shared evaluation inputs and metrics; reports never contain auth tokens."""

import json
import math
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).parent
SAMPLE_DATASET = HERE / "data" / "questions.jsonl"
FIXTURES = HERE / "fixtures"


def percentile(values: list[float], percent: float) -> float | None:
    """Nearest-rank percentile: a conservative small-sample latency statistic."""
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(percent * len(ordered)) - 1)], 3)


def load_dataset(path: Path) -> list[dict]:
    records, seen = [], set()
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        record = json.loads(line)
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("id"), str)
            or not record["id"]
        ):
            raise ValueError(f"Dataset line {line_number} needs a nonempty id")
        if record["id"] in seen:
            raise ValueError(f"Duplicate dataset id: {record['id']}")
        if not isinstance(record.get("query"), str) or not record["query"].strip():
            raise ValueError(f"Dataset line {line_number} needs query text")
        if not isinstance(record.get("answerable"), bool):
            raise TypeError(f"Dataset line {line_number} needs boolean answerable")
        labels = record.get("expected_sources")
        if not isinstance(labels, list) or not all(
            isinstance(item, str) and item for item in labels
        ):
            raise ValueError(
                f"Dataset line {line_number} needs expected_sources filenames"
            )
        if record["answerable"] != bool(labels):
            raise ValueError(
                f"Dataset line {line_number}: answerable must match source labels"
            )
        seen.add(record["id"])
        records.append(record)
    if not records:
        raise ValueError("Dataset is empty")
    return records


def document_ranking(hits: list[dict], top_k: int) -> list[str]:
    """Evaluate returned chunk top-k, deduplicating filenames without promoting lower hits."""
    return list(dict.fromkeys(str(hit.get("title", "")) for hit in hits[:top_k]))


def ranking_metrics(expected: list[str], ranking: list[str]) -> tuple[float, float]:
    labels = set(expected)
    if not labels:
        raise ValueError("Recall/MRR require at least one relevant source label")
    recall = len(labels & set(ranking)) / len(labels)
    reciprocal_rank = next(
        (
            1 / position
            for position, source in enumerate(ranking, 1)
            if source in labels
        ),
        0.0,
    )
    return recall, reciprocal_rank


def response_json(response: httpx.Response) -> dict:
    """Only public code/status in errors; never copy an upstream body or URL."""
    if response.is_error:
        code = "HTTP_ERROR"
        try:
            payload = response.json()
            if isinstance(payload, dict) and isinstance(payload.get("code"), str):
                code = payload["code"][:80]
        except ValueError:
            pass
        raise RuntimeError(f"HTTP {response.status_code}: {code}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise TypeError("Expected a JSON object response")
    return payload


def write_report(report: dict[str, Any], output: str) -> None:
    content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output == "-":
        print(content, end="")
    else:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"Report saved: {path.resolve()}")
