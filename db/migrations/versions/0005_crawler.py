"""Persist service health, crawl jobs and release lifecycle."""

import os

from alembic import op
from sqlalchemy import text

revision = "0005"
down_revision = "0004"


def upgrade():
    for statement in (
        "ALTER TABLE services ADD COLUMN health text NOT NULL DEFAULT 'unknown'",
        "ALTER TABLE services ADD COLUMN checked_at timestamptz",
        "ALTER TABLE services ADD COLUMN health_error text",
        "ALTER TABLE tools ADD COLUMN lifecycle text NOT NULL DEFAULT 'draft'",
        "UPDATE tools SET lifecycle='published' WHERE active_version IS NOT NULL",
        "ALTER TABLE tool_versions ADD COLUMN source jsonb",
        "ALTER TABLE publications ADD COLUMN action text NOT NULL DEFAULT 'publish'",
        "GRANT INSERT ON services TO toolmesh_app",
        "GRANT UPDATE (health,checked_at,health_error,name) ON services TO toolmesh_app",
        "GRANT UPDATE (lifecycle) ON tools TO toolmesh_app",
        """CREATE TABLE crawl_jobs (
            id uuid PRIMARY KEY, service_id text NOT NULL REFERENCES services,
            actor text NOT NULL, status text NOT NULL DEFAULT 'QUEUED',
            requested_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz,
            finished_at timestamptz, error text, changes jsonb NOT NULL DEFAULT '[]'
        )""",
        "CREATE INDEX crawl_jobs_queue ON crawl_jobs (status,requested_at)",
        "CREATE UNIQUE INDEX crawl_jobs_one_active ON crawl_jobs(service_id) WHERE status IN ('QUEUED','RUNNING')",
        "GRANT SELECT,INSERT,UPDATE ON crawl_jobs TO toolmesh_app",
        "CREATE SCHEMA inventory",
        "CREATE SCHEMA it",
        """CREATE TABLE inventory.items (
            id text PRIMARY KEY, customer_id text NOT NULL, quantity integer NOT NULL, location text NOT NULL
        )""",
        "INSERT INTO inventory.items VALUES ('SKU-102','C-102',12,'EAST'),('SKU-104','C-104',5,'WEST')",
        "CREATE TABLE inventory.schema_state (id boolean PRIMARY KEY DEFAULT true CHECK(id), require_location boolean NOT NULL DEFAULT false)",
        "INSERT INTO inventory.schema_state DEFAULT VALUES",
        """CREATE TABLE it.services (
            id text PRIMARY KEY, customer_id text NOT NULL, status text NOT NULL
        )""",
        """INSERT INTO it.services VALUES ('sandbox-api','C-102','degraded'),
            ('sandbox-worker','C-102','healthy'),('restricted-api','C-104','healthy')""",
        """CREATE TABLE it.incidents (
            id uuid PRIMARY KEY, service_id text NOT NULL REFERENCES it.services,
            customer_id text NOT NULL, title text NOT NULL, status text NOT NULL DEFAULT 'open',
            created_at timestamptz NOT NULL DEFAULT now()
        )""",
    ):
        op.execute(statement)
    connection = op.get_bind()
    for key in ("inventory", "it"):
        role = f"toolmesh_{key}"
        statement = connection.execute(
            text(
                "SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', CAST(:role AS text), CAST(:password AS text))"
            ),
            {
                "role": role,
                "password": os.environ[f"TOOLMESH_{key.upper()}_DB_PASSWORD"],
            },
        ).scalar_one()
        op.execute(statement)
        op.execute(f"GRANT CONNECT ON DATABASE toolmesh TO {role}")
        op.execute(f"GRANT USAGE ON SCHEMA {key} TO {role}")
        op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {key} TO {role}")
    op.execute("GRANT INSERT ON it.incidents TO toolmesh_it")


def downgrade():
    raise RuntimeError("Reverse this migration by restoring a verified backup.")
