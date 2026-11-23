-- Run atomically as the trusted provisioner of a disposable 0011 database.
CREATE ROLE m914_function_owner NOLOGIN;
CREATE ROLE m914_worker_access NOLOGIN;
CREATE SCHEMA m914_ownership AUTHORIZATION m914_function_owner;
REVOKE ALL ON SCHEMA m914_ownership FROM PUBLIC;
GRANT USAGE ON SCHEMA m914_ownership TO m914_worker_access;
GRANT USAGE ON SCHEMA public TO m914_function_owner, m914_worker_access;
GRANT SELECT (version_num) ON public.alembic_version TO m914_function_owner, m914_worker_access;

CREATE TABLE m914_ownership.sessions (
    login name NOT NULL,
    worker_id uuid PRIMARY KEY
);
ALTER TABLE m914_ownership.sessions OWNER TO m914_function_owner;

-- Row locking requires UPDATE on at least one column. The non-login owner
-- receives the minimum columns needed for the fixed SQL below.
GRANT SELECT (singleton, mode), UPDATE (singleton)
    ON public.execution_dispatch_gate TO m914_function_owner;
GRANT SELECT (singleton, revision), UPDATE (singleton)
    ON public.execution_authorization_guard TO m914_function_owner;
GRANT SELECT (id, status), UPDATE (status, error_code, finished_at)
    ON public.executions TO m914_function_owner;
GRANT SELECT (id, status), UPDATE (id) ON public.approvals TO m914_function_owner;
GRANT SELECT (execution_id, state, generation, scheduled_at, dispatch_not_after,
              lease_owner, lease_expires_at, verification_only),
      UPDATE (state, generation, lease_owner, lease_expires_at)
    ON public.execution_commands TO m914_function_owner;
GRANT SELECT (execution_id, generation, worker_id, kind, send_marked_at, outcome),
      INSERT (execution_id, generation, worker_id, kind, claimed_at),
      UPDATE (finished_at, outcome)
    ON public.execution_attempts TO m914_function_owner;

CREATE FUNCTION m914_ownership.register_worker() RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    identity uuid := gen_random_uuid();
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='M9145', MESSAGE='worker requires read committed';
    END IF;
    IF (SELECT array_agg(version_num) FROM public.alembic_version)
        IS DISTINCT FROM ARRAY['0011']::varchar[] THEN
        RAISE EXCEPTION USING ERRCODE='M9141', MESSAGE='incompatible worker schema';
    END IF;
    INSERT INTO m914_ownership.sessions VALUES (session_user, identity);
    RETURN identity;
END;
$$;

