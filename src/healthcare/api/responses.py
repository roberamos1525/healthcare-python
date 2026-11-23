"""Response helpers: JSON bodies with the reference escaping and RFC 9457 problem documents."""

import secrets
from typing import Any, Final

from starlette.requests import Request
from starlette.responses import Response

from healthcare.application.core.errors import AppError, FieldErrors
from healthcare.application.core.jsonref import reference_json
from healthcare.application.core.models import to_data

JSON_TYPE: Final = "application/json; charset=utf-8"
PROBLEM_TYPE: Final = "application/problem+json"

_TITLES: Final = {
    400: "The request is invalid.",
    401: "Authentication is required.",
    403: "Access is denied.",
    404: "The resource was not found.",
    409: "The request conflicts with the current state.",
    422: "The request cannot be processed.",
    429: "Too many requests.",
}


def trace_id_of(request: Request) -> str:
    """Return the W3C trace id (32 hexadecimal characters) assigned to the request by the pipeline."""
    trace_id = request.scope.get("state", {}).get("trace_id")
    return trace_id if isinstance(trace_id, str) else secrets.token_hex(16)


def json_response(value: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    """Write a success body with the contract's conventions and the reference escaping."""
    body = reference_json(to_data(value)).encode("utf-8")
    response = Response(body, status_code=status, headers=headers)
    response.headers["content-type"] = JSON_TYPE
    return response


def no_content() -> Response:
    """Write an empty 204."""
    return Response(status_code=204)


def problem_body(status: int, code: str, detail: str, trace_id: str, fields: FieldErrors | None) -> bytes:
    """Render a problem in the reference's member order and escaping."""
    document: dict[str, Any] = {
        "type": f"urn:healthcare:problem:{code}",
        "title": _TITLES.get(status, "An unexpected error occurred."),
        "status": status,
        "code": code,
        "detail": detail,
        "traceId": trace_id,
    }
    if fields is not None:
        document["errors"] = fields.as_dict()
    return reference_json(document).encode("utf-8")


def problem_response(
    request: Request, status: int, code: str, detail: str, fields: FieldErrors | None = None
) -> Response:
    """Write an application problem with the header rules of the reference.

    Problems from the reference's exception handler carry ``Expires`` and ``Pragma``; the authentication
    challenge and the rate limiter write their own headers instead.
    """
    response = Response(problem_body(status, code, detail, trace_id_of(request), fields), status_code=status)
    response.headers["content-type"] = PROBLEM_TYPE
    if code == "unauthorized":
        response.headers["www-authenticate"] = "Bearer"
    elif code == "rate_limited":
        response.headers["retry-after"] = "60"
    else:
        response.headers["expires"] = "-1"
        response.headers["pragma"] = "no-cache"
    return response


def error_response(request: Request, error: AppError) -> Response:
    """Write the problem of an application error."""
    return problem_response(request, error.status, error.code, error.message, error.fields)


def internal_error(request: Request) -> Response:
    """Write the generic 500 problem; details stay in the logs."""
    return problem_response(request, 500, "internal_error", "An unexpected error occurred. Please try again later.")


def unsupported_media_type(request: Request) -> Response:
    """Write the framework's default 415 document, as the reference does before its own error handling."""
    trace = reference_json(f"00-{trace_id_of(request)}-{secrets.token_hex(8)}-03")
    body = (
        '{"type":"https://tools.ietf.org/html/rfc9110#section-15.5.16","title":"Unsupported Media Type",'
        f'"status":415,"traceId":{trace}}}'
    ).encode()
    response = Response(body, status_code=415)
    response.headers["content-type"] = "application/problem+json; charset=utf-8"
    return response


def too_large(request: Request) -> Response:
    """Write the 413 problem for a body over 1 MiB."""
    return problem_response(request, 413, "bad_request", "The request could not be read.")
