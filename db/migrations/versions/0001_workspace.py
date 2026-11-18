"""Provision pgvector and the default sandbox workspace."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    workspaces = op.create_table(
        "workspaces",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.bulk_insert(workspaces, [{"id": "default", "display_name": "Sandbox"}])
    op.execute("GRANT SELECT ON workspaces, alembic_version TO toolmesh_app")


def downgrade():
    op.drop_table("workspaces")
