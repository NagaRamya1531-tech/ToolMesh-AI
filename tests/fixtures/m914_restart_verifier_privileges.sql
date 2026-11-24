-- Install after the disposable restart ledger and shared verifier fixture.
-- Verifier logins can read restart outcomes. They cannot write ledger rows,
-- obtain SEND authorization, or use the refund or operator endpoints.
GRANT USAGE ON SCHEMA m914_refund TO m914_restart_verifier, m914_restart_operator;
GRANT USAGE ON SCHEMA synthetic_restart TO m914_restart_verifier, m914_refund_function_owner;
GRANT SELECT (operation_id,binding,workspace_id,customer_id,service_id,sender_worker,
    sender_generation,prior_boot_generation,boot_generation) ON synthetic_restart.outcomes
    TO m914_restart_verifier, m914_refund_function_owner;
GRANT EXECUTE ON FUNCTION m914_refund.discover(integer,text),
    m914_refund.operate(text,uuid,uuid,bigint,integer,bytea,text,text,text,bigint),
    m914_refund.intent(uuid,uuid,bigint,text),m914_refund.receipt(uuid,uuid,bigint,text)
    TO m914_restart_verifier;
GRANT EXECUTE ON FUNCTION m914_refund.attention(uuid,text),m914_refund.resume(uuid,bigint,text,text)
    TO m914_restart_operator;
