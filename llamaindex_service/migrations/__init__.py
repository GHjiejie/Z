"""Explicit versioned migrations; API and Worker do not create production tables."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import insert, inspect, select, text, update

from llamaindex_service.persistence.models import metadata, schema_version


def upgrade(engine) -> None:
    """Run Alembic to head with a service-specific version table."""
    configuration = Config()
    configuration.set_main_option("script_location", str(Path(__file__).parent))
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(71030922001)"))
        configuration.attributes["connection"] = connection
        command.upgrade(configuration, "head")


def install_schema(connection) -> None:
    """Initial snapshot plus additive upgrade of pre-Alembic development schemas."""
    postgres = connection.dialect.name == "postgresql"
    if postgres:
        connection.execute(text("SELECT pg_advisory_xact_lock(71030922001)"))
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    metadata.create_all(connection)
    versions = list(connection.execute(select(schema_version.c.version)).scalars())
    if versions:
        if versions not in ([1], [2]):
            raise RuntimeError(
                "Unsupported schema revision; explicit migration required"
            )
        if versions == [1]:
            if "generation_id" not in {
                column["name"] for column in inspect(connection).get_columns("rag_jobs")
            }:
                connection.execute(
                    text("ALTER TABLE rag_jobs ADD COLUMN generation_id VARCHAR(64)")
                )
                if postgres:
                    connection.execute(
                        text(
                            "ALTER TABLE rag_jobs ADD CONSTRAINT rag_jobs_generation_scope_fk FOREIGN KEY (tenant_id, project_id, generation_id) REFERENCES rag_generations (tenant_id, project_id, id)"
                        )
                    )
            connection.execute(update(schema_version).values(version=2))
        return
    if postgres:
        connection.execute(
            text("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_api') THEN
                    CREATE ROLE rag_api NOLOGIN;
                END IF;
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rag_worker') THEN
                    CREATE ROLE rag_worker NOLOGIN;
                END IF;
            END $$
        """)
        )
        for table in metadata.sorted_tables:
            if "tenant_id" not in table.c:
                continue
            name = table.name
            connection.execute(text(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY"))
            connection.execute(text(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY"))
            connection.execute(
                text(f"""CREATE POLICY {name}_scope ON {name}
                USING ((tenant_id = current_setting('rag.tenant_id', true)
                        AND project_id = current_setting('rag.project_id', true))
                       OR pg_has_role(current_user, 'rag_worker', 'member'))
                WITH CHECK ((tenant_id = current_setting('rag.tenant_id', true)
                        AND project_id = current_setting('rag.project_id', true))
                       OR pg_has_role(current_user, 'rag_worker', 'member'))""")
            )
            connection.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON {name} TO rag_api, rag_worker"
                )
            )
        connection.execute(
            text("GRANT SELECT ON rag_schema_version TO rag_api, rag_worker")
        )
        connection.execute(
            text(
                "CREATE INDEX rag_chunks_fts_idx ON rag_chunks USING gin (to_tsvector('simple', lexical_text))"
            )
        )
    connection.execute(insert(schema_version).values(version=2))
