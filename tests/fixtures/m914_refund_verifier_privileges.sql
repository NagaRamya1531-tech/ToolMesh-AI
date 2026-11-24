-- Install atomically as the trusted provisioner of a disposable 0011 database.
CREATE ROLE m914_refund_verifier NOLOGIN;
CREATE ROLE m914_refund_operator NOLOGIN;
CREATE ROLE m914_restart_verifier NOLOGIN;
CREATE ROLE m914_restart_operator NOLOGIN;
CREATE ROLE m914_refund_function_owner NOLOGIN;
CREATE SCHEMA m914_refund AUTHORIZATION m914_refund_function_owner;
REVOKE ALL ON SCHEMA m914_refund FROM PUBLIC;
GRANT USAGE ON SCHEMA m914_refund TO m914_refund_verifier, m914_refund_operator;
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
GRANT SELECT (execution_id, generation, worker_id, kind, send_marked_at, outcome, finished_at),
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

-- The trigger runs as the writer, including inside the scoped refund functions.
-- Inherited INSERT privileges do not make a login the function owner.
CREATE FUNCTION m914_refund.protect_operator_audit() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    reserved constant text := '^SYNTHETIC_(VERIFY_(ATTENTION|RESUMED)|RESTART_VERIFY_)';
BEGIN
    IF TG_OP='TRUNCATE' THEN
        IF EXISTS (SELECT 1 FROM public.audit_events WHERE action ~ reserved) THEN
            RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='refund operator audit events are protected';
        END IF;
        RETURN NULL;
    END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.action ~ reserved AND current_user<>'m914_refund_function_owner' THEN
            RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='refund operator audit action is reserved';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.action ~ reserved THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='refund operator audit events are protected';
    END IF;
    IF TG_OP='UPDATE' THEN
        IF NEW.action ~ reserved THEN
            RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='refund operator audit events are protected';
        END IF;
        RETURN NEW;
    END IF;
    RETURN OLD;
END
$$;

ALTER FUNCTION m914_refund.protect_operator_audit() OWNER TO m914_refund_function_owner;
CREATE TRIGGER protect_synthetic_refund_operator_rows
    BEFORE INSERT OR UPDATE OR DELETE ON public.audit_events FOR EACH ROW
    EXECUTE FUNCTION m914_refund.protect_operator_audit();
CREATE TRIGGER protect_synthetic_refund_operator_truncate
    BEFORE TRUNCATE ON public.audit_events FOR EACH STATEMENT
    EXECUTE FUNCTION m914_refund.protect_operator_audit();
ALTER TABLE public.audit_events ENABLE ALWAYS TRIGGER protect_synthetic_refund_operator_rows;
ALTER TABLE public.audit_events ENABLE ALWAYS TRIGGER protect_synthetic_refund_operator_truncate;

CREATE TABLE m914_refund.claims (
    execution_id uuid PRIMARY KEY, generation bigint NOT NULL, worker_id uuid NOT NULL,
    login_name name NOT NULL
);
ALTER TABLE m914_refund.claims OWNER TO m914_refund_function_owner;

-- The provisioner owns defaults. Each window snapshots the limits so a reload
-- or provisioning change cannot replenish an existing verification budget.
CREATE TABLE m914_refund.retry_policy (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 30),
    elapsed_ms integer NOT NULL CHECK (elapsed_ms BETWEEN 1 AND 86400000),
    initial_ms integer NOT NULL CHECK (initial_ms BETWEEN 1 AND 300000),
    max_delay_ms integer NOT NULL CHECK (max_delay_ms BETWEEN initial_ms AND 300000),
    jitter_percent integer NOT NULL CHECK (jitter_percent BETWEEN 0 AND 50),
    lookup_ms integer NOT NULL CHECK (lookup_ms BETWEEN 1 AND 30000)
);
INSERT INTO m914_refund.retry_policy VALUES (true,6,300000,1000,30000,0,3000);
GRANT SELECT ON m914_refund.retry_policy TO m914_refund_function_owner;

