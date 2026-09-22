"""LlamaIndex document transformations with stable, version-scoped chunk IDs."""

import hashlib
import json
import math
import uuid
from collections.abc import Callable
from threading import Event
from typing import Any

from llama_index.core import Document
from llama_index.core.ingestion import IngestionPipeline
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import MetadataMode

from llamaindex_service.contracts import ServiceError
from llamaindex_service.retrieval.embeddings import create_embedding
from llamaindex_service.retrieval.lexical import lexical_text
from llamaindex_service.storage import Storage

from .readers import PARSER_VERSION, ParseResult, parse_document

CHUNKER_VERSION = "sentence-structured-v1"
PhaseCallback = Callable[[str, int], None]


def check_cancelled(cancelled: Event | None) -> None:
    if cancelled is not None and cancelled.is_set():
        raise ServiceError(
            "JOB_CANCELLED", "The job was cancelled or its lease expired.", 409
        )


class IngestionProcessor:
    def __init__(self, settings: Any, storage: Storage, embedding: Any = None):
        self.settings = settings
        self.storage = storage
        self.embedding = (
            embedding if embedding is not None else create_embedding(settings)
        )

    def process(
        self,
        job: dict[str, Any],
        on_phase: PhaseCallback | None = None,
        cancelled: Event | None = None,
    ) -> list[dict[str, Any]]:
        def phase(name: str, completed: int = 0) -> None:
            check_cancelled(cancelled)
            if on_phase:
                on_phase(name, completed)

        self._check_model(job)
        phase("VALIDATING")
        with self.storage.materialize(job["object_key"]) as path:
            phase("PARSING")
            parsed = parse_document(
                path,
                job["filename"],
                self.settings,
                expected_sha256=job["sha256"],
                cancelled=cancelled,
            )
        phase("CHUNKING", len(parsed.blocks))
        nodes = self._nodes(parsed, job)
        chunks: list[dict[str, Any]] = []
        batch_size = self.settings.embedding_batch_size
        phase("EMBEDDING")
        for offset in range(0, len(nodes), batch_size):
            check_cancelled(cancelled)
            batch = nodes[offset : offset + batch_size]
            texts = [
                node.get_content(metadata_mode=MetadataMode.NONE) for node in batch
            ]
            vectors = self.embedding.get_text_embedding_batch(texts)
            if len(vectors) != len(batch):
                raise ServiceError(
                    "INVALID_EMBEDDING",
                    "The embedding provider returned an incomplete batch.",
                    502,
                )
            for node, text, vector in zip(batch, texts, vectors, strict=True):
                if len(vector) != self.settings.embedding_dimension or not all(
                    math.isfinite(number) for number in vector
                ):
                    raise ServiceError(
                        "INVALID_EMBEDDING",
                        "The embedding provider returned incompatible vectors.",
                        502,
                    )
                chunks.append(
                    {
                        "id": node.node_id,
                        "text": text,
                        "embedding": [float(number) for number in vector],
                        "metadata": node.metadata,
                        "lexical_text": lexical_text(text),
                    }
                )
            phase("EMBEDDING", len(chunks))
        phase("INDEXING", len(chunks))
        return chunks

    def _check_model(self, job: dict[str, Any]) -> None:
        for name in ("embedding_model", "embedding_revision", "embedding_dimension"):
            if name in job and job[name] != getattr(self.settings, name):
                raise ServiceError(
                    "INDEX_MODEL_MISMATCH",
                    "The worker model does not match this index generation.",
                    409,
                )

    def _nodes(self, parsed: ParseResult, job: dict[str, Any]) -> list[Any]:
        config = {
            "parser": PARSER_VERSION,
            "chunker": CHUNKER_VERSION,
            "chunk_size": self.settings.chunk_size,
            "chunk_overlap": self.settings.chunk_overlap,
            "embedding_model": self.settings.embedding_model,
            "embedding_revision": self.settings.embedding_revision,
            "embedding_dimension": self.settings.embedding_dimension,
        }
        fingerprint = hashlib.sha256(
            json.dumps(config, sort_keys=True).encode()
        ).hexdigest()
        scope = ":".join(
            str(job.get(key, ""))
            for key in (
                "tenant_id",
                "project_id",
                "kb_id",
                "document_id",
                "version_id",
                "generation_id",
            )
        )
        documents = []
        for index, block in enumerate(parsed.blocks):
            metadata = {
                **block.metadata,
                "source_index": index,
                "title": job["filename"],
                "parser_version": PARSER_VERSION,
                "processing_fingerprint": fingerprint,
                "sha256": job["sha256"],
            }
            if parsed.warnings:
                metadata["parse_warnings"] = parsed.warnings
            documents.append(
                Document(
                    text=block.text,
                    id_=str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL, f"{scope}:{fingerprint}:block:{index}"
                        )
                    ),
                    metadata=metadata,
                    excluded_embed_metadata_keys=list(metadata),
                    excluded_llm_metadata_keys=list(metadata),
                )
            )
        splitter = SentenceSplitter(
            chunk_size=self.settings.chunk_size,
            chunk_overlap=self.settings.chunk_overlap,
            include_prev_next_rel=False,
            paragraph_separator="\n\n",
            # Preserve Chinese sentence punctuation before falling back to words.
            secondary_chunking_regex=r"[^,.;。？！!?]+[,.;。？！!?]?|[,.;。？！!?]",
        )
        pipeline = IngestionPipeline(transformations=[splitter], disable_cache=True)
        nodes = list(pipeline.run(documents=documents, show_progress=False))
        for index, node in enumerate(nodes):
            text = node.get_content(metadata_mode=MetadataMode.NONE)
            if node.metadata.get("table_headers"):
                header = " | ".join(node.metadata["table_headers"])
                if not text.startswith(header):
                    node.text = header + "\n" + text
                    text = node.text
            digest = hashlib.sha256(text.encode()).hexdigest()
            node.id_ = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL, f"{scope}:{fingerprint}:{index}:{digest}"
                )
            )
            node.metadata.update(
                {
                    "chunk_index": index,
                    "start_char": node.start_char_idx,
                    "end_char": node.end_char_idx,
                    "text_sha256": digest,
                }
            )
        if not nodes:
            raise ServiceError("NO_TEXT", "The document produced no searchable text.")
        return nodes
