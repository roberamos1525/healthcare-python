"""Structured JSON logging and OpenTelemetry tracing (OTLP export when OTEL_EXPORTER_OTLP_ENDPOINT is set)."""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Final

from opentelemetry import trace
from opentelemetry.trace import Tracer

TRACE_ID: ContextVar[str | None] = ContextVar("trace_id", default=None)
"""The trace id of the request being handled, added to every log record."""

_LEVELS: Final = {
    "verbose": logging.DEBUG,
    "debug": logging.DEBUG,
    "trace": logging.DEBUG,
    "information": logging.INFO,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.CRITICAL,
    "critical": logging.CRITICAL,
}
_STANDARD_ATTRIBUTES: Final = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    """Writes one JSON object per log record; extra fields become members (never bodies or PHI)."""

    def format(self, record: logging.LogRecord) -> str:
        """Render the record as compact JSON."""
        data: dict[str, Any] = {
            "@t": datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z"),
            "@l": record.levelname.title() if record.levelno != logging.INFO else "Information",
            "@m": record.getMessage(),
            "logger": record.name,
        }
        trace_id = TRACE_ID.get()
        if trace_id:
            data["traceId"] = trace_id
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRIBUTES and not key.startswith("_"):
                data[key] = value if isinstance(value, str | int | float | bool) or value is None else str(value)
        if record.exc_info and record.levelno >= logging.ERROR:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str)


def configure_logging(level_name: str) -> None:
    """Send JSON logs to standard output at the configured level (Serilog-style names accepted)."""
    level = _LEVELS.get(level_name.strip().lower(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Framework access logs would duplicate the request log line and could carry query strings.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
    logging.getLogger("aiormq").setLevel(logging.WARNING)
    logging.getLogger("aio_pika").setLevel(logging.WARNING)


def configure_tracing(otlp_endpoint: str, service_name: str = "healthcare-api") -> Tracer | None:
    """Install an OTLP exporter when an endpoint is configured and return the tracer (None otherwise)."""
    if not otlp_endpoint.strip():
        return None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    endpoint = otlp_endpoint.rstrip("/")
    if not endpoint.endswith("/v1/traces"):
        endpoint += "/v1/traces"
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    return trace.get_tracer("healthcare.api")


def shutdown_tracing() -> None:
    """Flush and stop the tracer provider, if one was installed."""
    provider = trace.get_tracer_provider()
    shutdown = getattr(provider, "shutdown", None)
    if callable(shutdown):
        shutdown()
