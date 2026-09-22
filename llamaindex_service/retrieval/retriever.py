"""Custom LlamaIndex retriever delegating both recall branches to authorized SQL."""

import asyncio
from typing import Any

from llama_index.core.retrievers import BaseRetriever
from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode

from .lexical import lexical_text


def reciprocal_rank_fusion(
    branches: dict[str, list[dict]], limit: int, constant: int = 60
) -> list[dict]:
    """Fuse ranks, not incomparable score scales; duplicate rows get one vote."""
    rows: dict[str, dict] = {}
    for branch, candidates in branches.items():
        seen: set[str] = set()
        for rank, candidate in enumerate(candidates, 1):
            key = str(candidate["chunk_id"])
            if key in seen:
                continue
            seen.add(key)
            row = rows.setdefault(key, {**candidate, "score": 0.0, "scores": {}})
            row["score"] += 1.0 / (constant + rank)
            row["scores"][branch] = float(candidate.get("score", 0.0))
    return sorted(rows.values(), key=lambda row: (-row["score"], row["chunk_id"]))[
        :limit
    ]


class AuthorizedHybridRetriever(BaseRetriever):
    def __init__(
        self, repository: Any, ctx: Any, embedding: Any, request: dict, settings: Any
    ):
        super().__init__()
        self.repository, self.ctx, self.embedding = repository, ctx, embedding
        self.request, self.settings = request, settings

    def _candidates(self, query: str, vector: list[float]) -> dict:
        return self.repository.search_candidates(
            self.ctx,
            self.request["knowledge_base_ids"],
            self.request.get("document_ids"),
            vector,
            lexical_text(query),
            top_k=max(self.settings.dense_top_k, self.settings.sparse_top_k),
            filters=self.request.get("filters"),
        )

    def _nodes(self, branches: dict) -> list[NodeWithScore]:
        branches = {
            "vector": branches.get("vector", [])[: self.settings.dense_top_k],
            "lexical": branches.get("lexical", [])[: self.settings.sparse_top_k],
        }
        rows = reciprocal_rank_fusion(branches, self.settings.rerank_top_k)
        return [
            NodeWithScore(
                node=TextNode(
                    id_=str(row["chunk_id"]),
                    text=row["text"],
                    metadata={
                        key: value for key, value in row.items() if key != "text"
                    },
                ),
                score=row["score"],
            )
            for row in rows
        ]

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        vector = self.embedding.get_query_embedding(query_bundle.query_str)
        return self._nodes(self._candidates(query_bundle.query_str, vector))

    async def _aretrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        vector = await self.embedding.aget_query_embedding(query_bundle.query_str)
        candidates = await asyncio.to_thread(
            self._candidates, query_bundle.query_str, vector
        )
        return self._nodes(candidates)
