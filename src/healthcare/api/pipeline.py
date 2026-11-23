"""The outermost ASGI middleware: trace id, request span, security headers, CORS, failure guard and logging."""

import logging
import secrets
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, Final

from opentelemetry.trace import SpanKind, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from starlette.datastructures import Headers, MutableHeaders

from healthcare.api.responses import problem_body
from healthcare.infrastructure.config import Settings
from healthcare.infrastructure.telemetry import TRACE_ID

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_LOGGER = logging.getLogger("healthcare.http")
CORS_METHODS: Final = "GET,POST,PUT,DELETE"
CORS_HEADERS: Final = "Authorization,Content-Type,X-Organization-Id,Idempotency-Key"
CORS_EXPOSED: Final = "Idempotent-Replayed,Retry-After"
_SECURITY_HEADERS: Final = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
    # API responses can contain protected health information and must never be stored by caches.
    (b"cache-control", b"no-store"),
)
_REPLACED: Final = frozenset(name for name, _ in _SECURITY_HEADERS) | {b"server", b"strict-transport-security"}
_PROPAGATOR: Final = TraceContextTextMapPropagator()


def incoming_trace_id(header: str | None) -> str | None:
    """Return the trace id of a valid incoming ``traceparent`` header."""
    if not header:
        return None
    parts = header.split("-")
    if len(parts) != 4 or len(parts[1]) != 32 or not parts[1].strip("0"):
        return None
    try:
        int(parts[1], 16)
    except ValueError:
        return None
    return parts[1].lower()


def normalize_path(path: str) -> str:
    """Match routes like the reference router: literal segments ignore case and a trailing slash is ignored."""
    lowered = path.lower()
    if len(lowered) > 1 and lowered.endswith("/"):
        lowered = lowered[:-1]
    return lowered


class OuterMiddleware:
    """Wraps every request: tracing, security headers (applied just before the response starts), CORS and logs."""

    def __init__(self, app: ASGIApp, settings: Settings, tracer: Tracer | None) -> None:
        """Wrap the application."""
        self.app = app
        self.settings = settings
        self.tracer = tracer
        self._origins = {origin.rstrip("/").lower(): origin for origin in settings.cors_allowed_origins}

    def _hsts_applies(self, scope: Scope, headers: Headers) -> bool:
        """Return whether HSTS is sent: HTTPS at a non-local host outside development and testing."""
        if self.settings.allows_temporary_keys:
            return False
        https = scope.get("scheme") == "https"
        if self.settings.forwarded_headers and headers.get("x-forwarded-proto", "").lower() == "https":
            https = True
        host = headers.get("host", "").rsplit(":", 1)[0] if not headers.get("host", "").endswith("]") else ""
        return https and host not in ("localhost", "127.0.0.1", "[::1]", "")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one HTTP request."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        headers = Headers(scope=scope)
        method: str = scope["method"]
        path: str = scope["path"]
        span = None
        if self.tracer is not None and not path.startswith("/health"):
            context = _PROPAGATOR.extract(carrier=dict(headers.items()))
            span = self.tracer.start_span(
                f"{method} {path}",
                context=context,
                kind=SpanKind.SERVER,
                attributes={"http.request.method": method, "url.path": path},
            )
        trace_id = None
        if span is not None and span.get_span_context().is_valid:
            trace_id = format(span.get_span_context().trace_id, "032x")
        trace_id = trace_id or incoming_trace_id(headers.get("traceparent")) or secrets.token_hex(16)
        scope.setdefault("state", {})["trace_id"] = trace_id
        scope["path"] = normalize_path(path)
        token = TRACE_ID.set(trace_id)
        hsts = self._hsts_applies(scope, headers)
        origin = headers.get("origin")
        allowed_origin = self._origins.get(origin.rstrip("/").lower()) if origin else None
        state = {"status": 0, "started": False}

        async def send_wrapper(message: Message) -> None:
            """Apply the security and CORS headers when the response starts."""
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                state["started"] = True
                raw = [(name, value) for name, value in message.get("headers", []) if name.lower() not in _REPLACED]
                if message["status"] != 405:
                    raw = [(name, value) for name, value in raw if name.lower() != b"allow"]
                raw.extend(_SECURITY_HEADERS)
                if hsts:
                    raw.append((b"strict-transport-security", b"max-age=2592000"))
                if allowed_origin is not None and origin is not None:
                    response_headers = MutableHeaders(raw=raw)
                    response_headers["access-control-allow-origin"] = origin
                    response_headers["access-control-expose-headers"] = CORS_EXPOSED
                    if len(self._origins) > 1:
                        response_headers["vary"] = "Origin"
                message["headers"] = raw
            await send(message)

        try:
            if origin and method == "OPTIONS" and headers.get("access-control-request-method"):
                await self._preflight(origin, allowed_origin, send)
                state["status"], state["started"] = 204, True
            else:
                await self.app(scope, receive, send_wrapper)
        except Exception as error:
            _LOGGER.error("A request handler failed", extra={"method": method, "path": path}, exc_info=error)
            if not state["started"]:
                await self._internal_error(trace_id, send_wrapper)
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            status = state["status"] or 500
            if span is not None:
                span.set_attribute("http.response.status_code", int(status))
                span.end()
            _LOGGER.info(
                f"HTTP {method} {path} responded {status} in {elapsed:.1f} ms",
                extra={"method": method, "path": path, "status": status, "elapsedMs": round(elapsed, 1)},
            )
            TRACE_ID.reset(token)

    async def _preflight(self, origin: str, allowed: str | None, send: Send) -> None:
        """Answer a CORS preflight with 204; an allowed origin gets the policy's full method and header lists."""
        raw: list[tuple[bytes, bytes]] = list(_SECURITY_HEADERS)
        if allowed is not None:
            raw.append((b"access-control-allow-origin", origin.encode("latin-1")))
            if len(self._origins) > 1:
                raw.append((b"vary", b"Origin"))
            raw.append((b"access-control-allow-headers", CORS_HEADERS.encode()))
            raw.append((b"access-control-allow-methods", CORS_METHODS.encode()))
        await send({"type": "http.response.start", "status": 204, "headers": raw})
        await send({"type": "http.response.body", "body": b""})

    async def _internal_error(self, trace_id: str, send: Send) -> None:
        """Write the generic 500 problem when a handler failed before responding."""
        body = problem_body(
            500, "internal_error", "An unexpected error occurred. Please try again later.", trace_id, None
        )
        await send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [
                    (b"content-type", b"application/problem+json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"expires", b"-1"),
                    (b"pragma", b"no-cache"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
