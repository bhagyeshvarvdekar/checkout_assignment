import json
import logging
import sys
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import Request
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

LOGGER = logging.getLogger("payment_gateway")
request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)
ROUTES = frozenset(
    {
        "/",
        "/payments",
        "/payments/{payment_id}",
        "/metrics",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/docs/oauth2-redirect",
    }
)
EVENT_FIELDS = frozenset(
    {
        "request_id",
        "route",
        "status_code",
        "duration_seconds",
        "error_code",
        "payment_id",
        "outcome",
    }
)
BANK_OUTCOMES = frozenset({"authorized", "declined", "unavailable", "unknown", "timeout", "error"})


class SafeJSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "timestamp": datetime.now(UTC).isoformat(),
            "severity": record.levelname,
            "event": record.getMessage(),
        }
        for field in EVENT_FIELDS:
            if field in record.__dict__:
                data[field] = record.__dict__[field]
        return json.dumps(data, separators=(",", ":"))


def configure_logging(level: str = "INFO") -> None:
    LOGGER.setLevel(level)
    for handler in list(LOGGER.handlers):
        if isinstance(handler.formatter, SafeJSONFormatter):
            LOGGER.removeHandler(handler)
            handler.close()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(SafeJSONFormatter())
    LOGGER.addHandler(handler)
    LOGGER.propagate = True


def log_event(event: str, *, severity: str = "INFO", **fields) -> None:
    safe_fields = {key: value for key, value in fields.items() if key in EVENT_FIELDS}
    safe_fields["request_id"] = request_id_context.get()
    LOGGER.log(getattr(logging, severity), event, extra=safe_fields)


def route_label(scope: dict) -> str:
    route = getattr(scope.get("route"), "path", None)
    if route is None:
        route = scope.get("path")
    return route if route in ROUTES else "unmatched"


class Metrics:
    content_type = CONTENT_TYPE_LATEST

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "gateway_http_requests_total",
            "HTTP requests completed.",
            ("route", "status"),
            registry=self.registry,
        )
        self.payments = Counter(
            "gateway_payment_outcomes_total",
            "Confirmed or rejected payment requests.",
            ("status",),
            registry=self.registry,
        )
        self.bank_duration = Histogram(
            "gateway_bank_request_duration_seconds",
            "Bank request duration including failures.",
            ("outcome",),
            registry=self.registry,
        )

    def observe_bank(self, outcome: str, duration: float) -> None:
        bounded_outcome = outcome if outcome in BANK_OUTCOMES else "error"
        self.bank_duration.labels(outcome=bounded_outcome).observe(duration)
        log_event(
            "bank_completed",
            route="/payments",
            outcome=bounded_outcome,
            duration_seconds=round(duration, 6),
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)


async def observe_request(request: Request, call_next):
    request.state.request_id = str(uuid4())
    token = request_id_context.set(request.state.request_id)
    started = time.monotonic()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        return response
    finally:
        route = route_label(request.scope)
        request.app.state.metrics.requests.labels(route=route, status=str(status_code)).inc()
        fields = {
            "route": route,
            "status_code": status_code,
            "duration_seconds": round(time.monotonic() - started, 6),
        }
        if hasattr(request.state, "payment_id"):
            fields["payment_id"] = request.state.payment_id
        if hasattr(request.state, "error_code"):
            fields["error_code"] = request.state.error_code
        log_event("request_completed", **fields)
        request_id_context.reset(token)
