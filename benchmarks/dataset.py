"""Versioned synthetic retrieval corpus. Query text is authored separately from documents."""

import hashlib
import json
import random
from dataclasses import dataclass

DATASET = "retrieval-sandbox-v1"
ORGANIZATIONS = ("Alder", "Birch", "Cedar", "Dune", "Elm", "Fir", "Grove", "Harbor")
REGIONS = ("North", "East", "Central", "West", "Coastal", "Inland", "South")
SCENARIOS = (
    (
        "payments",
        "detect duplicate capture",
        "Identify multiple settled charges for the same purchase; distinguish installments.",
        "check repeated card transaction billing",
        (
            "My card was billed twice for one order",
            "Did this buyer pay again for the same purchase?",
        ),
    ),
    (
        "payments",
        "read refund receipt",
        "Retrieve the current state and amount of an existing refund by its receipt.",
        "lookup refund receipt status",
        (
            "Did the customer's money go back to their card?",
            "Has the reimbursement finished processing?",
        ),
    ),
    (
        "support",
        "open customer ticket",
        "Create a support case linked to a customer's order and complaint.",
        "open a support ticket for an order",
        (
            "Log this buyer's problem so an operator can follow up",
            "Create a case for someone reporting an order issue",
        ),
    ),
    (
        "support",
        "search customer tickets",
        "Find existing trouble reports and case records for a customer.",
        "search for customer support tickets",
        (
            "What previous complaints did this buyer open?",
            "Find out whether someone already reported their problem",
        ),
    ),
    (
        "it",
        "check system health",
        "Read service availability and degradation status in the operations sandbox.",
        "check system service health",
        (
            "Is this application up and responding?",
            "Can users reach this application right now?",
        ),
    ),
    (
        "it",
        "create incident",
        "Open an operations incident for a degraded service requiring investigation.",
        "create an operations incident",
        (
            "Record the outage for the on-call engineer",
            "Start tracking this broken application with the response team",
        ),
    ),
    (
        "hr",
        "read leave balance",
        "Retrieve the remaining annual leave entitlement for an employee.",
        "look up employee leave balance",
        (
            "How much vacation does this person have left?",
            "How many more days off can this worker take?",
        ),
    ),
    (
        "hr",
        "submit leave request",
        "Record an employee request for time away from work over a date range.",
        "submit a leave request",
        (
            "Ask for next week away from work",
            "File an absence request for these vacation dates",
        ),
    ),
    (
        "analytics",
        "compare period revenue",
        "Calculate the difference in sales revenue between two reporting periods.",
        "compare sales revenue across periods",
        (
            "How did we do in sales versus last quarter?",
            "Show whether money from sales rose month over month",
        ),
    ),
    (
        "analytics",
        "show return totals",
        "Aggregate refund counts and amounts for a reporting window.",
        "aggregate refund totals for a period",
        (
            "Tell me how much money we gave back this month",
            "Count reimbursements and their value during the quarter",
        ),
    ),
    (
        "communication",
        "send customer notification",
        "Write a customer-facing update to the local notification outbox.",
        "enqueue a customer notification",
        (
            "Let this buyer know there is an update",
            "Tell the customer what just happened without opening a case",
        ),
    ),
    (
        "communication",
        "search local messages",
        "Retrieve previous customer notifications and internal messages matching a query.",
        "search existing messages and notifications",
        (
            "Find what we already told this buyer",
            "Look up a previous update we shared with the customer",
        ),
    ),
)


@dataclass(frozen=True)
class Case:
    id: str
    domain: str
    query: str
    expected: tuple[str, ...]
    kind: str


def canonical(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def corpus(size: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    regions = [f"{org} {region}" for org in ORGANIZATIONS for region in REGIONS]
    rng.shuffle(regions)
    pool = []
    for index in range(size):
        domain, operation, description, *_ = SCENARIOS[index % len(SCENARIOS)]
        group = index // len(SCENARIOS)
        region = f"{regions[group % len(regions)]} {group // len(regions) + 1:02d}"
        identifier = f"bench.{domain}.{operation.replace(' ', '_')}.{group:04d}"
        pool.append(
            {
                "id": identifier,
                "domain": domain,
                "region": region,
                "group": group,
                "restricted": group % 7 == 6,
                "definition": {
                    "name": f"{operation} {region}",
                    "description": f"{description} Scope: {region}.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "resource_id": {"type": "string"},
                            "scope": {"type": "string", "const": region},
                        },
                        "required": ["resource_id", "scope"],
                        "additionalProperties": False,
                    },
                },
            }
        )
    return pool


def labeled(documents: list[dict], count: int, seed: int, split: str) -> list[Case]:
    rng = random.Random(seed + 17)
    chosen = [
        doc
        for doc in documents
        if (doc["group"] % 5 == 0) == (split == "tuning") and not doc["restricted"]
    ]
    rng.shuffle(chosen)
    kinds = ("paraphrase", "lexical", "paraphrase", "denied", "unanswerable")
    cases = []
    scenario_by_operation = {record[1]: record for record in SCENARIOS}
    for index in range(count):
        doc = chosen[index % len(chosen)]
        operation = doc["definition"]["name"].removesuffix(" " + doc["region"])
        _, _, _, lexical, heldout = scenario_by_operation[operation]
        kind = kinds[index % len(kinds)]
        text = (
            lexical
            if kind == "lexical" or split == "tuning"
            else heldout[(index // len(chosen) + index) % len(heldout)]
        )
        expected: tuple[str, ...] = (doc["id"],)
        if kind == "denied":
            expected = ()
        elif kind == "unanswerable":
            text = "Schedule an in-person astronomy lesson"
            expected = ()
        joined = f"{text} for {doc['region']}"
        cases.append(
            Case(f"{split}-{index + 1:04d}", doc["domain"], joined, expected, kind)
        )
    return cases


def fingerprint(documents: list[dict], cases: list[Case]) -> str:
    return hashlib.sha256(
        canonical(
            [
                documents,
                [
                    [case.id, case.domain, case.query, case.expected, case.kind]
                    for case in cases
                ],
            ]
        )
    ).hexdigest()
