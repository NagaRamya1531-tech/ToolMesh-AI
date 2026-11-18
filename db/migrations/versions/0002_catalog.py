"""Persist catalog releases, sessions, traces, and isolated sandbox data."""

import json
import os
from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "0002"
down_revision = "0001"


def upgrade():
    connection = op.get_bind()
    for domain in ("payments", "support"):
        password = os.environ[f"TOOLMESH_{domain.upper()}_DB_PASSWORD"]
        role = f"toolmesh_{domain}"
        statement = connection.execute(
            text(
                "SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', CAST(:role AS text), CAST(:password AS text))"
            ),
            {"role": role, "password": password},
        ).scalar_one()
        op.execute(statement)
        op.execute(f"GRANT CONNECT ON DATABASE toolmesh TO {role}")
        op.execute(f"CREATE SCHEMA {domain}")
        op.execute(f"GRANT USAGE ON SCHEMA {domain} TO {role}")

    for statement in (
        """CREATE TABLE sessions (
            digest text PRIMARY KEY, principal text NOT NULL,
            expires_at timestamptz NOT NULL
        )""",
        """CREATE TABLE services (
            id text PRIMARY KEY, name text NOT NULL, protocol text NOT NULL,
            owner text NOT NULL
        )""",
        """CREATE TABLE tools (
            id text PRIMARY KEY, service_id text NOT NULL REFERENCES services,
            active_version integer
        )""",
        """CREATE TABLE tool_versions (
            tool_id text REFERENCES tools, version integer NOT NULL,
            contract jsonb NOT NULL, PRIMARY KEY (tool_id, version)
        )""",
        """ALTER TABLE tools ADD FOREIGN KEY (id, active_version)
            REFERENCES tool_versions(tool_id, version)""",
        """CREATE TABLE publications (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tool_id text REFERENCES tools, version integer NOT NULL,
            actor text NOT NULL, reason text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )""",
        """CREATE TABLE executions (
            id uuid PRIMARY KEY, principal text NOT NULL, tool_id text,
            version integer, status text NOT NULL, error_code text,
            result jsonb, started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz
        )""",
        "CREATE INDEX executions_principal_time ON executions (principal, started_at DESC)",
        """CREATE TABLE payments.transactions (
            id text PRIMARY KEY, customer_id text NOT NULL, order_id text NOT NULL,
            amount_minor integer NOT NULL CHECK (amount_minor > 0),
            currency text NOT NULL, status text NOT NULL, installment integer
        )""",
        """CREATE TABLE support.tickets (
            id uuid PRIMARY KEY, customer_id text NOT NULL, subject text NOT NULL,
            order_id text NOT NULL, status text NOT NULL DEFAULT 'open',
            created_at timestamptz NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX support_customer_time ON support.tickets (customer_id, created_at DESC)",
        """INSERT INTO payments.transactions VALUES
            ('TX-102-A', 'C-102', 'O-102', 4550, 'USD', 'captured', NULL),
            ('TX-102-B', 'C-102', 'O-102', 4550, 'USD', 'captured', NULL),
            ('TX-103-A', 'C-102', 'O-103', 2500, 'USD', 'captured', 1),
            ('TX-103-B', 'C-102', 'O-103', 2500, 'USD', 'captured', 2),
            ('TX-104-A', 'C-104', 'O-104', 1200, 'USD', 'captured', NULL)""",
        """INSERT INTO services VALUES
            ('payments', 'Payments', 'REST', 'platform-admin'),
            ('support', 'Customer Support', 'MCP', 'platform-admin')""",
        "GRANT SELECT, INSERT, DELETE ON sessions TO toolmesh_app",
        "GRANT SELECT ON services, tools, tool_versions, publications, executions TO toolmesh_app",
        "GRANT UPDATE (active_version) ON tools TO toolmesh_app",
        "GRANT INSERT ON tools, tool_versions, publications, executions TO toolmesh_app",
        "GRANT UPDATE (tool_id, version, status, error_code, result, finished_at) ON executions TO toolmesh_app",
        "GRANT SELECT ON payments.transactions TO toolmesh_payments",
        "GRANT SELECT, INSERT ON support.tickets TO toolmesh_support",
    ):
        op.execute(statement)

    definitions = json.loads(Path(__file__).with_name("0002_tools.json").read_text())
    for tool_id, contract in definitions.items():
        connection.execute(
            text("INSERT INTO tools (id, service_id) VALUES (:id, :service)"),
            {"id": tool_id, "service": tool_id.split(".")[0]},
        )
        connection.execute(
            text("INSERT INTO tool_versions VALUES (:id, 1, CAST(:contract AS jsonb))"),
            {"id": tool_id, "contract": json.dumps(contract)},
        )


def downgrade():
    for statement in (
        "DROP TABLE executions, publications, sessions, tool_versions, tools, services",
        "DROP TABLE support.tickets, payments.transactions",
        "DROP SCHEMA support, payments",
        "REVOKE CONNECT ON DATABASE toolmesh FROM toolmesh_payments, toolmesh_support",
        "DROP ROLE toolmesh_payments, toolmesh_support",
    ):
        op.execute(statement)
