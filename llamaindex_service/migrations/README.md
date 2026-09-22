# Schema migrations

Run `uv run --group rag python -m llamaindex_service.migrations` explicitly with
`RAG_DATABASE_URL` set to a migration-owner connection. Alembic revisions 0001/0002
create the pgvector extension, service tables and target-generation job support
in one transaction. The service-specific version table is `rag_alembic_version`.
Repeated runs are safe; pre-Alembic development schemas are upgraded explicitly.
Application and Worker startup do not implicitly migrate production databases.

PostgreSQL roles `rag_api` and `rag_worker` are group roles without login. A DBA
creates separate login roles and grants the API group to the API login and the
Worker group to the Worker login. Never grant the Worker group to the API login.
Set a distinct `RAG_DATABASE_URL` on each process. The migration owner must be able
to create the extension and group roles. Production readiness rejects table owner,
superuser and BYPASSRLS connections. Schema migration credentials must never be
used by the running service.

All tenant tables enable and force RLS. API transactions install tenant/project
settings transaction-locally, so pooled connections cannot retain request scope.
The worker group intentionally sees all tenants to claim the shared queue. SQL
predicates additionally enforce knowledge-base membership, document ACL, active
version, active generation and deletion on both recall paths. No end user receives
database credentials.

The vector path currently executes exact cosine ranking over the authorized
candidate set. This is a correctness-first implementation without a production
capacity claim. The lexical path has a GIN index using PostgreSQL simple lexemes;
Chinese preprocessing lives in the versioned application tokenizer. Introducing
an ANN index requires workload/filtered-recall evaluation and a new migration.

SQLite exists only for deterministic tests; it does not provide RLS, SKIP LOCKED,
native pgvector or multi-process admission. The root project contains the only
Python environment and lockfile.
