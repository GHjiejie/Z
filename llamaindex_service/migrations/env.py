"""Alembic entry configured by the root-environment migration command."""

from alembic import context

from llamaindex_service.persistence.models import metadata

connection = context.config.attributes["connection"]
context.configure(
    connection=connection,
    target_metadata=metadata,
    version_table="rag_alembic_version",
)
with context.begin_transaction():
    context.run_migrations()
