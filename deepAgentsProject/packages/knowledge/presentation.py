"""Public knowledge metadata contracts; storage and lease records stay internal."""


def select(row, fields):
    return {field: row[field] for field in fields if field in row}


def job_view(row):
    return select(row, (
        "id", "knowledge_base_id", "document_version_id", "status", "stage",
        "attempts", "chunk_count", "error_code", "error_message", "created_at", "updated_at",
    ))


def revision_view(row):
    # The manifest contains every document/chunk identity, including private sources.
    # Compilation and integrity verification read the original record internally.
    return select(row, (
        "id", "knowledge_base_id", "revision_number", "status", "retrieval_profile",
        "embedding_model", "embedding_dimensions", "index_hash", "created_at",
        "activated_at", "deprecated_at",
    ))


def version_view(row):
    return select(row, (
        "id", "document_id", "revision_number", "status", "content_type", "size_bytes",
        "content_sha256", "parser_version", "chunker_version", "embedding_revision_id",
        "indexed_at", "error_code", "error_message", "created_at",
    ))


EVENT_FIELDS = {
    "knowledge.base.created": ("knowledge_base_id", "name"),
    "knowledge.upload.prepared": ("document_id", "document_version_id"),
    "knowledge.ingestion.queued": ("job_id", "document_version_id"),
    "knowledge.ingestion.started": ("job_id",),
    "knowledge.ingestion.completed": ("job_id", "chunk_count", "revision_id"),
    "knowledge.ingestion.failed": ("job_id", "error_code", "error_message"),
    "knowledge.search.completed": ("audit_id", "result_count", "latency_ms", "revision_count"),
}


def event_view(row):
    result = select(row, (
        "id", "knowledge_base_id", "document_version_id", "ingestion_job_id", "type", "created_at",
    ))
    result["payload"] = select(row.get("payload") or {}, EVENT_FIELDS[row["type"]])
    return result
