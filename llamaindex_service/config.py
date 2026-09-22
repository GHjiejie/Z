"""Configuration is explicit; no model SDK or credentials are loaded on import."""

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RAG_", env_file=".env", extra="ignore", validate_default=True
    )

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://rag:rag@127.0.0.1:55432/rag"
    auto_migrate: bool = False
    auth_mode: Literal["dev", "jwt"] = "dev"
    dev_tenant_id: str = "local"
    dev_project_id: str = "default"
    dev_user_id: str = "developer"
    jwt_public_key: str = ""
    jwt_algorithm: Literal["RS256", "ES256"] = "RS256"
    jwt_issuer: str = ""
    jwt_audience: str = ""
    model_mode: Literal["live", "mock"] = "live"
    embedding_model: str = "text-embedding-3-small"
    embedding_revision: str = "1"
    embedding_dimension: int = Field(default=1536, ge=8, le=2000)
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_batch_size: int = Field(default=32, ge=1, le=256)
    storage_backend: Literal["local", "s3"] = "local"
    storage_path: Path = Path("llamaindex_service/data/objects")
    s3_bucket: str = ""
    s3_endpoint: str | None = None
    s3_region: str = "us-east-1"
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    max_pages: int = Field(default=500, ge=1)
    parse_timeout_seconds: float = Field(default=60, gt=0)
    parser_memory_mb: int = Field(default=512, ge=128)
    max_extracted_characters: int = Field(default=5_000_000, ge=1)
    max_docx_entries: int = Field(default=2000, ge=1)
    max_docx_uncompressed_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    max_docx_compression_ratio: int = Field(default=200, ge=1)
    worker_lease_seconds: int = Field(default=120, ge=5)
    worker_poll_seconds: float = Field(default=2, gt=0)
    worker_max_attempts: int = Field(default=3, ge=1, le=10)
    chunk_size: int = Field(default=600, ge=64, le=4096)
    chunk_overlap: int = Field(default=80, ge=0)
    generation_timeout_seconds: float = Field(default=120, gt=0)
    rerank_base_url: str = ""
    rerank_api_key: str = ""
    rerank_model: str = ""
    rerank_timeout_seconds: float = Field(default=5, gt=0)
    dense_top_k: int = Field(default=30, ge=1, le=100)
    sparse_top_k: int = Field(default=30, ge=1, le=100)
    rerank_top_k: int = Field(default=20, ge=1, le=100)
    context_top_k: int = Field(default=8, ge=1, le=20)
    context_max_tokens: int = Field(default=6000, ge=512, le=32000)
    min_similarity: float = Field(default=0.35, ge=-1, le=1)
    max_concurrent_answers: int = Field(default=4, ge=1, le=100)
    requests_per_minute: int = Field(default=120, ge=1)
    history_max_messages: int = Field(default=10, ge=0, le=100)
    conversation_retention_days: int = Field(default=30, ge=1)
    maintenance_interval_seconds: float = Field(default=3600, gt=0)
    maintenance_scan_limit: int = Field(default=500, ge=1)
    maintenance_delete_limit: int = Field(default=100, ge=1)
    orphan_grace_seconds: int = Field(default=86400, ge=86400)

    @model_validator(mode="after")
    def validate_boundaries(self) -> "Settings":
        # Test vectors must never be accepted as vectors from a real model,
        # even when both configurations otherwise share name and dimension.
        if self.model_mode == "mock" and not self.embedding_model.startswith("mock:"):
            self.embedding_model = "mock:" + self.embedding_model
        if self.model_mode == "live" and self.embedding_model.startswith("mock:"):
            raise ValueError("Live models cannot read a mock embedding index")
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be less than chunk_size")
        if self.auth_mode == "jwt" and not all(
            (self.jwt_public_key, self.jwt_issuer, self.jwt_audience)
        ):
            raise ValueError("JWT mode requires public key, issuer and audience")
        if self.storage_backend == "s3" and not self.s3_bucket:
            raise ValueError("S3 storage requires a bucket")
        if self.environment == "production":
            if self.auth_mode != "jwt" or self.model_mode != "live":
                raise ValueError("Production requires JWT and live models")
            if not self.database_url.startswith("postgresql"):
                raise ValueError("Production requires PostgreSQL")
            if self.storage_backend != "s3" or self.auto_migrate:
                raise ValueError(
                    "Production requires object storage and explicit migrations"
                )
        return self
