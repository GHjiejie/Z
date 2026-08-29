from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.request
from typing import List

from packages.knowledge.errors import KnowledgeStorageError


def lexical_tokens(text: str) -> List[str]:
    normalized = text.lower()
    words = re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]", normalized)
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    bigrams = [cjk[index : index + 2] for index in range(max(0, len(cjk) - 1))]
    return words + bigrams


class HashEmbeddingProvider:
    """Deterministic local reference embedding used when no model gateway is configured."""

    model_revision = "deepagent-hash-embedding-1.0"
    dimensions = 256

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._embed(text)

    def _embed(self, text: str) -> List[float]:
        vector = [0.0] * self.dimensions
        for token in lexical_tokens(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            direction = 1.0 if digest[4] & 1 else -1.0
            vector[index] += direction
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector


class OpenAICompatibleEmbeddingProvider:
    def __init__(self, base_url: str, api_key: str, model: str, dimensions: int):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_revision = model
        self.dimensions = dimensions

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        request = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=json.dumps({"model": self.model_revision, "input": texts}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # nosec: configured endpoint
                body = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise KnowledgeStorageError(f"Embedding provider request failed: {exc}") from exc
        ordered = sorted(body.get("data", []), key=lambda item: item.get("index", 0))
        vectors = [item["embedding"] for item in ordered]
        if len(vectors) != len(texts) or any(len(vector) != self.dimensions for vector in vectors):
            raise KnowledgeStorageError("Embedding provider returned an invalid vector shape")
        return vectors

    def embed_query(self, text: str) -> List[float]:
        return self.embed_documents([text])[0]


def create_embedding_provider():
    provider = os.getenv("KNOWLEDGE_EMBEDDING_PROVIDER", "hash").lower()
    if provider == "hash":
        return HashEmbeddingProvider()
    if provider == "openai_compatible":
        api_key = os.getenv("KNOWLEDGE_EMBEDDING_API_KEY", "")
        base_url = os.getenv("KNOWLEDGE_EMBEDDING_BASE_URL", "")
        model = os.getenv("KNOWLEDGE_EMBEDDING_MODEL", "")
        dimensions = int(os.getenv("KNOWLEDGE_EMBEDDING_DIMENSIONS", "0"))
        if not all((api_key, base_url, model, dimensions)):
            raise ValueError("OpenAI-compatible embedding provider configuration is incomplete")
        return OpenAICompatibleEmbeddingProvider(base_url, api_key, model, dimensions)
    raise ValueError(f"Unsupported KNOWLEDGE_EMBEDDING_PROVIDER: {provider}")