CREATE TABLE m914_refund.windows (
    execution_id uuid PRIMARY KEY REFERENCES public.execution_commands(execution_id),
    window_number bigint NOT NULL CHECK (window_number>0),
    base_generation bigint NOT NULL CHECK (base_generation>0),
    deadline timestamptz NOT NULL,
    max_attempts integer NOT NULL,
    initial_ms integer NOT NULL,
    max_delay_ms integer NOT NULL,
    jitter_percent integer NOT NULL,
    lookup_ms integer NOT NULL,
    attention_reason text CHECK (attention_reason IN ('ATTEMPTS','DEADLINE'))
);
ALTER TABLE m914_refund.windows OWNER TO m914_refund_function_owner;

CREATE FUNCTION m914_refund.backoff_ms(
    target uuid, fence bigint, used bigint, initial_ms integer, cap_ms integer, jitter integer
) RETURNS bigint LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog, pg_temp AS $$
    SELECT delay - delay*jitter*get_byte(sha256(uuid_send(target)||int8send(fence)),0)/25500
    FROM (SELECT least(cap_ms,initial_ms*(1::bigint << least(greatest(used-1,0),29)::integer)) AS delay) d
$$;

CREATE FUNCTION m914_refund.pause(target uuid, actor_name text, audit_prefix text, cause text)
RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    UPDATE m914_refund.windows SET attention_reason=cause WHERE execution_id=target;
    UPDATE public.execution_commands SET lease_owner=NULL,lease_expires_at=NULL,
        next_check_at=NULL WHERE execution_id=target;
    INSERT INTO public.audit_events(actor,action,resource_id)
        VALUES (actor_name,audit_prefix||'ATTENTION_'||cause,target);
END
$$;

-- Keep refund and restart authority distinct even though they share the
-- same fenced transaction and retry-window tables.
CREATE FUNCTION m914_refund.allowed(requested_tool text, is_operator boolean)
RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
    SELECT CASE requested_tool
        WHEN 'payments.issue_refund' THEN pg_has_role(session_user,
            CASE WHEN is_operator THEN 'm914_refund_operator' ELSE 'm914_refund_verifier' END,'USAGE')
        WHEN 'it.restart_service' THEN pg_has_role(session_user,
            CASE WHEN is_operator THEN 'm914_restart_operator' ELSE 'm914_restart_verifier' END,'USAGE')
        ELSE false END
$$;

CREATE FUNCTION m914_refund.marker_prefix(requested_tool text)
RETURNS text LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog, pg_temp AS $$
    SELECT CASE requested_tool
        WHEN 'payments.issue_refund' THEN 'SYNTHETIC_SEND_MARKED:payments.issue_refund:refund-ledger.v2:'
        WHEN 'it.restart_service' THEN 'SYNTHETIC_SEND_MARKED:it.restart_service:it-restart-ledger.v1:'
        END
$$;

-- Count every SEND marker, including other tool contracts. The only marker
-- must belong to the original sender and contain a canonical binding.
CREATE FUNCTION m914_refund.bound_marker(target uuid, sender uuid, prefix text)
RETURNS bytea LANGUAGE sql STABLE STRICT SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $$
    SELECT CASE WHEN count(*)=1 THEN decode(
        min(substr(a.action,length(prefix)+1)) FILTER (
            WHERE a.actor=sender::text AND starts_with(a.action,prefix)
              AND substr(a.action,length(prefix)+1) ~ '^[0-9a-f]{64}$'
        ),'hex') END
    FROM public.audit_events a WHERE a.resource_id=target
      AND (a.action='SYNTHETIC_SEND_MARKED' OR starts_with(a.action,'SYNTHETIC_SEND_MARKED:'))
$$;

