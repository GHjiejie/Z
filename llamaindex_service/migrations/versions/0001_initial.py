"""Revision 0001: business state, durable queue, vector projection and scope RLS.

The initial snapshot includes revision 0002 columns for fresh installations.
Future revisions must explicitly alter existing tables.
"""

revision = "0001"
down_revision = None


def upgrade():
    from alembic import op

    from llamaindex_service.migrations import install_schema

    install_schema(op.get_bind())


def downgrade():
    raise RuntimeError(
        "Downgrade would destroy enterprise data; restore a validated backup instead"
    )
