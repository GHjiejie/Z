"""Bounded parser policy, shared by the supervisor and standalone child."""
from dataclasses import dataclass, fields
import math
import os


@dataclass(frozen=True)
class ParseLimits:
    timeout_seconds: float = 30.0
    cpu_seconds: int = 15
    memory_bytes: int = 512 * 1024 * 1024
    max_input_bytes: int = 100 * 1024 * 1024
    max_output_bytes: int = 16 * 1024 * 1024
    max_expanded_bytes: int = 64 * 1024 * 1024
    max_archive_entries: int = 2048
    max_compression_ratio: int = 200
    max_pages: int = 500
    max_blocks: int = 20_000
    max_text_characters: int = 2_000_000
    max_chunks: int = 5000
    chunk_characters: int = 2200
    overlap_characters: int = 220
    max_concurrent: int = 2

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("Parser limits must be finite numbers")
            if item.name != "timeout_seconds" and not isinstance(value, int):
                raise ValueError("Parser counts and sizes must be integers")
            if value < (0 if item.name == "overlap_characters" else 1):
                raise ValueError("Parser limits must be positive (overlap may be zero)")
        ceilings = {"timeout_seconds": 300, "cpu_seconds": 120, "memory_bytes": 4 * 1024**3,
            "max_input_bytes": 100 * 1024**2, "max_output_bytes": 32 * 1024**2,
            "max_expanded_bytes": 256 * 1024**2, "max_archive_entries": 10_000,
            "max_compression_ratio": 1000, "max_pages": 2000, "max_blocks": 50_000,
            "max_text_characters": 8_000_000, "max_chunks": 10_000, "chunk_characters": 10_000,
            "max_concurrent": 16}
        if any(getattr(self, name) > maximum for name, maximum in ceilings.items()):
            raise ValueError("Parser limit exceeds the supported hard ceiling")
        if self.overlap_characters >= self.chunk_characters:
            raise ValueError("Parser overlap must be smaller than chunk size")

    @classmethod
    def from_environment(cls):
        defaults = cls()
        return cls(**{item.name: type(getattr(defaults, item.name))(
            os.getenv("DEEPAGENT_PARSER_" + item.name.upper(), str(getattr(defaults, item.name))))
            for item in fields(cls)})
