"""Add ranked discovery and isolated, durable retrieval evaluations."""

from alembic import op

revision = "0006"
down_revision = "0005"


def upgrade():
    for statement in (
        """ALTER TABLE tool_versions ADD COLUMN search_terms tsvector GENERATED ALWAYS AS (
            setweight(to_tsvector('english',translate(tool_id,'._','  ')), 'A')
            || setweight(to_tsvector('english',coalesce(contract->>'description','')), 'B')
        ) STORED""",
        "CREATE INDEX tool_versions_search ON tool_versions USING gin(search_terms)",
        """CREATE TABLE tool_embeddings (
            tool_id text NOT NULL, version integer NOT NULL, model text NOT NULL,
            embedding vector(256) NOT NULL,
            PRIMARY KEY (tool_id,version,model),
            FOREIGN KEY (tool_id,version) REFERENCES tool_versions(tool_id,version)
        )""",
        "GRANT SELECT,INSERT ON tool_embeddings TO toolmesh_app",
        """CREATE TABLE evaluation_runs (
            id uuid PRIMARY KEY, actor text NOT NULL, catalog_size int NOT NULL,
            case_count int NOT NULL, seed int NOT NULL, status text NOT NULL DEFAULT 'QUEUED',
            stage text NOT NULL DEFAULT 'QUEUED', processed int NOT NULL DEFAULT 0,
            configuration jsonb NOT NULL, summary jsonb, error_code text,
            requested_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz, finished_at timestamptz
        )""",
        "CREATE UNIQUE INDEX evaluations_one_active ON evaluation_runs ((1)) WHERE status IN ('QUEUED','RUNNING')",
        """CREATE TABLE evaluation_documents (
            run_id uuid NOT NULL REFERENCES evaluation_runs, id text NOT NULL, domain text NOT NULL,
            restricted boolean NOT NULL, definition jsonb NOT NULL, embedding vector(256) NOT NULL,
            search_terms tsvector GENERATED ALWAYS AS (
                setweight(to_tsvector('english',definition->>'name'),'A')
                || setweight(to_tsvector('english',definition->>'description'),'B')
            ) STORED, PRIMARY KEY (run_id,id)
        )""",
        "CREATE INDEX evaluation_documents_search ON evaluation_documents USING gin(search_terms)",
        """CREATE TABLE evaluation_cases (
            run_id uuid NOT NULL REFERENCES evaluation_runs, id text NOT NULL,
            domain text NOT NULL, kind text NOT NULL, query text NOT NULL, expected jsonb NOT NULL,
            rankings jsonb NOT NULL, measures jsonb NOT NULL, PRIMARY KEY (run_id,id)
        )""",
        "GRANT SELECT,INSERT,UPDATE ON evaluation_runs TO toolmesh_app",
        "GRANT SELECT,INSERT,DELETE ON evaluation_documents,evaluation_cases TO toolmesh_app",
    ):
        op.execute(statement)


def downgrade():
    for statement in (
        "DROP TABLE evaluation_cases,evaluation_documents,evaluation_runs,tool_embeddings",
        "ALTER TABLE tool_versions DROP COLUMN search_terms",
    ):
        op.execute(statement)
