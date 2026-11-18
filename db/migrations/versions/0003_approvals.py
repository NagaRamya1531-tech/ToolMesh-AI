"""Create approval binding, refund ledger, and local communication outbox."""

import json
import os
from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "0003"
down_revision = "0002"


def upgrade():
    conn = op.get_bind()
    statement = conn.execute(
        text(
            "SELECT format('CREATE ROLE toolmesh_communication LOGIN PASSWORD %L', CAST(:password AS text))"
        ),
        {"password": os.environ["TOOLMESH_COMMUNICATION_DB_PASSWORD"]},
    ).scalar_one()
    op.execute(statement)
    for statement in (
        "GRANT CONNECT ON DATABASE toolmesh TO toolmesh_communication",
        "CREATE SCHEMA communication",
        "GRANT USAGE ON SCHEMA communication TO toolmesh_communication",
        """CREATE TABLE communication.messages (
            id uuid PRIMARY KEY, customer_id text NOT NULL,
            refund_id uuid NOT NULL, body text NOT NULL,
            status text NOT NULL DEFAULT 'SIMULATED' CHECK(status='SIMULATED'),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(customer_id, refund_id)
        )""",
        "GRANT SELECT, INSERT ON communication.messages TO toolmesh_communication",
        """CREATE TABLE payments.refunds (
            id uuid PRIMARY KEY, transaction_id text NOT NULL REFERENCES payments.transactions,
            customer_id text NOT NULL, amount_minor integer NOT NULL CHECK (amount_minor > 0),
            currency text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX refunds_transaction ON payments.refunds (transaction_id)",
        "GRANT SELECT, INSERT ON payments.refunds TO toolmesh_payments",
        # PostgreSQL requires an UPDATE grant for SELECT FOR UPDATE.
        "GRANT UPDATE (id) ON payments.transactions TO toolmesh_payments",
        """CREATE TABLE refund_grants (
            principal text PRIMARY KEY, customer_id text NOT NULL, enabled boolean NOT NULL DEFAULT true
        )""",
        "INSERT INTO refund_grants VALUES ('finance-agent', 'C-102', true)",
        """CREATE TABLE approvals (
            id uuid PRIMARY KEY REFERENCES executions, requester text NOT NULL,
            tool_id text NOT NULL, version integer NOT NULL, customer_id text NOT NULL,
            arguments jsonb NOT NULL, intent_hash text NOT NULL, request_key text NOT NULL,
            status text NOT NULL CHECK(status IN ('PENDING','APPROVED','REJECTED','EXPIRED')),
            requested_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL DEFAULT now()+interval '15 minutes',
            decided_by text, decision_reason text, decided_at timestamptz,
            reconcile_after timestamptz,
            UNIQUE(requester, tool_id, request_key)
        )""",
        "CREATE INDEX approvals_status_expiry ON approvals(status, expires_at)",
        """CREATE TABLE audit_events (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, actor text NOT NULL,
            action text NOT NULL, resource_id uuid NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX audit_created ON audit_events(created_at DESC, id DESC)",
        "GRANT SELECT ON refund_grants TO toolmesh_app",
        "GRANT SELECT, INSERT, UPDATE ON approvals TO toolmesh_app",
        "GRANT SELECT, INSERT ON audit_events TO toolmesh_app",
        "GRANT UPDATE (status) ON support.tickets TO toolmesh_support",
        "ALTER TABLE support.tickets ADD COLUMN refund_id uuid",
        "GRANT UPDATE (refund_id) ON support.tickets TO toolmesh_support",
        """INSERT INTO services VALUES
            ('communication','Communication','MCP','platform-admin')""",
    ):
        op.execute(statement)
    snapshots = json.loads(Path(__file__).with_name("0003_tools.json").read_text())
    for tool_id, contract in snapshots.items():
        conn.execute(
            text("INSERT INTO tools (id,service_id) VALUES (:id,:service)"),
            {"id": tool_id, "service": tool_id.split(".")[0]},
        )
        conn.execute(
            text("INSERT INTO tool_versions VALUES (:id,1,CAST(:contract AS jsonb))"),
            {"id": tool_id, "contract": json.dumps(contract)},
        )


def downgrade():
    raise RuntimeError("Reverse this migration by restoring a verified backup.")
