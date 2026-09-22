"""Run the same public API contract against real pgvector, never a production DB."""

import os
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from llamaindex_service.tests import test_api


@unittest.skipUnless(
    os.environ.get("RAG_TEST_DATABASE_URL"), "RAG_TEST_DATABASE_URL is required"
)
class PostgresApiTests(test_api.ApiTests):
    def setUp(self):
        # Worker claims across tenants by design, including cleanup jobs that
        # have no embedding profile. Each HTTP test therefore needs its own
        # queue/database rather than consuming another fixture's jobs.
        base = make_url(os.environ["RAG_TEST_DATABASE_URL"])
        database = "rag_http_" + uuid4().hex
        admin = create_engine(base, isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database}"'))

        def cleanup():
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{database}"'))
            admin.dispose()

        self.addCleanup(cleanup)
        self.database_url = base.set(database=database).render_as_string(
            hide_password=False
        )
        super().setUp()


if __name__ == "__main__":
    unittest.main()
