from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=1000)


class UploadPrepare(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=160)
    size_bytes: int = Field(gt=0, le=100 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    description: str = Field(default="", max_length=1000)
    visibility: Literal["private", "project"] = "project"
    allowed_roles: List[str] = Field(default_factory=list)

    @field_validator("filename")
    @classmethod
    def filename_is_safe(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or normalized in {".", ".."}:
            raise ValueError("filename is invalid")
        return normalized


class UploadComplete(BaseModel):
    etag: Optional[str] = None
    object_version_id: Optional[str] = None


class KnowledgeSearchFilters(BaseModel):
    document_ids: List[str] = Field(default_factory=list, max_length=100)
    content_types: List[str] = Field(default_factory=list, max_length=20)


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    knowledge_base_id: Optional[str] = None
    revision_ids: List[str] = Field(default_factory=list, max_length=20)
    filters: KnowledgeSearchFilters = Field(default_factory=KnowledgeSearchFilters)
    top_k: int = Field(default=8, ge=1, le=20)


class ParsedBlock(BaseModel):
    text: str
    locator: Dict[str, Any] = Field(default_factory=dict)


class ChunkRecord(BaseModel):
    position: int
    text: str
    token_count: int
    content_hash: str
    locator: Dict[str, Any] = Field(default_factory=dict)


class SearchHit(BaseModel):
    citation_id: str
    chunk_id: str
    document_id: str
    document_version_id: str
    text: str
    score: float
    source: Dict[str, Any]
