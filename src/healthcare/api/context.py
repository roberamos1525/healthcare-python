"""The authentication part of the request pipeline, in the reference's order.

Bearer token, rate limits, session re-check, organization selection and access resolution, then the
authentication requirement of protected routes. A stale token sent to an anonymous endpoint is ignored.
"""

import time
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import Response

from healthcare.api.query import parse_guid
from healthcare.api.responses import error_response, problem_response, trace_id_of
from healthcare.application.access import AccessResolver
from healthcare.application.core.caller import Caller
from healthcare.application.core.errors import AppError
from healthcare.infrastructure.config import Settings
from healthcare.infrastructure.security import JwtService


class FixedWindowLimiter:
    """Counts requests per key in fixed one-minute windows (per process, like the reference's limiter)."""

    def __init__(self, permits: int, window_seconds: float = 60.0) -> None:
        """Create a limiter allowing ``permits`` requests per key per window."""
        self._permits = permits
        self._window = window_seconds
        self._entries: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        """Count a request and return whether it is within the key's window."""
        now = time.monotonic()
        if len(self._entries) > 50_000:
            self._entries = {k: v for k, v in self._entries.items() if now - v[0] < self._window}
        entry = self._entries.get(key)
        if entry is None or now - entry[0] >= self._window:
            entry = [now, 0.0]
            self._entries[key] = entry
        if entry[1] >= self._permits:
            return False
        entry[1] += 1
        return True


@dataclass(frozen=True, slots=True)
class EndpointOptions:
    """How the pipeline treats an endpoint."""

    anonymous: bool = False
    auth_limit: bool = False
    unlimited: bool = False
    organization_from_route: bool = False


class RequestPreparer:
    """Builds the caller of a request or answers it with the reference's authentication problems."""

    def __init__(self, settings: Settings, jwt: JwtService, access: AccessResolver) -> None:
        """Create the preparer with its own rate limiters."""
        self._settings = settings
        self._jwt = jwt
        self._access = access
        self._general = FixedWindowLimiter(settings.rate_limiting.general_per_minute)
        self._auth = FixedWindowLimiter(settings.rate_limiting.auth_per_minute)

    def client_address(self, request: Request) -> str:
        """Return the caller's network address, honouring X-Forwarded-For behind a trusted proxy."""
        if self._settings.forwarded_headers:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                for part in forwarded.split(","):
                    if part.strip():
                        return part.strip()
        host = request.client.host if request.client else ""
        host = host.removeprefix("::ffff:")
        return host or "unknown"

    async def prepare(self, request: Request, options: EndpointOptions | None) -> Caller | Response:
        """Return the caller, or the response that ends the request."""
        anonymous = options is not None and options.anonymous
        ip = self.client_address(request)
        principal = None
        header = request.headers.get("authorization", "")
        if len(header) > 7 and header[:7].lower() == "bearer ":
            principal = self._jwt.validate(header[7:].strip())

        if self._settings.rate_limiting.enabled and (options is None or not options.unlimited):
            key = principal.subject if principal is not None and principal.subject else ip
            limited = not self._general.allow(key)
            if not limited and options is not None and options.auth_limit:
                limited = not self._auth.allow(ip)
            if limited:
                return problem_response(request, 429, "rate_limited", "Too many requests. Try again in a minute.")

        caller = Caller(
            request_id=trace_id_of(request),
            ip_address=None if ip == "unknown" else ip,
            user_agent=request.headers.get("user-agent") or None,
        )
        if principal is not None:
            user_id, session_id = parse_guid(principal.subject), parse_guid(principal.session_id)
            if user_id is None or session_id is None:
                return problem_response(request, 401, "invalid_token", "The access token is not valid.")
            try:
                active = await self._access.is_session_active(session_id, user_id)
                if active:
                    caller.set_user(user_id, session_id, principal.platform_role)
                    if options is not None and options.organization_from_route:
                        raw = request.path_params.get("organizationId", "")
                    else:
                        raw = request.headers.get("x-organization-id", "")
                    if raw.strip():
                        organization_id = parse_guid(raw)
                        if organization_id is None:
                            return problem_response(
                                request,
                                400,
                                "invalid_organization_id",
                                "The organization identifier is not a valid UUID.",
                            )
                        await self._access.resolve(caller, user_id, organization_id)
                elif not anonymous:
                    return problem_response(request, 401, "session_expired", "Your session has ended. Sign in again.")
            except AppError as error:
                return error_response(request, error)
        if not anonymous and caller.user_id is None:
            return problem_response(request, 401, "unauthorized", "Authentication is required.")
        return caller
