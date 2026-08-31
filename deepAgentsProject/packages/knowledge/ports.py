from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol


@dataclass
class UploadAuthorization:
    method: str
    url: str
    expires_at: str
    headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class ObjectMetadata:
    bucket: str
    region: str
    object_key: str
    size_bytes: int
    content_type: str
    etag: Optional[str] = None
    version_id: Optional[str] = None
    storage_class: Optional[str] = None


class ObjectStorage(Protocol):
    provider: str
    bucket: str
    region: str

    def canonical_uri(self, object_key: str) -> str: ...

    def create_upload_authorization(
        self,
        object_key: str,
        content_type: str,
        expires_seconds: int = 900,
        *, size_bytes: int,
    ) -> UploadAuthorization: ...

    def put_content(self, object_key: str, content: bytes, content_type: str) -> ObjectMetadata: ...

    def head_object(self, object_key: str, version_id: Optional[str] = None) -> ObjectMetadata: ...

    def get_content(self, object_key: str, version_id: Optional[str] = None) -> bytes: ...

    def create_download_url(
        self, object_key: str, version_id: Optional[str] = None, expires_seconds: int = 300
    ) -> Optional[str]: ...


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: List[List[float]]
    input_tokens: int | None
    provider_receipt: str | None = None


class EmbeddingProvider(Protocol):
    model_revision: str
    dimensions: int

    def identity(self) -> Dict[str, str]: ...

    def embed_with_usage(self, texts: List[str]) -> EmbeddingResult: ...

    def embed_documents(self, texts: List[str]) -> List[List[float]]: ...

    def embed_query(self, text: str) -> List[float]: ...
