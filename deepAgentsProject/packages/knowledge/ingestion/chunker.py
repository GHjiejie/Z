from __future__ import annotations

import hashlib
import re
from typing import Iterable, List

from packages.knowledge.models import ChunkRecord, ParsedBlock


class StructureAwareChunker:
    version = "structure-chunker-1.0"

    def __init__(self, max_characters: int = 2200, overlap_characters: int = 220):
        self.max_characters = max_characters
        self.overlap_characters = overlap_characters

    def chunk(self, blocks: Iterable[ParsedBlock]) -> List[ChunkRecord]:
        chunks: List[ChunkRecord] = []
        for block in blocks:
            for text in self._split(block.text):
                normalized = re.sub(r"\s+", " ", text).strip()
                if not normalized:
                    continue
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

    def _split(self, text: str) -> List[str]:
        normalized = text.strip()
        if len(normalized) <= self.max_characters:
            return [normalized]
        sentences = re.split(r"(?<=[。！？.!?；;])\s*|\n+", normalized)
        result: List[str] = []
        buffer = ""
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) > self.max_characters:
                if buffer:
                    result.append(buffer)
                    buffer = ""
                step = self.max_characters - self.overlap_characters
                result.extend(
                    sentence[start : start + self.max_characters]
                    for start in range(0, len(sentence), step)
                )
                continue
            candidate = f"{buffer} {sentence}".strip()
            if len(candidate) <= self.max_characters:
                buffer = candidate
            else:
                result.append(buffer)
                overlap = buffer[-self.overlap_characters :]
                buffer = f"{overlap} {sentence}".strip()
        if buffer:
            result.append(buffer)
        return result
