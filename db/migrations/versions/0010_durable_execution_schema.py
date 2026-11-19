"""Add the dormant Slice A execution ledger and sandbox coordination records."""

from alembic import op

revision = "0010"
down_revision = "0009"


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE approvals ADD CONSTRAINT approvals_execution_intent_binding
        UNIQUE (id, requester, tool_id, version, customer_id)
        """
    )
    op.execute(
        """
        CREATE TABLE execution_commands (
            execution_id uuid PRIMARY KEY REFERENCES executions(id),
            workspace_id text NOT NULL REFERENCES workspaces(id),
            requester text NOT NULL CHECK (requester <> ''),
            customer_id text NOT NULL CHECK (customer_id <> ''),
            tool_id text NOT NULL CHECK (tool_id IN ('payments.issue_refund','it.restart_service')),
            tool_version integer NOT NULL CHECK (tool_version > 0),
            contract_digest bytea NOT NULL CHECK (octet_length(contract_digest) = 32),
            state text NOT NULL CHECK (state IN (
                'RECEIVED','PENDING_APPROVAL','READY','CLAIMED','DISPATCHING','UNKNOWN',
                'DENIED','REJECTED','EXPIRED','SUCCEEDED','FAILED'
            )),
            ciphertext bytea,
            wrapped_data_key bytea,
            nonce bytea,
            key_version integer,
            scheduled_at timestamptz NOT NULL DEFAULT now(),
            dispatch_not_after timestamptz,
            lease_owner uuid,
            lease_expires_at timestamptz,
            generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
            verification_only boolean NOT NULL DEFAULT false,
            next_check_at timestamptz,
            FOREIGN KEY (execution_id, requester, tool_id, tool_version, customer_id)
                REFERENCES approvals (id, requester, tool_id, version, customer_id),
            FOREIGN KEY (tool_id, tool_version) REFERENCES tool_versions (tool_id, version),
            CHECK (
                num_nonnulls(ciphertext, wrapped_data_key, nonce, key_version) = 0 OR
                (num_nonnulls(ciphertext, wrapped_data_key, nonce, key_version) = 4
                    AND octet_length(ciphertext) > 0 AND octet_length(wrapped_data_key) > 0
                    AND octet_length(nonce) = 12 AND key_version > 0)
            ),
            CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL)),
            CHECK (lease_owner IS NULL OR (
                generation > 0 AND state IN ('CLAIMED','DISPATCHING','UNKNOWN')
            )),
            CHECK (state NOT IN ('CLAIMED','DISPATCHING') OR lease_owner IS NOT NULL),
            CHECK (state <> 'PENDING_APPROVAL' OR ciphertext IS NOT NULL),
            CHECK (state NOT IN ('READY','CLAIMED','DISPATCHING') OR (
                NOT verification_only AND ciphertext IS NOT NULL AND dispatch_not_after IS NOT NULL
            )),
            CHECK (NOT verification_only OR state IN ('UNKNOWN','SUCCEEDED','FAILED')),
            CHECK (next_check_at IS NULL OR state = 'UNKNOWN')
        )
        """
    )
    op.execute(
        """
        CREATE TABLE execution_request_keys (
            execution_id uuid PRIMARY KEY,
            workspace_id text NOT NULL REFERENCES workspaces(id),
            requester text NOT NULL CHECK (requester <> ''),
            customer_id text NOT NULL CHECK (customer_id <> ''),
            tool_id text NOT NULL CHECK (tool_id IN ('payments.issue_refund','it.restart_service')),
            tool_version integer NOT NULL CHECK (tool_version > 0),
            key_digest bytea NOT NULL CHECK (octet_length(key_digest) = 32),
            intent_digest bytea NOT NULL CHECK (octet_length(intent_digest) = 32),
            digest_key_version integer NOT NULL CHECK (digest_key_version > 0),
            canonicalizer_version integer NOT NULL CHECK (canonicalizer_version > 0),
            UNIQUE (workspace_id, requester, customer_id, tool_id, key_digest),
            UNIQUE (execution_id, requester, tool_id),
            FOREIGN KEY (execution_id, requester, tool_id, tool_version, customer_id)
                REFERENCES approvals (id, requester, tool_id, version, customer_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE execution_legacy_key_aliases (
            requester text NOT NULL,
            tool_id text NOT NULL,
            key_digest bytea NOT NULL CHECK (octet_length(key_digest) = 32),
            execution_id uuid NOT NULL,
            PRIMARY KEY (requester, tool_id, key_digest),
            UNIQUE (execution_id),
            FOREIGN KEY (execution_id, requester, tool_id)
                REFERENCES execution_request_keys (execution_id, requester, tool_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE execution_attempts (
            execution_id uuid NOT NULL REFERENCES execution_commands(execution_id),
            generation bigint NOT NULL CHECK (generation > 0),
            worker_id uuid NOT NULL,
            kind text NOT NULL CHECK (kind IN ('SEND','VERIFY')),
            claimed_at timestamptz NOT NULL,
            send_marked_at timestamptz,
            admission_revision bigint CHECK (admission_revision > 0),
            finished_at timestamptz,
            outcome text CHECK (outcome IN ('NOT_SENT','UNKNOWN','SUCCEEDED','FAILED')),
            PRIMARY KEY (execution_id, generation),
            UNIQUE (execution_id, generation, outcome),
            CHECK ((send_marked_at IS NULL) = (admission_revision IS NULL)),
            CHECK (kind = 'SEND' OR (send_marked_at IS NULL AND outcome IS DISTINCT FROM 'NOT_SENT')),
            CHECK ((finished_at IS NULL) = (outcome IS NULL)),
            CHECK (kind <> 'SEND' OR outcome IS NULL OR (
                (outcome = 'NOT_SENT') = (send_marked_at IS NULL)
            ))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE execution_receipts (
            execution_id uuid PRIMARY KEY,
            generation bigint NOT NULL CHECK (generation > 0),
            outcome text NOT NULL CHECK (outcome IN ('SUCCEEDED','FAILED')),
            evidence_source text NOT NULL CHECK (evidence_source IN ('RESPONSE','LOOKUP')),
            external_reference text NOT NULL CHECK (external_reference <> ''),
            evidence_digest bytea NOT NULL CHECK (octet_length(evidence_digest) = 32),
            recorded_at timestamptz NOT NULL DEFAULT now(),
            ciphertext bytea,
            wrapped_data_key bytea,
            nonce bytea,
            key_version integer,
            FOREIGN KEY (execution_id, generation, outcome)
                REFERENCES execution_attempts (execution_id, generation, outcome),
            CHECK (
                num_nonnulls(ciphertext, wrapped_data_key, nonce, key_version) = 0 OR
                (num_nonnulls(ciphertext, wrapped_data_key, nonce, key_version) = 4
                    AND octet_length(ciphertext) > 0 AND octet_length(wrapped_data_key) > 0
                    AND octet_length(nonce) = 12 AND key_version > 0)
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE execution_approval_entitlements (
            principal text NOT NULL CHECK (principal <> ''),
            customer_id text NOT NULL CHECK (customer_id <> ''),
            tool_id text NOT NULL REFERENCES tools(id)
                CHECK (tool_id IN ('payments.issue_refund','it.restart_service')),
            enabled boolean NOT NULL DEFAULT true,
            PRIMARY KEY (principal, customer_id, tool_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE execution_authorization_guard (
            singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE execution_dispatch_gate (
            singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            mode text NOT NULL DEFAULT 'LEGACY' CHECK (mode IN ('LEGACY','PAUSED','DURABLE'))
        )
        """
    )
    op.execute("INSERT INTO execution_authorization_guard DEFAULT VALUES")
    op.execute("INSERT INTO execution_dispatch_gate DEFAULT VALUES")
    op.execute(
        """
        INSERT INTO execution_approval_entitlements (principal, customer_id, tool_id)
        VALUES ('refund-approver','C-102','payments.issue_refund'),
               ('refund-approver','C-102','it.restart_service')
        """
    )
    op.execute(
        """
        CREATE INDEX execution_commands_ready ON execution_commands (scheduled_at, execution_id)
        WHERE state = 'READY'
        """
    )
    op.execute(
        """
        CREATE INDEX execution_commands_expired_lease ON execution_commands (lease_expires_at, execution_id)
        WHERE lease_expires_at IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX execution_commands_verify ON execution_commands (next_check_at, execution_id)
        WHERE state = 'UNKNOWN' AND next_check_at IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX execution_attempts_one_send ON execution_attempts (execution_id)
        WHERE send_marked_at IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            LOCK TABLE execution_dispatch_gate, execution_commands, execution_request_keys
                IN ACCESS EXCLUSIVE MODE;
            IF (SELECT mode FROM execution_dispatch_gate WHERE singleton) IS DISTINCT FROM 'LEGACY'
                OR EXISTS (SELECT 1 FROM execution_commands)
                OR EXISTS (SELECT 1 FROM execution_request_keys)
            THEN
                RAISE EXCEPTION 'Cannot downgrade a transferred or populated durable execution ledger';
            END IF;
        END
        $$
        """
    )
    for table in (
        "execution_receipts",
        "execution_attempts",
        "execution_legacy_key_aliases",
        "execution_request_keys",
        "execution_commands",
        "execution_approval_entitlements",
        "execution_authorization_guard",
        "execution_dispatch_gate",
    ):
        op.drop_table(table)
    op.execute(
        "ALTER TABLE approvals DROP CONSTRAINT approvals_execution_intent_binding"
    )
