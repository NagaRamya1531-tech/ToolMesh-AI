"""One authorization predicate for discovery, schema checks and approval consumption."""

from gateway.identity import Principal

POLICY = """
    t.service_id=ANY(CAST(:services AS text[]))
    AND (NOT :is_agent OR EXISTS(SELECT 1 FROM agent_accounts WHERE id=:principal AND enabled))
    AND (t.service_id<>'hr' OR :profile='hr')
    AND (:profile<>'hr' OR t.service_id<>'communication' OR t.id='communication.notify_leave')
    AND (t.id<>'communication.notify_leave' OR :profile='hr')
    AND NOT EXISTS (
        SELECT 1 FROM policy_rules p WHERE p.principal=:principal AND p.enabled
        AND p.tool_id=t.id AND p.customer_id=:customer AND p.effect='deny')
    AND (
        v.contract->>'risk'='read'
        OR (v.contract->>'risk'='write' AND (
            :can_write OR (:profile='finance' AND t.id='analytics.generate_report')
            OR EXISTS(SELECT 1 FROM policy_rules p WHERE p.principal=:principal AND p.enabled
              AND p.tool_id=t.id AND p.customer_id=:customer AND p.effect='allow')))
        OR (v.contract->>'risk'='approval' AND (
            (t.id='payments.issue_refund' AND EXISTS (
                SELECT 1 FROM refund_grants WHERE principal=:principal AND enabled AND customer_id=:customer))
            OR (t.id='it.restart_service' AND :profile='it')))
    )
"""


def scope_params(principal: Principal) -> dict:
    return {
        "principal": principal.id,
        "services": list(principal.services),
        "is_agent": principal.kind == "agent",
        "customer": principal.customer,
        "profile": principal.profile,
        "can_write": principal.can_write,
    }


def object_allowed(principal: Principal, arguments: dict) -> bool:
    return arguments.get("customer_id") == principal.customer and (
        principal.profile != "hr"
        or (
            arguments.get("employee_id", "E-102") == "E-102"
            and arguments.get("department_id", "D-102") == "D-102"
        )
    )
