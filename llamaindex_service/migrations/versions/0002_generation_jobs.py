"""Revision 0002: target-generation jobs for staged index rebuilds."""

revision = "0002"
down_revision = "0001"


def upgrade():
    from alembic import op

    from llamaindex_service.migrations import install_schema

    # Also upgrades development databases created before the Alembic wrapper.
    install_schema(op.get_bind())


def downgrade():
    raise RuntimeError("Dropping index generations requires an explicit data migration")
