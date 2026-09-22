"""LlamaIndex embedding adapters. Chat credentials are never loaded here."""

import hashlib
import math
from typing import Any

import httpx
from llama_index.core.embeddings import BaseEmbedding
from pydantic import Field, SecretStr

from .lexical import tokenize


class EmbeddingError(RuntimeError):
    """An upstream failure or incompatible vector; never a no-evidence result."""


class DeterministicEmbedding(BaseEmbedding):
    """Offline hash embedding for development fixtures, not semantic evaluation."""

    dimension: int = Field(gt=0)

    def _get_text_embedding(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in tokenize(text):
            digest = hashlib.sha256(token.encode()).digest()
            vector[int.from_bytes(digest[:4], "big") % self.dimension] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if not norm:
            vector[0] = 1.0
            norm = 1.0
        return [value / norm for value in vector]

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._get_text_embedding(query)

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._get_query_embedding(query)

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return self._get_text_embedding(text)


class GatewayEmbedding(BaseEmbedding):
    """OpenAI-compatible embedding endpoint with strict count/index/dimension checks."""

    dimension: int = Field(gt=0)
    base_url: str
    api_key: SecretStr = Field(default=SecretStr(""), exclude=True)
    timeout_seconds: float = 60.0

    def _headers(self) -> dict[str, str]:
        key = self.api_key.get_secret_value()
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _body(self, texts: list[str]) -> dict[str, Any]:
        # Validate returned dimensions instead of requiring a provider-specific
        # dimensions parameter unsupported by many self-hosted embedding models.
        return {"model": self.model_name, "input": texts, "encoding_format": "float"}

    def _decode(self, response: httpx.Response, count: int) -> list[list[float]]:
        try:
            response.raise_for_status()
            data = response.json()["data"]
            if not isinstance(data, list) or len(data) != count:
                raise ValueError("Wrong embedding count")
            if sorted(item["index"] for item in data) != list(range(count)):
                raise ValueError("Wrong embedding indices")
            vectors = [
                item["embedding"]
                for item in sorted(data, key=lambda item: item["index"])
            ]
            for vector in vectors:
                if len(vector) != self.dimension or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    for value in vector
                ):
                    raise ValueError("Invalid vector dimension or values")
                if not any(vector):
                    raise ValueError("Zero vector is not searchable")
            return vectors
        except (ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
            raise EmbeddingError(
                "Embedding endpoint returned an invalid response"
            ) from exc

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(
                    f"{self.base_url.rstrip('/')}/embeddings",
                    headers=self._headers(),
                    json=self._body(texts),
                )
            return self._decode(response, len(texts))
        except httpx.HTTPError as exc:
            raise EmbeddingError("Embedding endpoint is unavailable") from exc

    async def _aget_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.base_url.rstrip('/')}/embeddings",
                    headers=self._headers(),
                    json=self._body(texts),
                )
            return self._decode(response, len(texts))
        except httpx.HTTPError as exc:
            raise EmbeddingError("Embedding endpoint is unavailable") from exc

    def _get_text_embedding(self, text: str) -> list[float]:
        return self._get_text_embeddings([text])[0]

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._get_text_embedding(query)

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return (await self._aget_text_embeddings([text]))[0]

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return await self._aget_text_embedding(query)


def create_embedding(settings: Any) -> BaseEmbedding:
    """Build an explicit request/worker-scoped embedding without global Settings."""
    common = {
        "model_name": settings.embedding_model,
        "dimension": settings.embedding_dimension,
        "embed_batch_size": settings.embedding_batch_size,
    }
    if settings.model_mode == "mock":
        if getattr(settings, "environment", "development") not in {
            "development",
            "dev",
            "test",
        }:
            raise ValueError("Mock embeddings are only allowed in development/test")
        return DeterministicEmbedding(**common)
    if settings.model_mode != "live" or not settings.embedding_base_url:
        raise ValueError("Live embeddings require an explicit endpoint")
    return GatewayEmbedding(
        **common,
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key or "",
    )
