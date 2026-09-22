"""Versioned, deterministic terms shared by ingestion and PostgreSQL FTS queries."""

import re
import unicodedata

TOKENIZER_VERSION = "mixed-bigram-v1"
_PARTS = re.compile(r"[\u3400-\u9fff]+|[a-z0-9]+(?:[._:/-][a-z0-9]+)*")


def tokenize(text: str) -> list[str]:
    """Keep technical identifiers and Chinese bigrams; not a semantic tokenizer."""
    terms: list[str] = []
    for part in _PARTS.findall(unicodedata.normalize("NFKC", text).lower()):
        if "\u3400" <= part[0] <= "\u9fff":
            terms.extend(part[i : i + 2] for i in range(max(1, len(part) - 1)))
        else:
            terms.append(part)
            terms.extend(
                re.findall(r"[a-z0-9]+", part) if re.search(r"[._:/-]", part) else []
            )
    return terms


def lexical_text(text: str) -> str:
    return " ".join(tokenize(text))