-- The entry point derives targets and transitions in SQL. It accepts no caller
-- supplied state, SQL fragment, or relation name.
CREATE FUNCTION m914_ownership.operate(
    operation text, worker uuid, target_id uuid, expected_generation bigint, lease_ms integer
) RETURNS TABLE(execution_id uuid, generation bigint, expires_at timestamptz, state text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    mode_value text;
    selected_id uuid;
    command_row record;
    approval_status text;
    current_time_value timestamptz;
    end_time timestamptz;
    next_state text;
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='M9145', MESSAGE='worker requires read committed';
    END IF;

    SELECT mode INTO mode_value FROM public.execution_dispatch_gate
        WHERE singleton FOR SHARE;
    PERFORM revision FROM public.execution_authorization_guard WHERE singleton FOR SHARE;
    IF mode_value IS NULL OR NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='M9142', MESSAGE='missing execution coordination';
    END IF;
    -- One slot per registration. Different processes may register separately
    -- under the same login. Serialize renewals as well as claims so a renewal
    -- cannot revive a slot concurrently with a second claim.
    PERFORM 1 FROM m914_ownership.sessions s
        WHERE s.login=session_user AND s.worker_id=worker FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='unrecognized worker identity';
    END IF;
    IF (SELECT array_agg(version_num) FROM public.alembic_version)
        IS DISTINCT FROM ARRAY['0011']::varchar[] THEN
        RAISE EXCEPTION USING ERRCODE='M9141', MESSAGE='incompatible worker schema';
    END IF;
    IF mode_value <> 'DURABLE' THEN
        RAISE EXCEPTION USING ERRCODE='M9143', MESSAGE='durable ownership disabled';
    END IF;
    IF operation='claim' AND EXISTS (
        SELECT 1 FROM public.execution_commands c WHERE c.lease_owner=worker
          AND c.lease_expires_at > clock_timestamp()
    ) THEN
        RETURN;
    END IF;

    IF operation IN ('claim','recover') THEN
        SELECT e.id INTO selected_id
        FROM public.execution_commands c JOIN public.executions e ON e.id=c.execution_id
        WHERE NOT c.verification_only
          AND NOT EXISTS (SELECT 1 FROM public.execution_attempts a
                          WHERE a.execution_id=c.execution_id AND a.send_marked_at IS NOT NULL)
          AND ((operation='claim' AND c.state='READY'
                AND c.scheduled_at <= statement_timestamp()
                AND c.dispatch_not_after > statement_timestamp())
            OR (operation='recover' AND
                ((c.state='CLAIMED' AND c.lease_expires_at <= statement_timestamp())
                 OR (c.state='READY' AND c.dispatch_not_after <= statement_timestamp()))))
        ORDER BY CASE WHEN operation='claim' THEN c.scheduled_at
                      WHEN c.state='CLAIMED' THEN c.lease_expires_at
                      ELSE c.dispatch_not_after END, e.id
        LIMIT 1 FOR UPDATE OF e SKIP LOCKED;
    ELSIF operation IN ('renew','release') THEN
        SELECT e.id INTO selected_id
          FROM public.executions e JOIN public.execution_commands c ON c.execution_id=e.id
          WHERE e.id=target_id AND c.state='CLAIMED' AND c.lease_owner=worker
            AND c.generation=expected_generation FOR UPDATE OF e;
    ELSE
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid ownership operation';
    END IF;
    IF selected_id IS NULL THEN RETURN; END IF;

    SELECT a.status INTO approval_status FROM public.approvals a
        WHERE a.id=selected_id FOR UPDATE;
    SELECT c.state, c.generation, c.scheduled_at, c.dispatch_not_after,
           c.lease_owner, c.lease_expires_at, c.verification_only
    INTO command_row FROM public.execution_commands c
        WHERE c.execution_id=selected_id FOR UPDATE;
    IF NOT FOUND OR approval_status IS DISTINCT FROM 'APPROVED'
       OR command_row.verification_only OR command_row.state NOT IN ('READY','CLAIMED')
       OR EXISTS (SELECT 1 FROM public.execution_attempts a
                  WHERE a.execution_id=selected_id AND a.send_marked_at IS NOT NULL) THEN
        RETURN;
    END IF;
    current_time_value := clock_timestamp();

    IF operation IN ('claim','renew') AND (lease_ms IS NULL OR lease_ms NOT BETWEEN 1 AND 300000) THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid lease duration';
    END IF;

    IF operation='claim' THEN
        IF command_row.state <> 'READY' OR command_row.lease_owner IS NOT NULL
           OR command_row.scheduled_at > current_time_value
           OR command_row.dispatch_not_after <= current_time_value THEN RETURN; END IF;
        end_time := least(current_time_value + lease_ms * interval '1 millisecond',
                          command_row.dispatch_not_after);
        UPDATE public.execution_commands c SET state='CLAIMED', generation=c.generation+1,
            lease_owner=worker, lease_expires_at=end_time WHERE c.execution_id=selected_id;
        INSERT INTO public.execution_attempts (execution_id,generation,worker_id,kind,claimed_at)
            VALUES (selected_id,command_row.generation+1,worker,'SEND',current_time_value);
        RETURN QUERY SELECT selected_id, command_row.generation+1, end_time, 'CLAIMED'::text;
        RETURN;
    END IF;

    IF operation IN ('renew','release') THEN
        IF command_row.state <> 'CLAIMED' OR command_row.lease_owner IS DISTINCT FROM worker
           OR command_row.generation IS DISTINCT FROM expected_generation
           OR command_row.lease_expires_at <= current_time_value THEN RETURN; END IF;
        IF operation='renew' THEN
            IF command_row.dispatch_not_after <= current_time_value THEN RETURN; END IF;
            end_time := least(greatest(current_time_value + lease_ms * interval '1 millisecond',
                                      command_row.lease_expires_at), command_row.dispatch_not_after);
            UPDATE public.execution_commands c SET lease_expires_at=end_time
                WHERE c.execution_id=selected_id;
            RETURN QUERY SELECT selected_id, command_row.generation, end_time, 'CLAIMED'::text;
            RETURN;
        END IF;
    ELSIF (command_row.state='CLAIMED' AND command_row.lease_expires_at > current_time_value)
       OR (command_row.state='READY' AND command_row.dispatch_not_after > current_time_value) THEN
        RETURN;
    END IF;

    IF command_row.state='CLAIMED' THEN
        UPDATE public.execution_attempts a SET finished_at=current_time_value, outcome='NOT_SENT'
        WHERE a.execution_id=selected_id AND a.generation=command_row.generation
          AND a.worker_id=command_row.lease_owner AND a.kind='SEND'
          AND a.send_marked_at IS NULL AND a.outcome IS NULL;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='M9144', MESSAGE='ownership evidence conflict';
        END IF;
    END IF;
    next_state := CASE WHEN command_row.dispatch_not_after <= current_time_value
                       THEN 'FAILED' ELSE 'READY' END;
    UPDATE public.execution_commands c SET state=next_state, lease_owner=NULL, lease_expires_at=NULL
        WHERE c.execution_id=selected_id;
    IF next_state='FAILED' THEN
        UPDATE public.executions e SET status='FAILED', error_code='DISPATCH_DEADLINE_EXCEEDED',
            finished_at=current_time_value WHERE e.id=selected_id AND e.status='RUNNING';
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='M9144', MESSAGE='ownership projection conflict';
        END IF;
    END IF;
    RETURN QUERY SELECT selected_id, command_row.generation, NULL::timestamptz, next_state;
END;
$$;

ALTER FUNCTION m914_ownership.register_worker() OWNER TO m914_function_owner;
ALTER FUNCTION m914_ownership.operate(text,uuid,uuid,bigint,integer) OWNER TO m914_function_owner;
REVOKE ALL ON FUNCTION m914_ownership.register_worker() FROM PUBLIC;
REVOKE ALL ON FUNCTION m914_ownership.operate(text,uuid,uuid,bigint,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION m914_ownership.register_worker() TO m914_worker_access;
GRANT EXECUTE ON FUNCTION m914_ownership.operate(text,uuid,uuid,bigint,integer) TO m914_worker_access;
