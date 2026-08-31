from __future__ import annotations

import hashlib
import re
from typing import Iterable, List

from packages.knowledge.models import ChunkRecord, ParsedBlock
from packages.knowledge.errors import ParseLimitExceeded
from .limits import ParseLimits


class StructureAwareChunker:
    version = "structure-chunker-2.0"

    def __init__(self, max_characters: int = 2200, overlap_characters: int = 220, *, limits: ParseLimits | None = None):
        self.limits = limits or ParseLimits(chunk_characters=max_characters, overlap_characters=overlap_characters)
        if (max_characters != self.limits.chunk_characters or overlap_characters != self.limits.overlap_characters):
            raise ValueError("Chunk settings must match parser resource policy")
        self.max_characters = max_characters
        self.overlap_characters = overlap_characters

    def chunk(self, blocks: Iterable[ParsedBlock]) -> List[ChunkRecord]:
        chunks: List[ChunkRecord] = []
        total = 0
        for index, block in enumerate(blocks):
            total += len(block.text)
            if index >= self.limits.max_blocks or total > self.limits.max_text_characters:
                raise ParseLimitExceeded("Document exceeds chunk input limits")
            for text in self._split(block.text):
                normalized = re.sub(r"\s+", " ", text).strip()
                if not normalized:
                    continue
                if len(chunks) >= self.limits.max_chunks:
                    raise ParseLimitExceeded("Document exceeds chunk count limit")
                chunks.append(
                    ChunkRecord(
                        position=len(chunks),
                        text=normalized,
                        token_count=max(1, len(normalized) // 4),
                        content_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                        locator=dict(block.locator),
                    )
                )
        return chunks

    def _split(self, text: str) -> Iterable[str]:
        normalized = text.strip()
        if len(normalized) <= self.max_characters:
            yield normalized
            return
        sentences = re.split(r"(?<=[。！？.!?；;])\s*|\n+", normalized)
        buffer = ""
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) > self.max_characters:
                if buffer:
                    yield buffer
                    buffer = ""
                step = self.max_characters - self.overlap_characters
                for start in range(0, len(sentence), step):
                    yield sentence[start : start + self.max_characters]
                continue
            candidate = f"{buffer} {sentence}".strip()
            if len(candidate) <= self.max_characters:
                buffer = candidate
            else:
                yield buffer
                overlap = buffer[-self.overlap_characters :] if self.overlap_characters else ""
                buffer = f"{overlap} {sentence}".strip()
                while len(buffer) > self.max_characters:
                    yield buffer[:self.max_characters]
                    buffer = buffer[self.max_characters - self.overlap_characters:]
        if buffer:
            yield buffer
