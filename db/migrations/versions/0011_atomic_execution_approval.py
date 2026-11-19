"""Add encrypted decision evidence and coordinate authorization writers."""

from alembic import op

revision = "0011"
down_revision = "0010"

AUTHORIZATION_TABLES = (
    "agent_accounts",
    "policy_rules",
    "refund_grants",
    "execution_approval_entitlements",
    "tools",
    "tool_versions",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE execution_approval_decisions (
            execution_id uuid PRIMARY KEY REFERENCES execution_commands(execution_id),
            authorization_revision bigint NOT NULL CHECK (authorization_revision > 0),
            reason_digest bytea NOT NULL CHECK (octet_length(reason_digest) = 32),
            ciphertext bytea NOT NULL CHECK (octet_length(ciphertext) > 0),
            wrapped_data_key bytea NOT NULL CHECK (octet_length(wrapped_data_key) > 0),
            nonce bytea NOT NULL CHECK (octet_length(nonce) = 12),
            key_version integer NOT NULL CHECK (key_version > 0)
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.lock_execution_authorization() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            PERFORM revision FROM public.execution_authorization_guard
            WHERE singleton FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'EXECUTION_COORDINATION_UNAVAILABLE' USING ERRCODE = '55000';
            END IF;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.advance_execution_authorization() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            UPDATE public.execution_authorization_guard SET revision=revision+1
            WHERE singleton;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'EXECUTION_COORDINATION_UNAVAILABLE' USING ERRCODE = '55000';
            END IF;
            RETURN NULL;
        END
        $$
        """
    )
    for table in AUTHORIZATION_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER execution_authorization_changed
            BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION public.advance_execution_authorization()
            """
        )
    op.execute(
        """
        CREATE FUNCTION public.is_durable_approval(approval_id uuid) RETURNS boolean
        LANGUAGE sql STABLE STRICT SECURITY DEFINER SET search_path = pg_catalog AS $$
            SELECT EXISTS (
                SELECT 1 FROM public.execution_commands WHERE execution_id=approval_id
            )
        $$
        """
    )
    for function in (
        "lock_execution_authorization()",
        "advance_execution_authorization()",
        "is_durable_approval(uuid)",
    ):
        op.execute(f"REVOKE EXECUTE ON FUNCTION public.{function} FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.lock_execution_authorization(), "
        "public.is_durable_approval(uuid) TO toolmesh_app"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            LOCK TABLE execution_dispatch_gate, execution_approval_decisions
                IN ACCESS EXCLUSIVE MODE;
            IF (SELECT mode FROM execution_dispatch_gate WHERE singleton) IS DISTINCT FROM 'LEGACY'
                OR EXISTS (SELECT 1 FROM execution_approval_decisions)
            THEN
                RAISE EXCEPTION 'Cannot downgrade an active or populated approval decision ledger';
            END IF;
        END
        $$
        """
    )
    for table in AUTHORIZATION_TABLES:
        op.execute(f"DROP TRIGGER execution_authorization_changed ON {table}")
    op.execute("DROP FUNCTION public.is_durable_approval(uuid)")
    op.execute("DROP FUNCTION public.advance_execution_authorization()")
    op.execute("DROP FUNCTION public.lock_execution_authorization()")
    op.drop_table("execution_approval_decisions")
