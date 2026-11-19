from typing import Annotated
from uuid import UUID

from pydantic import Field

from shared.base_contracts import Contract as Contract
from shared.base_contracts import Customer as Customer
from shared.base_contracts import Identifier as Identifier
from shared.base_contracts import Subject as Subject
from shared.domain_contracts import DOMAIN_TOOLS


class TransactionLookup(Customer):
    transaction_id: Identifier


class OrderLookup(Customer):
    order_id: Identifier


class Transaction(Contract):
    id: Identifier
    order_id: Identifier
    customer_id: Identifier
    amount_minor: int
    currency: str
    status: str
    installment: int | None


class Transactions(Contract):
    transactions: list[Transaction]


class DuplicateEvidence(Contract):
    duplicate_capture_detected: bool
    groups: list[list[str]]


class CreateTicket(OrderLookup):
    subject: Subject


class TicketLookup(Customer):
    ticket_id: UUID


class Ticket(CreateTicket):
    id: UUID
    status: str


class Tickets(Contract):
    tickets: list[Ticket]


class RefundIntent(TransactionLookup):
    amount_minor: Annotated[int, Field(ge=1, le=2_147_483_647)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


class RefundLookup(Customer):
    refund_id: Annotated[UUID, Field(strict=False)]


class Refund(RefundIntent):
    id: UUID
    status: str


class Notification(RefundLookup):
    id: UUID
    status: str


class TicketResolution(TicketLookup):
    refund_id: UUID


class ResolvedTicket(TicketResolution):
    status: str


TOOLS: dict[str, tuple[str, type[Contract], type[Contract], bool]] = {
    "payments.get_transaction": (
        "Retrieve a captured transaction.",
        TransactionLookup,
        Transaction,
        False,
    ),
    "payments.list_transactions": (
        "List up to 50 customer captures.",
        Customer,
        Transactions,
        False,
    ),
    "payments.check_duplicate_payment": (
        "Find matching non-installment captures for an order.",
        OrderLookup,
        DuplicateEvidence,
        False,
    ),
    "support.create_ticket": (
        "Create a ticket for a customer and order.",
        CreateTicket,
        Ticket,
        True,
    ),
    "support.get_ticket": ("Retrieve a customer ticket.", TicketLookup, Ticket, False),
    "support.search_tickets": (
        "List up to 50 recent customer tickets.",
        Customer,
        Tickets,
        False,
    ),
    "payments.issue_refund": (
        "Request a sandbox refund with human approval.",
        RefundIntent,
        Refund,
        True,
    ),
    "payments.get_refund_status": (
        "Read a confirmed refund from the sandbox ledger.",
        RefundLookup,
        Refund,
        False,
    ),
    "support.resolve_ticket": (
        "Resolve a ticket after confirmed refund and notification.",
        TicketResolution,
        ResolvedTicket,
        True,
    ),
    "communication.send_notification": (
        "Record a local notification for a confirmed refund.",
        RefundLookup,
        Notification,
        True,
    ),
}


def tool_contract(tool_id: str) -> dict:
    description, arguments, result, write = TOOLS[tool_id]
    return {
        "description": description,
        "input_schema": arguments.model_json_schema(),
        "output_schema": result.model_json_schema(),
        "risk": "approval"
        if tool_id in {"payments.issue_refund", "it.restart_service"}
        else "write"
        if write
        else "read",
    }


class ITHealth(Customer):
    service_id: Identifier


class ITService(Contract):
    service_id: Identifier
    status: str


class OpenIncident(ITHealth):
    title: Subject


class Incident(ITService):
    id: str
    title: Subject


class Incidents(Contract):
    incidents: list[Incident]


TOOLS.update(
    {
        "it.check_service_health": (
            "Read a sandbox service health record.",
            ITHealth,
            ITService,
            False,
        ),
        "it.list_incidents": (
            "List recent persisted sandbox incidents.",
            Customer,
            Incidents,
            False,
        ),
        "it.create_incident": (
            "Open a simulated incident for a customer service.",
            OpenIncident,
            Incident,
            True,
        ),
    }
)

TOOLS.update(DOMAIN_TOOLS)
