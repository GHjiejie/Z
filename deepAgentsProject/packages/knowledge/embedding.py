from __future__ import annotations

import hashlib
import json
import math
import os
import re
from typing import List
from time import monotonic

import httpx

from packages.knowledge.errors import KnowledgeStorageError
from packages.secrets import read_secret
from packages.http_security import provider_client, validate_provider_url
from packages.knowledge.ports import EmbeddingResult


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

    def identity(self):
        return {"provider": "test_double", "route": "local-hash", "model": self.model_revision}

    def embed_with_usage(self, texts: List[str]) -> EmbeddingResult:
        from packages.operations.telemetry import operation
        with operation('knowledge.embed'):
            return self._embed_with_usage(texts)

    def _embed_with_usage(self, texts: List[str]) -> EmbeddingResult:
        return EmbeddingResult(self.embed_documents(texts), sum(len(lexical_tokens(text)) for text in texts))

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
    def __init__(self, base_url: str, api_key: str, model: str, dimensions: int, *, transport=None):
        self.base_url = validate_provider_url(
            base_url, allowlist_variable="KNOWLEDGE_EMBEDDING_ALLOWED_ORIGINS"
        )
        self.api_key = api_key
        self.model_revision = model
        self.dimensions = dimensions
        self.transport = transport

    def identity(self):
        return {"provider": "embeddings", "route": self.base_url, "model": self.model_revision}

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return self.embed_with_usage(texts).vectors

    def embed_with_usage(self, texts: List[str]) -> EmbeddingResult:
        from packages.operations.telemetry import operation
        with operation('knowledge.embed'):
            return self._embed_with_usage(texts)

    def _embed_with_usage(self, texts: List[str]) -> EmbeddingResult:
        deadline = monotonic() + 60
        try:
            with provider_client(self.base_url, transport=self.transport, timeout=httpx.Timeout(15,connect=10)) as client:
                with client.stream(
                    "POST", f"{self.base_url}/embeddings",
                    json={"model": self.model_revision, "input": texts, "encoding_format": "float"},
                    headers={"Authorization": f"Bearer {self.api_key}"},
                ) as response:
                    response.raise_for_status()
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        if monotonic() > deadline:
                            raise KnowledgeStorageError("Embedding response exceeded its wall-clock deadline")
                        content.extend(chunk)
                        if len(content) > 16 * 1024 * 1024:
                            raise KnowledgeStorageError("Embedding response exceeds the size limit")
                    body = json.loads(content)
                    receipt = response.headers.get("x-request-id", "")[:512] or None
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise KnowledgeStorageError(
                f"Embedding provider request failed ({exc.__class__.__name__})"
            ) from exc
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise KnowledgeStorageError("Embedding provider returned an invalid response")
        if body.get("model") != self.model_revision:
            raise KnowledgeStorageError("Embedding provider returned a different model than the pinned revision")
        data = body["data"]
        if (any(not isinstance(item, dict) or type(item.get("index")) is not int for item in data)
                or sorted(item["index"] for item in data) != list(range(len(texts)))):
            raise KnowledgeStorageError("Embedding provider returned invalid vector indices")
        vectors = [item.get("embedding") for item in sorted(data, key=lambda item: item["index"])]
        if any(not isinstance(vector, list) or len(vector) != self.dimensions
               or any(type(value) not in {int, float} or not math.isfinite(value) for value in vector)
               for vector in vectors):
            raise KnowledgeStorageError("Embedding provider returned an invalid vector shape")
        usage = body.get("usage") or {}
        tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        total = usage.get("total_tokens") if isinstance(usage, dict) else None
        if type(tokens) is not int or not 0 <= tokens <= 10**9 or type(total) is not int or total != tokens:
            tokens = None
        return EmbeddingResult(vectors, tokens, receipt)

    def embed_query(self, text: str) -> List[float]:
        return self.embed_documents([text])[0]


def create_embedding_provider():
    provider = os.getenv("KNOWLEDGE_EMBEDDING_PROVIDER", "hash").lower()
    if provider == "hash":
        return HashEmbeddingProvider()
    if provider == "openai_compatible":
        api_key = read_secret("KNOWLEDGE_EMBEDDING_API_KEY")
        base_url = os.getenv("KNOWLEDGE_EMBEDDING_BASE_URL", "")
        model = os.getenv("KNOWLEDGE_EMBEDDING_MODEL", "")
        dimensions = int(os.getenv("KNOWLEDGE_EMBEDDING_DIMENSIONS", "0"))
        if not all((api_key, base_url, model, dimensions)):
            raise ValueError("OpenAI-compatible embedding provider configuration is incomplete")
        return OpenAICompatibleEmbeddingProvider(base_url, api_key, model, dimensions)
    raise ValueError(f"Unsupported KNOWLEDGE_EMBEDDING_PROVIDER: {provider}")
