"""Provide restricted public protected admission and decision access."""

from alembic import op

revision = "0012"
down_revision = "0011"

ROLE = "toolmesh_protected_api"

GRANTS = (
    "USAGE ON SCHEMA public",
    "SELECT ON public.agent_accounts, public.policy_rules, public.refund_grants",
    "SELECT ON public.tools, public.tool_versions",
    "SELECT (singleton, mode), UPDATE (singleton) ON public.execution_dispatch_gate",
    "SELECT (singleton, revision), UPDATE (singleton) ON public.execution_authorization_guard",
    "UPDATE (id) ON public.tools",
    "SELECT, INSERT ON public.execution_request_keys",
    "UPDATE (execution_id) ON public.execution_request_keys",
    "SELECT ON public.execution_legacy_key_aliases, public.execution_approval_entitlements",
    "SELECT ON public.execution_commands",
    "INSERT (execution_id,workspace_id,requester,customer_id,tool_id,tool_version,contract_digest,"
    "state,ciphertext,wrapped_data_key,nonce,key_version) ON public.execution_commands",
    "UPDATE (state, dispatch_not_after, scheduled_at) ON public.execution_commands",
    "SELECT, INSERT ON public.execution_approval_decisions",
    "SELECT (execution_id,outcome,ciphertext,wrapped_data_key,nonce,key_version) ON public.execution_receipts",
    "SELECT (id,principal,tool_id,version,status,error_code,started_at,finished_at,request_id,otel_trace_id)"
    " ON public.executions",
    "INSERT (id,principal,tool_id,version,status) ON public.executions",
    "UPDATE (status, finished_at) ON public.executions",
    "SELECT, INSERT ON public.approvals",
    "UPDATE (status, decided_by, decided_at, decision_reason, reconcile_after) ON public.approvals",
    "INSERT (actor, action, resource_id) ON public.audit_events",
    "SELECT (id, actor, action, resource_id, created_at) ON public.audit_events",
)


def upgrade() -> None:
    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='{ROLE}') THEN
                CREATE ROLE {ROLE} NOLOGIN;
            END IF;
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO {ROLE}', current_database());
        END
        $$
    """)
    for grant in GRANTS:
        op.execute(f"GRANT {grant} TO {ROLE}")
    op.execute(
        "GRANT SELECT (singleton, mode) ON public.execution_dispatch_gate TO toolmesh_app"
    )

    op.execute("""
        CREATE FUNCTION public.protected_legacy_open() RETURNS boolean
        LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog AS $$
            SELECT COALESCE((SELECT mode='LEGACY' FROM public.execution_dispatch_gate
                             WHERE singleton FOR SHARE), false)
        $$
    """)
    op.execute("REVOKE EXECUTE ON FUNCTION public.protected_legacy_open() FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.protected_legacy_open() TO toolmesh_app"
    )

    # Legacy and durable still share the public projections. Check the
    # effective writer before any old durable-backed row can be changed.
    op.execute("""
        CREATE FUNCTION public.protect_durable_shared_row() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog AS $$
        BEGIN
            IF pg_has_role(current_user,'toolmesh_app','USAGE')
               AND NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user)
            THEN
                IF public.is_durable_approval(OLD.id) THEN
                    RAISE EXCEPTION 'durable projection requires a durable writer'
                        USING ERRCODE = '42501';
                END IF;
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute(
        "REVOKE EXECUTE ON FUNCTION public.protect_durable_shared_row() FROM PUBLIC"
    )
    for table in ("executions", "approvals"):
        op.execute(f"""
            CREATE TRIGGER protect_durable_shared_row BEFORE UPDATE OR DELETE ON public.{table}
            FOR EACH ROW EXECUTE FUNCTION public.protect_durable_shared_row()
        """)
        op.execute(
            f"ALTER TABLE public.{table} ENABLE ALWAYS TRIGGER protect_durable_shared_row"
        )


def downgrade() -> None:
    for table in ("executions", "approvals"):
        op.execute(f"DROP TRIGGER protect_durable_shared_row ON public.{table}")
    op.execute("DROP FUNCTION public.protect_durable_shared_row()")
    op.execute("DROP FUNCTION public.protected_legacy_open()")
    op.execute(
        "REVOKE SELECT (singleton, mode) ON public.execution_dispatch_gate FROM toolmesh_app"
    )
    op.execute(f"""DO $$ BEGIN
        EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM {ROLE}', current_database());
    END $$""")
    for grant in reversed(GRANTS):
        op.execute(f"REVOKE {grant} FROM {ROLE}")
    # PostgreSQL roles are cluster-wide and can serve another migrated database.
