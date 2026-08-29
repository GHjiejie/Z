from __future__ import annotations

from pathlib import PurePosixPath


ALLOWED_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".pdf",
    ".docx",
    ".html",
    ".htm",
    ".json",
    ".csv",
}


def extension_for(filename: str) -> str:
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    return suffix if suffix in ALLOWED_EXTENSIONS else ".bin"


def build_object_key(
    environment_id: str,
    tenant_id: str,
    project_id: str,
    knowledge_base_id: str,
    document_version_id: str,
    filename: str,
) -> str:
    segments = [
        "rag",
        _safe_segment(environment_id),
        _safe_segment(tenant_id),
        _safe_segment(project_id),
        _safe_segment(knowledge_base_id),
        "documents",
        _safe_segment(document_version_id),
        f"source{extension_for(filename)}",
    ]
    return "/".join(segments)


def _safe_segment(value: str) -> str:
    normalized = "".join(character for character in value if character.isalnum() or character in "-_")
    if not normalized:
        raise ValueError("Object key segment is empty after normalization")
    return normalized[:128]
