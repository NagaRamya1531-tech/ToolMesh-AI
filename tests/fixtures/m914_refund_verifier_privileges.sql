-- Install atomically as the trusted provisioner of a disposable 0011 database.
CREATE ROLE m914_refund_verifier NOLOGIN;
CREATE ROLE m914_refund_function_owner NOLOGIN;
CREATE SCHEMA m914_refund AUTHORIZATION m914_refund_function_owner;
REVOKE ALL ON SCHEMA m914_refund FROM PUBLIC;
GRANT USAGE ON SCHEMA m914_refund TO m914_refund_verifier;
GRANT USAGE ON SCHEMA public, synthetic_refund TO m914_refund_function_owner;
GRANT USAGE ON SCHEMA synthetic_refund TO m914_refund_verifier;
GRANT SELECT (operation_id, binding, refunded_minor) ON synthetic_refund.outcomes
    TO m914_refund_verifier, m914_refund_function_owner;

GRANT SELECT (version_num) ON public.alembic_version TO m914_refund_function_owner;
GRANT SELECT, UPDATE (singleton) ON public.execution_dispatch_gate,
    public.execution_authorization_guard TO m914_refund_function_owner;
GRANT SELECT (id, status, principal, tool_id, version), UPDATE (status, error_code, finished_at)
    ON public.executions TO m914_refund_function_owner;
GRANT SELECT (id, status, arguments, request_key, intent_hash)
    ON public.approvals TO m914_refund_function_owner;
GRANT SELECT (execution_id, tool_id, state, verification_only, generation, lease_owner,
    lease_expires_at, next_check_at, workspace_id, requester, customer_id, tool_version,
    contract_digest, ciphertext, wrapped_data_key, nonce, key_version),
    UPDATE (generation, lease_owner, lease_expires_at, next_check_at, state)
    ON public.execution_commands TO m914_refund_function_owner;
GRANT SELECT (execution_id, workspace_id, requester, customer_id, tool_id, tool_version,
    key_digest, intent_digest, digest_key_version, canonicalizer_version)
    ON public.execution_request_keys TO m914_refund_function_owner;
GRANT SELECT (tool_id, version, contract) ON public.tool_versions TO m914_refund_function_owner;
GRANT SELECT (execution_id, generation, worker_id, kind, send_marked_at, outcome),
    INSERT (execution_id, generation, worker_id, kind, claimed_at),
    UPDATE (finished_at, outcome) ON public.execution_attempts TO m914_refund_function_owner;
GRANT SELECT (execution_id, generation, outcome, evidence_source, external_reference, evidence_digest),
    INSERT (execution_id, generation, outcome, evidence_source, external_reference,
        evidence_digest, recorded_at)
    ON public.execution_receipts TO m914_refund_function_owner;
GRANT SELECT (actor, action, resource_id) ON public.audit_events TO m914_refund_function_owner;
-- Required for the table lock that pins the installed audit guards. The owner
-- has no login and no exported operation updates an audit row.
GRANT INSERT ON public.audit_events TO m914_refund_function_owner;

CREATE TABLE m914_refund.claims (
    execution_id uuid PRIMARY KEY, generation bigint NOT NULL, worker_id uuid NOT NULL,
    login_name name NOT NULL
);
ALTER TABLE m914_refund.claims OWNER TO m914_refund_function_owner;

-- Keep scheduled work and expired leases as separate indexed ranges. Discovery
-- is advisory. The claim rechecks state, time and the protected marker under lock.
CREATE FUNCTION m914_refund.discover(batch integer) RETURNS TABLE (execution_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    WITH eligible AS NOT MATERIALIZED (
        SELECT c.execution_id,c.next_check_at,c.lease_expires_at
        FROM public.execution_commands c JOIN public.executions e ON e.id=c.execution_id
        WHERE c.state='UNKNOWN' AND e.status='UNKNOWN'
          AND c.tool_id='payments.issue_refund' AND e.tool_id=c.tool_id AND NOT c.verification_only
          AND EXISTS (
            SELECT 1 FROM public.execution_attempts a
            WHERE a.execution_id=c.execution_id AND a.kind='SEND'
              AND a.send_marked_at IS NOT NULL AND a.outcome='UNKNOWN'
          )
          AND EXISTS (
            SELECT 1 FROM public.audit_events a WHERE a.resource_id=c.execution_id
              AND starts_with(a.action,'SYNTHETIC_SEND_MARKED:payments.issue_refund:refund-ledger.v2:')
          )
    )
    SELECT d.execution_id FROM (
        (SELECT e.execution_id,e.next_check_at AS due FROM eligible e
         WHERE e.lease_expires_at IS NULL AND e.next_check_at <= statement_timestamp()
         ORDER BY due,e.execution_id LIMIT least(greatest(batch,1),8))
        UNION ALL
        (SELECT e.execution_id,e.lease_expires_at AS due FROM eligible e
         WHERE e.lease_expires_at <= statement_timestamp()
         ORDER BY due,e.execution_id LIMIT least(greatest(batch,1),8))
    ) d ORDER BY d.due,d.execution_id LIMIT least(greatest(batch,1),8)
$$;

CREATE FUNCTION m914_refund.owned(target uuid, verifier uuid, fence bigint)
RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM m914_refund.claims o
        WHERE o.execution_id=target AND o.worker_id=verifier AND o.generation=fence
          AND o.login_name=session_user
    )
