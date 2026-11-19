"""Persist ordered scripted agent checkpoints."""

from alembic import op

revision = "0008"
down_revision = "0007"


def upgrade():
    op.execute("""
        CREATE TABLE agent_runs (
            id uuid PRIMARY KEY, principal text NOT NULL, profile text NOT NULL,
            arguments jsonb NOT NULL, steps jsonb NOT NULL DEFAULT '[]', next_step int NOT NULL DEFAULT 0,
            status text NOT NULL DEFAULT 'QUEUED', approval_id uuid REFERENCES approvals,
            created_at timestamptz NOT NULL DEFAULT now(), touched_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX agent_runs_queue ON agent_runs (status,created_at)")
    op.execute(
        "CREATE INDEX agent_runs_owner ON agent_runs (principal,created_at DESC)"
    )
    op.execute("GRANT SELECT,INSERT,UPDATE ON agent_runs TO toolmesh_app")


def downgrade():
    op.execute("DROP TABLE agent_runs")