-- Keep scheduled work and expired leases as separate indexed ranges. Discovery
-- is advisory. The claim rechecks state, time and the protected marker under lock.
CREATE FUNCTION m914_refund.discover(batch integer, requested_tool text DEFAULT 'payments.issue_refund') RETURNS TABLE (execution_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    WITH eligible AS NOT MATERIALIZED (
        SELECT c.execution_id,c.next_check_at,c.lease_expires_at
        FROM public.execution_commands c JOIN public.executions e ON e.id=c.execution_id
        WHERE c.state='UNKNOWN' AND e.status='UNKNOWN'
          AND c.tool_id=requested_tool AND e.tool_id=c.tool_id AND NOT c.verification_only
          AND m914_refund.allowed(requested_tool,false)
          AND NOT EXISTS (
            SELECT 1 FROM m914_refund.windows w WHERE w.execution_id=c.execution_id
              AND w.attention_reason IS NOT NULL
          )
          AND EXISTS (
            SELECT 1 FROM public.execution_attempts a
            WHERE a.execution_id=c.execution_id AND a.kind='SEND'
              AND a.send_marked_at IS NOT NULL AND a.outcome='UNKNOWN'
              AND CASE WHEN requested_tool='it.restart_service' THEN
                c.generation>=a.generation AND m914_refund.bound_marker(
                    c.execution_id,a.worker_id,m914_refund.marker_prefix(requested_tool)
                ) IS NOT NULL
              ELSE EXISTS (
                SELECT 1 FROM public.audit_events marker WHERE marker.resource_id=c.execution_id
                  AND starts_with(marker.action,m914_refund.marker_prefix(requested_tool))
              ) END
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
    preimage bytea, verdict text, reason text,
    requested_tool text DEFAULT 'payments.issue_refund', effect_version bigint DEFAULT NULL
) RETURNS TABLE (
    generation bigint, expires_at timestamptz, sender_worker uuid,
    sender_generation bigint, intent_binding bytea, state text, lookup_timeout_ms integer
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    gate text;
    cmd record;
    original record;
    marker_prefix constant text := m914_refund.marker_prefix(requested_tool);
    audit_prefix constant text := CASE WHEN requested_tool='it.restart_service'
        THEN 'SYNTHETIC_RESTART_VERIFY_' ELSE 'SYNTHETIC_VERIFY_' END;
    current_time_value timestamptz;
    manifest jsonb;
    amount numeric;
    effect bigint;
    restart_effect record;
    target_state text;
    evidence_bytes bytea;
    policy m914_refund.retry_policy%ROWTYPE;
    active_window m914_refund.windows%ROWTYPE;
    used bigint;
    next_due timestamptz;
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
    IF NOT m914_refund.allowed(requested_tool,action='resume') THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='synthetic verification role required';
    END IF;
    IF action='resume' THEN
        IF fence IS NULL OR fence<1 OR reason IS NULL OR length(btrim(reason)) NOT BETWEEN 1 AND 240
          OR reason ~ '[[:cntrl:]]' THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='expected window and nonblank reason required';
        END IF;
    ELSIF action IS NULL OR action NOT IN ('claim','finish') OR verifier IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid verification operation';
    END IF;
    PERFORM id FROM public.executions WHERE id=target AND status='UNKNOWN'
        AND tool_id=requested_tool FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT c.generation,c.lease_owner,c.lease_expires_at,c.next_check_at,c.workspace_id,c.customer_id INTO cmd
        FROM public.execution_commands c WHERE c.execution_id=target AND c.state='UNKNOWN'
        AND c.tool_id=requested_tool AND NOT c.verification_only FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;
    IF action='finish' AND (NOT m914_refund.owned(target,verifier,fence)
        OR cmd.generation IS DISTINCT FROM fence OR cmd.lease_owner IS DISTINCT FROM verifier)
    THEN RETURN; END IF;
    SELECT a.worker_id,a.generation,a.finished_at INTO original FROM public.execution_attempts a
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
    IF (SELECT count(*) FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
        WHERE t.tgrelid='public.audit_events'::regclass AND t.tgenabled='A'
          AND p.oid=to_regprocedure('m914_refund.protect_operator_audit()')
          AND p.proowner='m914_refund_function_owner'::regrole AND NOT p.prosecdef
          AND ((t.tgname='protect_synthetic_refund_operator_rows' AND t.tgtype=31)
            OR (t.tgname='protect_synthetic_refund_operator_truncate' AND t.tgtype=34))) <> 2
    THEN
        RAISE EXCEPTION USING ERRCODE='M9148', MESSAGE='refund operator audit guards unavailable';
    END IF;
    intent_binding := m914_refund.bound_marker(target,original.worker_id,marker_prefix);
    IF intent_binding IS NULL THEN RETURN; END IF;
    current_time_value := clock_timestamp();

    IF action='claim' THEN
        IF lease_ms IS NULL OR lease_ms NOT BETWEEN 1 AND 300000 THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid verifier lease duration';
        END IF;
        IF cmd.next_check_at > current_time_value OR cmd.lease_expires_at > current_time_value
          OR (cmd.next_check_at IS NULL AND cmd.lease_expires_at IS NULL)
          OR cmd.generation < original.generation THEN RETURN; END IF;
    END IF;

    SELECT w.* INTO active_window FROM m914_refund.windows w WHERE w.execution_id=target;
    IF action='resume' THEN
        IF active_window.window_number IS DISTINCT FROM fence OR active_window.attention_reason IS NULL
          OR cmd.lease_expires_at IS NOT NULL OR cmd.next_check_at IS NOT NULL
        THEN RETURN; END IF;
        SELECT p.* INTO STRICT policy FROM m914_refund.retry_policy p WHERE p.singleton;
        UPDATE m914_refund.windows SET window_number=fence+1,base_generation=cmd.generation,
            deadline=current_time_value+policy.elapsed_ms*interval '1 millisecond',
            max_attempts=policy.max_attempts,initial_ms=policy.initial_ms,
            max_delay_ms=policy.max_delay_ms,jitter_percent=policy.jitter_percent,
            lookup_ms=policy.lookup_ms,attention_reason=NULL WHERE execution_id=target;
        UPDATE public.execution_commands SET next_check_at=current_time_value WHERE execution_id=target;
        INSERT INTO public.audit_events(actor,action,resource_id)
          VALUES (session_user::text,
            format(audit_prefix||'RESUMED:%s:%s:%s',fence+1,cmd.generation,btrim(reason)),target);
        generation := fence+1;
        RETURN NEXT;
        RETURN;
    END IF;

    IF active_window.execution_id IS NULL THEN
        -- Initial window starts when the protected SEND was parked. This also
        -- counts any earlier Slice 1 or Slice 2 generations on the same command.
        IF action<>'claim' THEN RETURN; END IF;
        SELECT p.* INTO STRICT policy FROM m914_refund.retry_policy p WHERE p.singleton;
        INSERT INTO m914_refund.windows VALUES (target,1,original.generation,
            original.finished_at+policy.elapsed_ms*interval '1 millisecond',policy.max_attempts,
            policy.initial_ms,policy.max_delay_ms,policy.jitter_percent,policy.lookup_ms,NULL)
          RETURNING * INTO active_window;
    END IF;
    IF active_window.attention_reason IS NOT NULL THEN RETURN; END IF;
    used := cmd.generation-active_window.base_generation;

    IF action='claim' THEN
        IF cmd.lease_expires_at IS NOT NULL THEN
            UPDATE public.execution_attempts a SET outcome='UNKNOWN',finished_at=current_time_value
              WHERE a.execution_id=target AND a.generation=cmd.generation AND a.worker_id=cmd.lease_owner
                AND a.kind='VERIFY' AND a.outcome IS NULL;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE='M9144', MESSAGE='invalid expired verifier';
            END IF;
            INSERT INTO public.audit_events(actor,action,resource_id)
              VALUES (cmd.lease_owner::text,audit_prefix||'LEASE_EXPIRED',target);
        END IF;
        IF current_time_value>=active_window.deadline OR used>=active_window.max_attempts THEN
            PERFORM m914_refund.pause(target,verifier::text,audit_prefix,
              CASE WHEN current_time_value>=active_window.deadline THEN 'DEADLINE' ELSE 'ATTEMPTS' END);
            RETURN;
        END IF;
        IF cmd.lease_expires_at IS NOT NULL THEN
            next_due := least(active_window.deadline,cmd.lease_expires_at
              +m914_refund.backoff_ms(target,cmd.generation,used,active_window.initial_ms,
                active_window.max_delay_ms,active_window.jitter_percent)*interval '1 millisecond');
            IF next_due>current_time_value THEN
                UPDATE public.execution_commands SET lease_owner=NULL,lease_expires_at=NULL,
                    next_check_at=next_due WHERE execution_id=target;
                INSERT INTO public.audit_events(actor,action,resource_id)
                  VALUES (verifier::text,audit_prefix||'DEFERRED_LEASE_EXPIRED',target);
                RETURN;
            END IF;
        END IF;
        generation := cmd.generation+1;
        expires_at := least(active_window.deadline,current_time_value+lease_ms*interval '1 millisecond');
        lookup_timeout_ms := greatest(1,least(active_window.lookup_ms,
            floor(extract(epoch FROM expires_at-current_time_value)*1000)::integer));
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
          VALUES (verifier::text,audit_prefix||'CLAIMED',target);
        RETURN NEXT;
        RETURN;
    END IF;

    IF cmd.lease_expires_at IS NULL OR cmd.lease_expires_at <= current_time_value
      OR active_window.deadline<=current_time_value
      OR fence <= original.generation THEN RETURN; END IF;
    IF verdict IS NULL THEN
        IF reason IS NULL OR reason NOT IN ('NO_EVIDENCE','UNAVAILABLE','MISMATCH','TIMEOUT')
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
        IF requested_tool='it.restart_service' THEN
            SELECT o.prior_boot_generation,o.boot_generation INTO restart_effect
                FROM synthetic_restart.outcomes o
                WHERE o.operation_id=target AND o.binding=intent_binding
                  AND o.workspace_id=cmd.workspace_id AND o.customer_id=cmd.customer_id
                  AND o.service_id=manifest #>> '{3,service_id}'
                  AND o.sender_worker=original.worker_id AND o.sender_generation=original.generation;
            IF NOT FOUND OR manifest #>> '{3,customer_id}' IS DISTINCT FROM cmd.customer_id
              OR effect_version IS DISTINCT FROM restart_effect.boot_generation
              OR (verdict='EFFECT_COMMITTED' AND (
                  restart_effect.prior_boot_generation IS NULL OR restart_effect.boot_generation IS NULL
                  OR restart_effect.prior_boot_generation<0
                  OR restart_effect.boot_generation-restart_effect.prior_boot_generation<>1))
              OR (verdict='DEFINITIVE_NO_EFFECT' AND (
                  restart_effect.prior_boot_generation IS NOT NULL OR restart_effect.boot_generation IS NOT NULL))
            THEN RAISE EXCEPTION USING ERRCODE='M9147', MESSAGE='restart ledger mismatch'; END IF;
            evidence_bytes := convert_to(format(
              '["synthetic-outcome.v3","it-restart-ledger.v1",{"operation_id":"%s","owner":{"execution_id":"%s","generation":%s,"worker_id":"%s"},"result":"%s"},"%s",%s]',
              target,target,original.generation,original.worker_id,verdict,
              encode(intent_binding,'hex'),coalesce(effect_version::text,'null')
            ),'UTF8');
        ELSE
            amount := (manifest #>> '{3,amount_minor}')::numeric;
            SELECT o.refunded_minor INTO effect FROM synthetic_refund.outcomes o
                WHERE o.operation_id=target AND o.binding=intent_binding;
            IF NOT FOUND OR amount IS NULL OR amount<=0 OR effect_version IS NOT NULL
              OR (verdict='EFFECT_COMMITTED' AND effect<>amount)
              OR (verdict='DEFINITIVE_NO_EFFECT' AND effect<>0)
            THEN RAISE EXCEPTION USING ERRCODE='M9147', MESSAGE='refund ledger mismatch'; END IF;
            evidence_bytes := convert_to(format(
              '["synthetic-outcome.v2",{"operation_id":"%s","owner":{"execution_id":"%s","generation":%s,"worker_id":"%s"},"result":"%s"},"%s"]',
              target,target,original.generation,original.worker_id,verdict,encode(intent_binding,'hex')
            ),'UTF8');
        END IF;
        target_state := CASE WHEN verdict='EFFECT_COMMITTED' THEN 'SUCCEEDED' ELSE 'FAILED' END;
    END IF;
    -- Recheck wall time after validating preimage and ledger evidence.
    current_time_value := clock_timestamp();
    IF cmd.lease_expires_at <= current_time_value OR active_window.deadline<=current_time_value
    THEN RETURN; END IF;
    UPDATE public.execution_attempts a SET outcome=target_state,finished_at=current_time_value
        WHERE a.execution_id=target AND a.generation=fence AND a.worker_id=verifier
          AND a.kind='VERIFY' AND a.outcome IS NULL;
    IF NOT FOUND THEN RETURN; END IF;
    IF target_state='UNKNOWN' THEN
        INSERT INTO public.audit_events(actor,action,resource_id)
            VALUES (verifier::text,audit_prefix||'DEFERRED_'||reason,target);
        IF used>=active_window.max_attempts THEN
            PERFORM m914_refund.pause(target,verifier::text,audit_prefix,'ATTEMPTS');
        ELSE
            next_due := least(active_window.deadline,current_time_value+
                m914_refund.backoff_ms(target,fence,used,active_window.initial_ms,
                    active_window.max_delay_ms,active_window.jitter_percent)*interval '1 millisecond');
            UPDATE public.execution_commands SET lease_owner=NULL,lease_expires_at=NULL,
                next_check_at=next_due WHERE execution_id=target;
        END IF;
    ELSE
        UPDATE public.execution_commands SET state=target_state,lease_owner=NULL,lease_expires_at=NULL,
            next_check_at=NULL WHERE execution_id=target;
        INSERT INTO public.execution_receipts(execution_id,generation,outcome,evidence_source,
            external_reference,evidence_digest,recorded_at)
          VALUES (target,fence,target_state,'LOOKUP','synthetic:'||target,
                  sha256(evidence_bytes),current_time_value);
        UPDATE public.executions SET status=target_state,finished_at=current_time_value,
            error_code=CASE WHEN target_state='FAILED' THEN 'SYNTHETIC_DEFINITIVE_NO_EFFECT' END
          WHERE id=target;
        INSERT INTO public.audit_events(actor,action,resource_id)
          VALUES (verifier::text,audit_prefix||target_state,target);
    END IF;
    state := target_state;
    RETURN NEXT;
END
$$;

CREATE FUNCTION m914_refund.intent(target uuid, verifier uuid, fence bigint, requested_tool text DEFAULT 'payments.issue_refund') RETURNS TABLE (
    command jsonb, reservation jsonb, approval jsonb, execution jsonb, contract jsonb
) LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT to_jsonb(c),to_jsonb(k),to_jsonb(a),to_jsonb(e),v.contract
    FROM (
        SELECT execution_id,workspace_id,requester,customer_id,tool_id,tool_version,contract_digest,
            ciphertext,wrapped_data_key,nonce,key_version
        FROM public.execution_commands WHERE execution_id=target AND tool_id=requested_tool
          AND state='UNKNOWN' AND NOT verification_only AND generation=fence
          AND lease_owner=verifier AND lease_expires_at>statement_timestamp()
    ) c
    JOIN (SELECT execution_id,workspace_id,requester,customer_id,tool_id,tool_version,key_digest,
            intent_digest,digest_key_version,canonicalizer_version FROM public.execution_request_keys
          WHERE execution_id=target) k USING (execution_id)
    JOIN (SELECT id,arguments,request_key,intent_hash FROM public.approvals
          WHERE id=target AND status='APPROVED') a ON a.id=c.execution_id
    JOIN (SELECT id,principal,tool_id,version FROM public.executions
          WHERE id=target AND status='UNKNOWN' AND tool_id=requested_tool) e ON e.id=c.execution_id
    JOIN public.tool_versions v ON v.tool_id=c.tool_id AND v.version=c.tool_version
    WHERE m914_refund.allowed(requested_tool,false) AND m914_refund.owned(target,verifier,fence)
$$;

CREATE FUNCTION m914_refund.receipt(target uuid, verifier uuid, fence bigint, requested_tool text DEFAULT 'payments.issue_refund')
RETURNS TABLE (state text,evidence_digest bytea)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT c.state,r.evidence_digest FROM public.execution_receipts r
    JOIN public.execution_commands c USING (execution_id)
    JOIN public.executions e ON e.id=r.execution_id
    JOIN public.execution_attempts a ON a.execution_id=r.execution_id AND a.generation=r.generation
    WHERE r.execution_id=target AND r.generation=fence AND r.evidence_source='LOOKUP'
      AND r.external_reference='synthetic:'||target AND r.outcome=c.state AND e.status=c.state
      AND c.tool_id=requested_tool AND e.tool_id=c.tool_id AND c.generation=fence
      AND m914_refund.allowed(requested_tool,false)
      AND a.kind='VERIFY' AND a.worker_id=verifier AND a.outcome=r.outcome
      AND m914_refund.owned(target,verifier,fence)
$$;

CREATE FUNCTION m914_refund.attention(target uuid, requested_tool text DEFAULT 'payments.issue_refund')
RETURNS TABLE(window_number bigint,attempts_used bigint,deadline timestamptz,reason text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT w.window_number,c.generation-w.base_generation,w.deadline,w.attention_reason
    FROM m914_refund.windows w JOIN public.execution_commands c USING (execution_id)
    WHERE w.execution_id=target AND w.attention_reason IS NOT NULL
      AND c.tool_id=requested_tool AND c.state='UNKNOWN'
      AND m914_refund.allowed(requested_tool,true)
$$;

CREATE FUNCTION m914_refund.resume(target uuid, expected_window bigint, operator_reason text, requested_tool text DEFAULT 'payments.issue_refund')
RETURNS TABLE(window_number bigint)
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT r.generation FROM m914_refund.operate(
        'resume',target,NULL,expected_window,NULL,NULL,NULL,operator_reason,requested_tool
    ) r
$$;

ALTER FUNCTION m914_refund.allowed(text,boolean) OWNER TO m914_refund_function_owner;
ALTER FUNCTION m914_refund.marker_prefix(text) OWNER TO m914_refund_function_owner;
ALTER FUNCTION m914_refund.bound_marker(uuid,uuid,text) OWNER TO m914_refund_function_owner;
ALTER FUNCTION m914_refund.backoff_ms(uuid,bigint,bigint,integer,integer,integer)
    OWNER TO m914_refund_function_owner;
ALTER FUNCTION m914_refund.pause(uuid,text,text,text) OWNER TO m914_refund_function_owner;
ALTER FUNCTION m914_refund.attention(uuid,text) OWNER TO m914_refund_function_owner;
ALTER FUNCTION m914_refund.resume(uuid,bigint,text,text) OWNER TO m914_refund_function_owner;
ALTER FUNCTION m914_refund.owned(uuid,uuid,bigint) OWNER TO m914_refund_function_owner;
ALTER FUNCTION m914_refund.discover(integer,text) OWNER TO m914_refund_function_owner;
ALTER FUNCTION m914_refund.operate(text,uuid,uuid,bigint,integer,bytea,text,text,text,bigint)
    OWNER TO m914_refund_function_owner;
ALTER FUNCTION m914_refund.intent(uuid,uuid,bigint,text) OWNER TO m914_refund_function_owner;
ALTER FUNCTION m914_refund.receipt(uuid,uuid,bigint,text) OWNER TO m914_refund_function_owner;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA m914_refund FROM PUBLIC;
GRANT EXECUTE ON FUNCTION m914_refund.discover(integer,text),
    m914_refund.operate(text,uuid,uuid,bigint,integer,bytea,text,text,text,bigint),
    m914_refund.intent(uuid,uuid,bigint,text),m914_refund.receipt(uuid,uuid,bigint,text) TO m914_refund_verifier;
GRANT EXECUTE ON FUNCTION m914_refund.attention(uuid,text),m914_refund.resume(uuid,bigint,text,text)
    TO m914_refund_operator;