$$;

CREATE FUNCTION m914_refund.operate(
    action text, target uuid, verifier uuid, fence bigint, lease_ms integer,
    preimage bytea, verdict text, delay_ms bigint, reason text
) RETURNS TABLE (
    generation bigint, expires_at timestamptz, sender_worker uuid,
    sender_generation bigint, intent_binding bytea, state text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    gate text;
    cmd record;
    original record;
    marker text;
    marker_count integer;
    marker_prefix constant text := 'SYNTHETIC_SEND_MARKED:payments.issue_refund:refund-ledger.v2:';
    current_time_value timestamptz;
    manifest jsonb;
    amount numeric;
    effect bigint;
    target_state text;
    evidence_bytes bytea;
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='M9145', MESSAGE='verifier requires read committed';
    END IF;
    SELECT mode INTO gate FROM public.execution_dispatch_gate WHERE singleton FOR SHARE;
    PERFORM revision FROM public.execution_authorization_guard WHERE singleton FOR SHARE;
    IF gate IS NULL OR NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='M9142', MESSAGE='missing coordination';
    END IF;
    IF (SELECT array_agg(version_num) FROM public.alembic_version)
        IS DISTINCT FROM ARRAY['0011']::varchar[] THEN
        RAISE EXCEPTION USING ERRCODE='M9141', MESSAGE='incompatible schema';
    END IF;
    IF gate <> 'DURABLE' THEN
        RAISE EXCEPTION USING ERRCODE='M9143', MESSAGE='durable verification disabled';
    END IF;
    IF action IS NULL OR action NOT IN ('claim','finish') OR verifier IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid verification operation';
    END IF;
    PERFORM id FROM public.executions WHERE id=target AND status='UNKNOWN'
        AND tool_id='payments.issue_refund' FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT c.generation,c.lease_owner,c.lease_expires_at,c.next_check_at INTO cmd
        FROM public.execution_commands c WHERE c.execution_id=target AND c.state='UNKNOWN'
        AND c.tool_id='payments.issue_refund' AND NOT c.verification_only FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;
    IF action='finish' AND (NOT m914_refund.owned(target,verifier,fence)
        OR cmd.generation IS DISTINCT FROM fence OR cmd.lease_owner IS DISTINCT FROM verifier)
    THEN RETURN; END IF;
    SELECT a.worker_id,a.generation INTO original FROM public.execution_attempts a
        WHERE a.execution_id=target AND a.kind='SEND' AND a.send_marked_at IS NOT NULL
        AND a.outcome='UNKNOWN' FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;
    LOCK TABLE public.audit_events IN ROW EXCLUSIVE MODE;
    IF (SELECT count(*) FROM pg_trigger
        WHERE tgrelid='public.audit_events'::regclass AND tgenabled='A'
          AND tgfoid=to_regprocedure('public.protect_synthetic_dispatch_evidence()')
          AND ((tgname='protect_synthetic_consumption_rows' AND tgtype=31)
            OR (tgname='protect_synthetic_consumption_truncate' AND tgtype=34))) <> 2
    THEN
        RAISE EXCEPTION USING ERRCODE='M9146', MESSAGE='synthetic audit guards unavailable';
    END IF;
    SELECT count(*),min(a.action) FILTER (WHERE a.actor=original.worker_id::text)
      INTO marker_count,marker FROM public.audit_events a WHERE a.resource_id=target
      AND (a.action='SYNTHETIC_SEND_MARKED' OR starts_with(a.action,'SYNTHETIC_SEND_MARKED:'));
    IF marker_count<>1 OR marker IS NULL OR NOT starts_with(marker,marker_prefix)
        OR substr(marker,length(marker_prefix)+1) !~ '^[0-9a-f]{64}$'
    THEN RETURN; END IF;
    intent_binding := decode(substr(marker,length(marker_prefix)+1),'hex');
    current_time_value := clock_timestamp();

    IF action='claim' THEN
        IF lease_ms IS NULL OR lease_ms NOT BETWEEN 1 AND 300000 THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid verifier lease duration';
        END IF;
        IF cmd.next_check_at > current_time_value OR cmd.lease_expires_at > current_time_value
          OR (cmd.next_check_at IS NULL AND cmd.lease_expires_at IS NULL)
          OR cmd.generation < original.generation THEN RETURN; END IF;
        IF cmd.lease_expires_at IS NOT NULL THEN
            UPDATE public.execution_attempts a SET outcome='UNKNOWN',finished_at=current_time_value
              WHERE a.execution_id=target AND a.generation=cmd.generation AND a.worker_id=cmd.lease_owner
                AND a.kind='VERIFY' AND a.outcome IS NULL;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE='M9144', MESSAGE='invalid expired verifier';
            END IF;
            INSERT INTO public.audit_events(actor,action,resource_id)
              VALUES (cmd.lease_owner::text,'SYNTHETIC_VERIFY_LEASE_EXPIRED',target);
        END IF;
        generation := cmd.generation+1;
        expires_at := current_time_value+lease_ms*interval '1 millisecond';
        sender_worker := original.worker_id;
        sender_generation := original.generation;
        state := 'UNKNOWN';
        UPDATE public.execution_commands SET generation=cmd.generation+1,lease_owner=verifier,
            lease_expires_at=expires_at,next_check_at=NULL WHERE execution_id=target;
        INSERT INTO public.execution_attempts(execution_id,generation,worker_id,kind,claimed_at)
          VALUES (target,cmd.generation+1,verifier,'VERIFY',current_time_value);
        INSERT INTO m914_refund.claims AS o VALUES (target,cmd.generation+1,verifier,session_user)
          ON CONFLICT (execution_id) DO UPDATE
            SET generation=excluded.generation,worker_id=excluded.worker_id,login_name=excluded.login_name;
        INSERT INTO public.audit_events(actor,action,resource_id)
          VALUES (verifier::text,'SYNTHETIC_VERIFY_CLAIMED',target);
        RETURN NEXT;
        RETURN;
    END IF;

    IF cmd.lease_expires_at IS NULL OR cmd.lease_expires_at <= current_time_value
      OR fence <= original.generation THEN RETURN; END IF;
    IF verdict IS NULL THEN
        IF reason IS NULL OR reason NOT IN ('NO_EVIDENCE','UNAVAILABLE','MISMATCH')
          OR delay_ms IS NULL OR delay_ms NOT BETWEEN 1 AND 300000
        THEN RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid verification deferral'; END IF;
        target_state := 'UNKNOWN';
    ELSE
        -- The caller supplies the canonical preimage of the immutable SEND
        -- marker. Derive the accepted amount from it, then check the actual
        -- committed ledger row instead of trusting a caller-supplied verdict.
        IF verdict NOT IN ('EFFECT_COMMITTED','DEFINITIVE_NO_EFFECT')
          OR octet_length(preimage)>65536 OR sha256(preimage) IS DISTINCT FROM intent_binding
          OR (SELECT array_agg(a.actor) FROM public.audit_events a WHERE a.resource_id=target
                AND a.action='SYNTHETIC_INVOCATION_CONSUMED')
             IS DISTINCT FROM ARRAY[original.worker_id::text]
        THEN RAISE EXCEPTION USING ERRCODE='M9147', MESSAGE='refund binding mismatch'; END IF;
        manifest := convert_from(preimage,'UTF8')::jsonb;
        amount := (manifest #>> '{3,amount_minor}')::numeric;
        SELECT o.refunded_minor INTO effect FROM synthetic_refund.outcomes o
            WHERE o.operation_id=target AND o.binding=intent_binding;
        IF NOT FOUND OR amount IS NULL OR amount<=0
          OR (verdict='EFFECT_COMMITTED' AND effect<>amount)
          OR (verdict='DEFINITIVE_NO_EFFECT' AND effect<>0)
        THEN RAISE EXCEPTION USING ERRCODE='M9147', MESSAGE='refund ledger mismatch'; END IF;
        target_state := CASE WHEN verdict='EFFECT_COMMITTED' THEN 'SUCCEEDED' ELSE 'FAILED' END;
        -- Same byte encoding as synthetic_evidence_bytes for refund v2.
        evidence_bytes := convert_to(format(
          '["synthetic-outcome.v2",{"operation_id":"%s","owner":{"execution_id":"%s","generation":%s,"worker_id":"%s"},"result":"%s"},"%s"]',
          target,target,original.generation,original.worker_id,verdict,encode(intent_binding,'hex')
        ),'UTF8');
    END IF;
    -- Recheck wall time after validating preimage and ledger evidence.
    current_time_value := clock_timestamp();
    IF cmd.lease_expires_at <= current_time_value THEN RETURN; END IF;
    UPDATE public.execution_attempts a SET outcome=target_state,finished_at=current_time_value
        WHERE a.execution_id=target AND a.generation=fence AND a.worker_id=verifier
          AND a.kind='VERIFY' AND a.outcome IS NULL;
    IF NOT FOUND THEN RETURN; END IF;
    UPDATE public.execution_commands SET state=target_state,lease_owner=NULL,lease_expires_at=NULL,
        next_check_at=CASE WHEN target_state='UNKNOWN'
            THEN current_time_value+delay_ms*interval '1 millisecond' END WHERE execution_id=target;
    IF target_state='UNKNOWN' THEN
        INSERT INTO public.audit_events(actor,action,resource_id)
            VALUES (verifier::text,'SYNTHETIC_VERIFY_DEFERRED_'||reason,target);
    ELSE
        INSERT INTO public.execution_receipts(execution_id,generation,outcome,evidence_source,
            external_reference,evidence_digest,recorded_at)
          VALUES (target,fence,target_state,'LOOKUP','synthetic:'||target,
                  sha256(evidence_bytes),current_time_value);
        UPDATE public.executions SET status=target_state,finished_at=current_time_value,
            error_code=CASE WHEN target_state='FAILED' THEN 'SYNTHETIC_DEFINITIVE_NO_EFFECT' END
          WHERE id=target;
        INSERT INTO public.audit_events(actor,action,resource_id)
          VALUES (verifier::text,'SYNTHETIC_VERIFY_'||target_state,target);
    END IF;
    state := target_state;
    RETURN NEXT;
END
$$;

CREATE FUNCTION m914_refund.intent(target uuid, verifier uuid, fence bigint) RETURNS TABLE (
    command jsonb, reservation jsonb, approval jsonb, execution jsonb, contract jsonb
) LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT to_jsonb(c),to_jsonb(k),to_jsonb(a),to_jsonb(e),v.contract
    FROM (
        SELECT execution_id,workspace_id,requester,customer_id,tool_id,tool_version,contract_digest,
            ciphertext,wrapped_data_key,nonce,key_version
        FROM public.execution_commands WHERE execution_id=target AND tool_id='payments.issue_refund'
          AND state='UNKNOWN' AND NOT verification_only AND generation=fence
          AND lease_owner=verifier AND lease_expires_at>statement_timestamp()
    ) c
    JOIN (SELECT execution_id,workspace_id,requester,customer_id,tool_id,tool_version,key_digest,
            intent_digest,digest_key_version,canonicalizer_version FROM public.execution_request_keys
          WHERE execution_id=target) k USING (execution_id)
    JOIN (SELECT id,arguments,request_key,intent_hash FROM public.approvals
          WHERE id=target AND status='APPROVED') a ON a.id=c.execution_id
    JOIN (SELECT id,principal,tool_id,version FROM public.executions
          WHERE id=target AND status='UNKNOWN' AND tool_id='payments.issue_refund') e ON e.id=c.execution_id
    JOIN public.tool_versions v ON v.tool_id=c.tool_id AND v.version=c.tool_version
    WHERE m914_refund.owned(target,verifier,fence)
$$;

CREATE FUNCTION m914_refund.receipt(target uuid, verifier uuid, fence bigint)
RETURNS TABLE (state text,evidence_digest bytea)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT c.state,r.evidence_digest FROM public.execution_receipts r
    JOIN public.execution_commands c USING (execution_id)
    JOIN public.executions e ON e.id=r.execution_id
    JOIN public.execution_attempts a ON a.execution_id=r.execution_id AND a.generation=r.generation
    WHERE r.execution_id=target AND r.generation=fence AND r.evidence_source='LOOKUP'
      AND r.external_reference='synthetic:'||target AND r.outcome=c.state AND e.status=c.state
      AND c.tool_id='payments.issue_refund' AND e.tool_id=c.tool_id AND c.generation=fence
      AND a.kind='VERIFY' AND a.worker_id=verifier AND a.outcome=r.outcome
      AND m914_refund.owned(target,verifier,fence)
$$;

ALTER FUNCTION m914_refund.owned(uuid,uuid,bigint) OWNER TO m914_refund_function_owner;
ALTER FUNCTION m914_refund.discover(integer) OWNER TO m914_refund_function_owner;
ALTER FUNCTION m914_refund.operate(text,uuid,uuid,bigint,integer,bytea,text,bigint,text)
    OWNER TO m914_refund_function_owner;
ALTER FUNCTION m914_refund.intent(uuid,uuid,bigint) OWNER TO m914_refund_function_owner;
ALTER FUNCTION m914_refund.receipt(uuid,uuid,bigint) OWNER TO m914_refund_function_owner;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA m914_refund FROM PUBLIC;
GRANT EXECUTE ON FUNCTION m914_refund.discover(integer),
    m914_refund.operate(text,uuid,uuid,bigint,integer,bytea,text,bigint,text),
    m914_refund.intent(uuid,uuid,bigint),m914_refund.receipt(uuid,uuid,bigint) TO m914_refund_verifier;
