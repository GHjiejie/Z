"""Check document fences, local links and pinned GitHub source line references."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
REPOSITORIES = {
    "openai/codex": ROOT / ".cache/codex",
    "google-gemini/gemini-cli": ROOT / ".cache/gemini-cli",
    "anthropics/claude-code": ROOT / ".cache/claude-code",
    "anthropics/claude-agent-sdk-python": ROOT / ".cache/claude-agent-sdk-python",
}


def main() -> None:
    errors: list[str] = []
    links = 0
    refs = set()
    pages = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    for page in pages:
        content = page.read_text()
        fence = None
        for line in content.splitlines():
            match = re.match(r"^(`{3,}|~{3,})", line)
            if match:
                marker = match.group(1)
                if fence is None:
                    fence = marker
                elif marker[0] == fence[0] and len(marker) >= len(fence):
                    fence = None
        if fence:
            errors.append(f"Unclosed Markdown fence: {page.name}")
        for href in re.findall(r"\]\(([^\s)]+)(?:\s+[^)]*)?\)", content):
            if href.startswith(("https:", "http:", "#", "mailto:", "data:")):
                continue
            target = unquote(href.strip("<>").split("#")[0])
            resolved = (page.parent / target).resolve()
            generated_validation = ROOT / "sources" / "validation.json"
            if resolved != generated_validation and not resolved.exists():
                errors.append(f"Broken local link in {page.name}: {href}")
            links += 1
        refs.update(
            re.findall(
                r"https://github\.com/([^/]+/[^/]+)/blob/([a-f0-9]{40})/([^\s)#]+)(?:#L(\d+)(?:-L(\d+))?)?",
                content,
            )
        )
    for repo, sha, path, first, last in sorted(refs):
        checkout = REPOSITORIES.get(repo)
        if checkout is None or not checkout.exists():
            errors.append(f"No verification checkout for {repo}")
            continue
        source = subprocess.run(
            ["git", "-C", str(checkout), "show", f"{sha}:{unquote(path)}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if source.returncode:
            errors.append(f"Missing source: {repo}@{sha}:{path}")
            continue
        count = len(source.stdout.splitlines())
        if first and not 1 <= int(first) <= int(last or first) <= count:
            errors.append(f"Invalid line range: {path} L{first}-L{last}, total {count}")
    result = {
        "markdown_pages": len(pages),
        "local_links_checked": links,
        "unique_pinned_source_references_checked": len(refs),
        "svg_count": len(list((ROOT / "diagrams").glob("*.svg"))),
        "errors": errors,
        "scope": "Static integrity only; source references are checked for existence and range, not semantic correctness.",
    }
    (ROOT / "sources" / "validation.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
