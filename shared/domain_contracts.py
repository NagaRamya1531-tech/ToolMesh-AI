"""Public models for additional sandbox operations."""

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from shared.base_contracts import Contract, Customer, Identifier, Subject

Date = Annotated[date, Field(strict=False)]
Guid = Annotated[UUID, Field(strict=False)]
ShortText = Annotated[str, Field(min_length=1, max_length=500)]


class MethodLookup(Customer):
    transaction_id: Identifier


class PaymentMethod(Contract):
    brand: str
    last_four: str


class CustomerRecord(Customer):
    display_name: str
    status: str


class TicketChange(Customer):
    ticket_id: Guid
    note: Subject


class TicketTransition(TicketChange):
    status: Literal["investigating", "resolved"]


class TicketState(Contract):
    ticket_id: Guid
    status: str


class ServiceLookup(Customer):
    service_id: Identifier


class LogQuery(ServiceLookup):
    level: Literal["INFO", "WARN", "ERROR"]
    limit: Annotated[int, Field(ge=1, le=50)]


class Log(Contract):
    timestamp: str
    level: str
    message: str


class Logs(Contract):
    logs: list[Log]


class RestartResult(ServiceLookup):
    id: Guid
    status: str


class Deployment(Contract):
    service_id: Identifier
    release: str
    status: str


class EmployeeLookup(Customer):
    employee_id: Identifier


class DepartmentLookup(Customer):
    department_id: Identifier


class Employee(Contract):
    employee_id: Identifier
    department_id: Identifier
    display_name: str
    title: str


class Department(Contract):
    department_id: Identifier
    name: str


class Balance(Contract):
    employee_id: Identifier
    available_days: int


class DateRange(Contract):
    start_date: Date
    end_date: Date


class LeaveRequest(EmployeeLookup, DateRange):
    pass


class LeaveReceipt(Balance, DateRange):
    id: Guid
    status: str


class HRNotice(EmployeeLookup):
    leave_id: Guid


class PolicyLookup(Customer):
    policy_id: Identifier


class CompanyPolicy(Contract):
    policy_id: Identifier
    title: str
    summary: str


class ReportingWindow(Customer, DateRange):
    pass


class CompareWindow(ReportingWindow):
    previous_start_date: Date
    previous_end_date: Date


class Totals(Contract):
    count: int
    total_minor: int
    currency: Literal["USD"]


class DailyTotal(Totals):
    day: Date


class Revenue(Contract):
    days: list[DailyTotal]


class CustomerMetrics(Contract):
    customers: int
    repeat_customers: int


class Comparison(Contract):
    current: Totals
    previous: Totals
    delta_minor: int


class Report(Contract):
    id: Guid
    sales: Totals
    refunds: Totals


class Email(Customer):
    recipient_id: Identifier
    subject: Subject
    body: ShortText


class MessageLookup(Customer):
    message_id: Guid


class MessageSearch(Customer):
    query: Annotated[str, Field(max_length=100)]
    limit: Annotated[int, Field(ge=1, le=25)]


class Message(Contract):
    id: Guid
    kind: str
    subject: str
    body: str
    status: str


class Messages(Contract):
    messages: list[Message]


class ChannelLookup(Customer):
    channel_name: Identifier


class Channel(Contract):
    channel_name: Identifier
    members: list[Identifier]


DOMAIN_TOOLS: dict[str, tuple[str, type[Contract], type[Contract], bool]] = {
    "payments.get_payment_method": (
        "Return only a payment brand and masked suffix.",
        MethodLookup,
        PaymentMethod,
        False,
    ),
    "support.get_customer": (
        "Read the scoped customer directory record.",
        Customer,
        CustomerRecord,
        False,
    ),
    "support.update_ticket": (
        "Move a scoped ticket through its investigation states.",
        TicketTransition,
        TicketState,
        True,
    ),
    "support.escalate_ticket": (
        "Escalate an active ticket with a note.",
        TicketChange,
        TicketState,
        True,
    ),
    "it.get_system_logs": (
        "Read timestamped simulated logs by severity.",
        LogQuery,
        Logs,
        False,
    ),
    "it.restart_service": (
        "Request a human-approved simulated restart.",
        ServiceLookup,
        RestartResult,
        True,
    ),
    "it.get_deployment_status": (
        "Read the current deployment release and status.",
        ServiceLookup,
        Deployment,
        False,
    ),
    "hr.get_employee": (
        "Read an authorized employee directory entry.",
        EmployeeLookup,
        Employee,
        False,
    ),
    "hr.get_department": (
        "Read an authorized department.",
        DepartmentLookup,
        Department,
        False,
    ),
    "hr.get_leave_balance": (
        "Read the scoped employee's remaining allowance.",
        EmployeeLookup,
        Balance,
        False,
    ),
    "hr.request_leave": (
        "Reserve leave days and persist a request.",
        LeaveRequest,
        LeaveReceipt,
        True,
    ),
    "hr.get_manager": (
        "Read the employee's manager directory entry.",
        EmployeeLookup,
        Employee,
        False,
    ),
    "hr.get_company_policy": (
        "Read a sandbox company policy.",
        PolicyLookup,
        CompanyPolicy,
        False,
    ),
    "analytics.get_daily_revenue": (
        "Group captured USD revenue by day.",
        ReportingWindow,
        Revenue,
        False,
    ),
    "analytics.get_sales_metrics": (
        "Aggregate captured USD transactions.",
        ReportingWindow,
        Totals,
        False,
    ),
    "analytics.get_customer_metrics": (
        "Count active and repeat customers.",
        ReportingWindow,
        CustomerMetrics,
        False,
    ),
    "analytics.generate_report": (
        "Persist a sales and refund report for a date window.",
        ReportingWindow,
        Report,
        True,
    ),
    "analytics.get_refund_metrics": (
        "Aggregate confirmed USD refunds.",
        ReportingWindow,
        Totals,
        False,
    ),
    "analytics.compare_periods": (
        "Compare sales between two date windows.",
        CompareWindow,
        Comparison,
        False,
    ),
    "communication.send_email": (
        "Record a simulated email for a scoped recipient.",
        Email,
        Message,
        True,
    ),
    "communication.search_messages": (
        "Search local message text within customer scope.",
        MessageSearch,
        Messages,
        False,
    ),
    "communication.get_message": (
        "Read a scoped local message.",
        MessageLookup,
        Message,
        False,
    ),
    "communication.create_channel": (
        "Create a sandbox communication channel.",
        ChannelLookup,
        Channel,
        True,
    ),
    "communication.get_channel_members": (
        "Read membership of a scoped channel.",
        ChannelLookup,
        Channel,
        False,
    ),
    "communication.notify_leave": (
        "Record a simulated notification after a confirmed leave request.",
        HRNotice,
        Message,
        True,
    ),
}
