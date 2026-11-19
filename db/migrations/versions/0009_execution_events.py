from alembic import op

revision = "0009"
down_revision = "0008"


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE executions ADD COLUMN request_id text,
            ADD COLUMN otel_trace_id text, ADD COLUMN dispatch_risk text;
        """
    )
    op.execute(
        """
        CREATE TABLE execution_events (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            execution_id uuid NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
            status text NOT NULL,
            error_code text,
            recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
        )
        """
    )
    op.execute("GRANT SELECT ON execution_events TO toolmesh_app")
    op.execute("GRANT UPDATE (dispatch_risk) ON executions TO toolmesh_app")
    op.execute("CREATE INDEX ON execution_events (execution_id, id)")
    op.execute(
        "CREATE INDEX executions_stale_started ON executions (started_at) WHERE status IN ('RECEIVED', 'RUNNING')"
    )
    op.execute(
        """
        CREATE FUNCTION capture_execution_status() RETURNS trigger LANGUAGE plpgsql
        SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'INSERT' OR NEW.status IS DISTINCT FROM OLD.status THEN
                INSERT INTO public.execution_events (execution_id, status, error_code)
                VALUES (NEW.id, NEW.status, NEW.error_code);
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute("REVOKE EXECUTE ON FUNCTION capture_execution_status() FROM PUBLIC")
    op.execute(
        """
        CREATE TRIGGER capture_execution_status AFTER INSERT OR UPDATE OF status
        ON executions FOR EACH ROW EXECUTE FUNCTION capture_execution_status()
        """
    )


def downgrade() -> None:
    op.execute("REVOKE UPDATE (dispatch_risk) ON executions FROM toolmesh_app")
    op.execute("DROP TRIGGER capture_execution_status ON executions")
    op.execute("DROP FUNCTION capture_execution_status()")
    op.execute("DROP TABLE execution_events")
    op.execute("DROP INDEX executions_stale_started")
    op.execute(
        "ALTER TABLE executions DROP COLUMN request_id, DROP COLUMN otel_trace_id, "
        "DROP COLUMN dispatch_risk"
    )
