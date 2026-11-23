"""Route registration with the reference router's semantics.

Route values declared as identifiers must be GUIDs, otherwise the route counts as unmatched (an empty 404
after authentication). "Method not allowed" is decided on the path template alone, before that constraint,
and ``Allow`` lists the declared methods in ordinal order.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date
from typing import Final
from uuid import UUID

from starlette.requests import Request
from starlette.responses import Response

from healthcare.api.binding import (
    MAX_BODY_BYTES,
    BodyTooLargeError,
    UnsupportedMediaTypeError,
    bind_bytes,
    is_json_media_type,
)
from healthcare.api.context import EndpointOptions, RequestPreparer
from healthcare.api.query import QueryParams, parse_guid
from healthcare.api.responses import error_response, internal_error, too_large, unsupported_media_type
from healthcare.application.core.caller import Caller
from healthcare.application.core.errors import AppError
from healthcare.application.core.models import RequestModel
from healthcare.application.core.ports import Clock

_LOGGER = logging.getLogger("healthcare.http")
ORDINAL_METHODS: Final = ("DELETE", "GET", "POST", "PUT")
_PARAMETER: Final = re.compile(r"\{([A-Za-z]+)\}")


@dataclass(slots=True)
class Ctx:
    """What a handler receives: the request, the caller and binding helpers."""

    request: Request
    caller: Caller
    clock: Clock
    forwarded_headers: bool = False

    def id(self, name: str) -> UUID:
        """Return a route identifier (already validated as a GUID)."""
        value = parse_guid(self.request.path_params[name])
        assert value is not None
        return value

    def query(self) -> QueryParams:
        """Return the query string parameters."""
        return QueryParams(self.request.scope.get("query_string", b"").decode("latin-1"))

    def today(self) -> date:
        """Return the current UTC date for validation rules."""
        return self.clock().astimezone(UTC).date()

    async def body[M: RequestModel](self, model: type[M]) -> M:
        """Read, bind and validate the JSON body (415 for other media types, 413 above 1 MiB)."""
        if not is_json_media_type(self.request.headers.get("content-type")):
            raise UnsupportedMediaTypeError
        declared = self.request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            raise BodyTooLargeError
        chunks: list[bytes] = []
        size = 0
        async for chunk in self.request.stream():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise BodyTooLargeError
            chunks.append(chunk)
        return bind_bytes(b"".join(chunks), model, self.today())


Handler = Callable[[Ctx], Awaitable[Response]]


@dataclass(slots=True)
class RouteSpec:
    """One declared operation."""

    method: str
    path: str
    handler: Handler
    options: EndpointOptions
    parameters: tuple[str, ...]
    segments: tuple[str | None, ...] = field(init=False)

    def __post_init__(self) -> None:
        """Split the template into lower-case literal segments and identifier segments (None)."""
        self.segments = tuple(
            None if _PARAMETER.fullmatch(segment) else segment.lower() for segment in self.path.strip("/").split("/")
        )


class Router:
    """Collects the operations of the API and serves them through FastAPI routes."""

    def __init__(self, preparer: RequestPreparer, clock: Clock, forwarded_headers: bool = False) -> None:
        """Create an empty route table."""
        self.preparer = preparer
        self.clock = clock
        self.forwarded_headers = forwarded_headers
        self.routes: list[RouteSpec] = []

    def route(
        self, method: str, path: str, *, anonymous: bool = False, auth_limit: bool = False, unlimited: bool = False
    ) -> Callable[[Handler], Handler]:
        """Declare an operation."""

        def register(handler: Handler) -> Handler:
            """Add the handler to the route table."""
            parameters = tuple(_PARAMETER.findall(path))
            options = EndpointOptions(anonymous, auth_limit, unlimited, "organizationId" in parameters)
            self.routes.append(RouteSpec(method, path, handler, options, parameters))
            return handler

        return register

    def candidates(self, path: str) -> list[RouteSpec]:
        """Return the operations whose template matches the path, as the reference's routing tree selects them.

        Segment by segment, a literal segment of a template wins over identifier segments: identifiers are
        GUID-constrained, so a template literal such as ``self-registration`` can never be one. Any other
        value goes to the identifier branch without evaluating the constraint, which is why the reference
        decides "method not allowed" before it checks that the identifier is a GUID.
        """
        segments = [segment.lower() for segment in path.strip("/").split("/")]
        remaining = [r for r in self.routes if len(r.segments) == len(segments)]
        for index, segment in enumerate(segments):
            literal = [r for r in remaining if r.segments[index] == segment]
            remaining = literal or [r for r in remaining if r.segments[index] is None]
            if not remaining:
                break
        return remaining

    def allowed_methods(self, path: str) -> list[str]:
        """Return the methods declared for a path, in ordinal order, ignoring route constraints."""
        methods = {route.method for route in self.candidates(path)}
        return [method for method in ORDINAL_METHODS if method in methods]

    def resolve(self, method: str, path: str) -> RouteSpec | int:
        """Return the operation serving the request, or 404/405 as the reference router decides."""
        candidates = self.candidates(path)
        if not candidates:
            return 404
        chosen = next((route for route in candidates if route.method == method), None)
        if chosen is None:
            return 405
        values = [s for s, t in zip(path.strip("/").split("/"), chosen.segments, strict=True) if t is None]
        if any(parse_guid(value) is None for value in values):
            return 404
        return chosen

    def endpoint(self, spec: RouteSpec) -> Callable[[Request], Awaitable[Response]]:
        """Wrap a handler with the GUID constraint, the authentication pipeline and error mapping."""

        async def serve(request: Request) -> Response:
            """Serve one request of the operation."""
            outcome = self.resolve(request.method, request.scope["path"])
            if outcome == 405:
                return await self.method_not_allowed(request)
            if outcome is not spec:
                return await self.unmatched(request)
            prepared = await self.preparer.prepare(request, spec.options)
            if isinstance(prepared, Response):
                return prepared
            try:
                return await spec.handler(Ctx(request, prepared, self.clock, self.forwarded_headers))
            except AppError as error:
                if error.status in (401, 403):
                    _LOGGER.warning(
                        "Request refused",
                        extra={
                            "status": error.status,
                            "code": error.code,
                            "method": request.method,
                            "path": request.url.path,
                        },
                    )
                return error_response(request, error)
            except UnsupportedMediaTypeError:
                return unsupported_media_type(request)
            except BodyTooLargeError:
                return too_large(request)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if await request.is_disconnected():
                    return Response(status_code=499)
                _LOGGER.error(
                    "Unhandled error while processing the request",
                    extra={
                        "method": request.method,
                        "path": request.url.path,
                        "errorType": type(error).__name__,
                    },
                    exc_info=error,
                )
                return internal_error(request)

        serve.__name__ = f"{spec.method.lower()}_{spec.path}"
        return serve

    async def unmatched(self, request: Request) -> Response:
        """Answer a route that does not exist: authentication is still required, then an empty 404."""
        prepared = await self.preparer.prepare(request, None)
        if isinstance(prepared, Response):
            return prepared
        return Response(status_code=404)

    async def method_not_allowed(self, request: Request) -> Response:
        """Answer an undeclared method: after authentication, an empty 405 listing the declared methods."""
        allowed = self.allowed_methods(request.scope["path"])
        prepared = await self.preparer.prepare(request, None)
        if isinstance(prepared, Response):
            return prepared
        headers = {"allow": ", ".join(allowed)} if allowed else None
        return Response(status_code=405, headers=headers)
