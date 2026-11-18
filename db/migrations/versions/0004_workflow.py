"""Persist guided duplicate-payment runs and structured ticket evidence."""

from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade():
    op.execute("ALTER TABLE support.tickets ADD COLUMN duplicate_transactions text[]")
    op.execute("""
        CREATE TABLE duplicate_workflows (
            id uuid PRIMARY KEY, customer_id text NOT NULL, order_id text NOT NULL,
            started_by text NOT NULL, evidence jsonb, ticket_id uuid, refund_id uuid REFERENCES approvals,
            notification_id uuid, status text NOT NULL DEFAULT 'STARTED',
            created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX duplicate_workflows_customer_time ON duplicate_workflows(customer_id, created_at DESC)"
    )
    op.execute("GRANT SELECT,INSERT,UPDATE ON duplicate_workflows TO toolmesh_app")


def downgrade():
    op.execute("DROP TABLE duplicate_workflows")
    op.execute("ALTER TABLE support.tickets DROP COLUMN duplicate_transactions")
