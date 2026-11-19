from contextvars import ContextVar

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import get_current_span
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def trace_id() -> str | None:
    context = get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None


class Telemetry:
    def __init__(self, endpoint: str | None = None):
        self.provider = TracerProvider(
            resource=Resource.create({"service.name": "toolmesh-gateway"})
        )
        if endpoint:
            self.provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=3))
            )
        self.tracer = self.provider.get_tracer("toolmesh.gateway")
        self.registry = CollectorRegistry()
        self.requests = Histogram(
            "toolmesh_http_request_seconds",
            "Gateway HTTP response setup latency",
            ["route", "status"],
            registry=self.registry,
        )
        self.executions = Histogram(
            "toolmesh_execution_seconds",
            "Gateway invocation latency by initial outcome",
            ["status"],
            registry=self.registry,
        )
        self.resilience = Counter(
            "toolmesh_read_dispatch_events",
            "Read dispatch retry and circuit events",
            ["service", "event"],
            registry=self.registry,
        )
        self.approval_decisions = Counter(
            "toolmesh_approval_decisions",
            "Committed approval decisions and expirations",
            ["status"],
            registry=self.registry,
        )
        self.approval_age = Gauge(
            "toolmesh_oldest_pending_approval_seconds",
            "Age of the oldest pending approval at the last successful recovery pass",
            registry=self.registry,
        )
        self.recovered = Counter(
            "toolmesh_stale_executions",
            "Stale executions classified by the recovery worker",
            ["status"],
            registry=self.registry,
        )

    def span(self, name: str):
        return self.tracer.start_as_current_span(
            name, record_exception=False, set_status_on_exception=False
        )

    def dispatch_event(self, service: str, event: str) -> None:
        self.resilience.labels(service=service, event=event).inc()
