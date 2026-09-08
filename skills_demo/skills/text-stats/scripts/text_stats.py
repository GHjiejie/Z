"""Read UTF-8 text from stdin and print deterministic statistics as JSON."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter


def analyze(text: str) -> dict[str, object]:
    words = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", text.lower())
    frequencies = Counter(words)
    return {
        "characters": len(text),
        "characters_without_whitespace": len(re.sub(r"\s", "", text)),
        "words": len(words),
        "lines": len(text.splitlines()) if text else 0,
        "top_words": [
            {"word": word, "count": count}
            for word, count in sorted(
                frequencies.items(), key=lambda item: (-item[1], item[0])
            )[:5]
        ],
    }


def main() -> None:
    text = sys.stdin.read()
    print(json.dumps(analyze(text), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
