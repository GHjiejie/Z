"""Service-owned tables; LlamaIndex nodes are stored as rebuildable projections."""

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()


def scope():
    return [
        Column("tenant_id", String(128), nullable=False),
        Column("project_id", String(128), nullable=False),
    ]


def entity():
    return [
        Column("id", String(64), primary_key=True),
        *scope(),
        Column("created_at", Float, nullable=False),
    ]


def scoped_unique():
    return UniqueConstraint("tenant_id", "project_id", "id")


def scoped_fk(column, target):
    return ForeignKeyConstraint(
        ["tenant_id", "project_id", column],
        [f"{target}.tenant_id", f"{target}.project_id", f"{target}.id"],
    )


schema_version = Table(
    "rag_schema_version", metadata, Column("version", Integer, primary_key=True)
)
memberships = Table(
    "rag_memberships",
    metadata,
    *scope(),
    Column("user_id", String(128), nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("roles", JSON, nullable=False),
    UniqueConstraint("tenant_id", "project_id", "user_id"),
)
rate_limits = Table(
    "rag_rate_limits",
    metadata,
    *scope(),
    Column("user_id", String(128), nullable=False),
    Column("window_start", Integer, nullable=False),
    Column("count", Integer, nullable=False),
    UniqueConstraint("tenant_id", "project_id", "user_id"),
)
knowledge_bases = Table(
    "rag_knowledge_bases",
    metadata,
    *entity(),
    Column("name", String(255), nullable=False),
    Column("description", Text, nullable=False, default=""),
    Column("status", String(32), nullable=False),
    Column("active_generation_id", String(64)),
    scoped_unique(),
)
kb_permissions = Table(
    "rag_kb_permissions",
    metadata,
    *scope(),
    Column("kb_id", String(64), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("role", String(16), nullable=False),
    UniqueConstraint("tenant_id", "project_id", "kb_id", "user_id"),
    scoped_fk("kb_id", "rag_knowledge_bases"),
)
generations = Table(
    "rag_generations",
    metadata,
    *entity(),
    Column("kb_id", String(64), nullable=False),
    Column("embedding_model", String(255), nullable=False),
    Column("embedding_revision", String(128), nullable=False),
    Column("embedding_dimension", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    scoped_unique(),
    scoped_fk("kb_id", "rag_knowledge_bases"),
)
documents = Table(
    "rag_documents",
    metadata,
    *entity(),
    Column("kb_id", String(64), nullable=False),
    Column("filename", String(255), nullable=False),
    Column("active_version_id", String(64)),
    Column("update_sequence", Integer, nullable=False, default=0),
    Column("deleted_at", Float),
    Column("acl_restricted", Boolean, nullable=False, default=False),
    Column("allowed_roles", JSON, nullable=False, default=list),
    scoped_unique(),
    scoped_fk("kb_id", "rag_knowledge_bases"),
)
document_permissions = Table(
    "rag_document_permissions",
    metadata,
    *scope(),
    Column("document_id", String(64), nullable=False),
    Column("user_id", String(128), nullable=False),
    UniqueConstraint("tenant_id", "project_id", "document_id", "user_id"),
    scoped_fk("document_id", "rag_documents"),
)
versions = Table(
    "rag_versions",
    metadata,
    *entity(),
    Column("document_id", String(64), nullable=False),
    Column("generation_id", String(64), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("filename", String(255), nullable=False),
    Column("object_key", Text, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("content_type", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    UniqueConstraint("document_id", "sequence"),
    scoped_unique(),
    scoped_fk("document_id", "rag_documents"),
    scoped_fk("generation_id", "rag_generations"),
)
chunks = Table(
    "rag_chunks",
    metadata,
    *entity(),
    Column("kb_id", String(64), nullable=False),
    Column("document_id", String(64), nullable=False),
    Column("version_id", String(64), nullable=False),
    Column("generation_id", String(64), nullable=False),
    Column("text", Text, nullable=False),
    Column("lexical_text", Text, nullable=False),
    Column("embedding", Vector().with_variant(JSON(), "sqlite"), nullable=False),
    Column("source_metadata", JSON, nullable=False),
    scoped_fk("kb_id", "rag_knowledge_bases"),
    scoped_fk("document_id", "rag_documents"),
    scoped_fk("version_id", "rag_versions"),
    scoped_fk("generation_id", "rag_generations"),
)
jobs = Table(
    "rag_jobs",
    metadata,
    *entity(),
    Column("kb_id", String(64), nullable=False),
    Column("document_id", String(64), nullable=False),
    Column("version_id", String(64)),
    Column("generation_id", String(64)),
    Column("kind", String(16), nullable=False),
    Column("status", String(32), nullable=False),
    Column("phase", String(32), nullable=False),
    Column("completed", Integer, nullable=False, default=0),
    Column("attempts", Integer, nullable=False, default=0),
    Column("available_at", Float, nullable=False),
    Column("lease_until", Float),
    Column("claim_token", String(64)),
    Column("worker_id", String(128)),
    Column("error_code", String(64)),
    Column("error_message", Text),
    scoped_unique(),
    scoped_fk("document_id", "rag_documents"),
    scoped_fk("version_id", "rag_versions"),
    scoped_fk("generation_id", "rag_generations"),
)
idempotency = Table(
    "rag_idempotency",
    metadata,
    *scope(),
    Column("user_id", String(128), nullable=False),
    Column("operation", String(128), nullable=False),
    Column("key", String(255), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("result", JSON, nullable=False),
    UniqueConstraint("tenant_id", "project_id", "user_id", "operation", "key"),
)
conversations = Table(
    "rag_conversations",
    metadata,
    *entity(),
    Column("user_id", String(128), nullable=False),
    Column("title", String(255), nullable=False, default=""),
    Column("kb_ids", JSON, nullable=False),
    Column("deleted_at", Float),
    scoped_unique(),
)
messages = Table(
    "rag_messages",
    metadata,
    *entity(),
    Column("conversation_id", String(64), nullable=False),
    Column("role", String(32), nullable=False),
    Column("content", Text, nullable=False),
    Column("citations", JSON, nullable=False),
    scoped_fk("conversation_id", "rag_conversations"),
)
query_runs = Table(
    "rag_query_runs",
    metadata,
    *entity(),
    Column("user_id", String(128), nullable=False),
    Column("conversation_id", String(64)),
    Column("status", String(32), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("result", JSON),
    Column("error_code", String(64)),
    Column("lease_until", Float, nullable=False),
    scoped_unique(),
    scoped_fk("conversation_id", "rag_conversations"),
)
feedback = Table(
    "rag_feedback",
    metadata,
    *entity(),
    Column("request_id", String(64), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("rating", String(32), nullable=False),
    Column("comment", Text),
    Column("category", String(64)),
    scoped_fk("request_id", "rag_query_runs"),
)
audit = Table(
    "rag_audit",
    metadata,
    *entity(),
    Column("user_id", String(128), nullable=False),
    Column("action", String(64), nullable=False),
    Column("target_id", String(64), nullable=False),
    Column("details", JSON, nullable=False),
)

Index("rag_jobs_claim_idx", jobs.c.status, jobs.c.available_at, jobs.c.lease_until)
Index(
    "rag_chunks_scope_idx",
    chunks.c.tenant_id,
    chunks.c.project_id,
    chunks.c.kb_id,
    chunks.c.version_id,
    chunks.c.generation_id,
)
Index(
    "rag_documents_kb_idx",
    documents.c.tenant_id,
    documents.c.project_id,
    documents.c.kb_id,
)
