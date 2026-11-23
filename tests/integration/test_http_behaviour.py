"""HTTP behaviour scenarios: body binding stages, routing, CORS, operational endpoints and problem documents."""

import re
import uuid
from typing import Any

from tests.integration.support import create_user, expect, new_client, problem

_ORIGIN = "http://localhost:5173"


async def test_request_bodies_are_bound_in_the_reference_stages() -> None:
    """The unreadable-body, required-property and validation stages and their messages."""
    anonymous = new_client("")
    unsupported = expect(
        await anonymous.send("POST", "/api/v1/auth/login", "email=a", {"Content-Type": "text/plain"}), 415
    )
    media: dict[str, Any] = unsupported.json()
    assert media["type"] == "https://tools.ietf.org/html/rfc9110#section-15.5.16", f"unexpected 415 document {media}"
    assert media["title"] == "Unsupported Media Type", f"unexpected 415 document {media}"
    empty = problem(
        await anonymous.send("POST", "/api/v1/auth/login", "", {"Content-Type": "application/json"}),
        400,
        "validation_failed",
    )
    assert empty.get("errors", {}).get("body") == ["A non-empty request body is required."], (
        f"unexpected empty-body errors {empty.get('errors')}"
    )
    wrong_type = problem(
        await anonymous.send("POST", "/api/v1/auth/login", '{"email": 5, "password": "x"}'), 400, "validation_failed"
    )
    assert wrong_type.get("errors", {}).get("request") == ["The request field is required."], (
        f"unexpected wrong-type errors {wrong_type.get('errors')}"
    )
    missing = problem(await anonymous.post("/api/v1/auth/login", {"password": "x"}), 400, "validation_failed")
    assert missing.get("errors", {}).get("email") == ["The Email field is required."], (
        f"unexpected missing-property errors {missing.get('errors')}"
    )
    too_large = await anonymous.send("POST", "/api/v1/auth/login", '{"email":"' + "a" * ((1 << 20) + 16) + '"}')
    assert too_large.status == 413, f"expected 413, got {too_large.status}"


async def test_routing_matches_the_reference_for_unknown_routes_methods_and_identifiers() -> None:
    """Unknown routes, malformed identifiers and unsupported methods answer like the reference."""
    user = await create_user("routing")
    problem(await new_client("").get("/api/v1/unknown"), 401, "unauthorized")
    unknown = await user.get("/api/v1/unknown")
    assert unknown.status == 404 and len(unknown.body) == 0, (
        f"expected an empty 404, got {unknown.status} {unknown.body!r}"
    )
    not_guid = await user.get("/api/v1/patients/not-a-guid")
    assert not_guid.status == 404 and len(not_guid.body) == 0, (
        f"a malformed identifier does not match the route: {not_guid.status} {not_guid.body!r}"
    )
    method = await user.send("DELETE", f"/api/v1/patients/{uuid.uuid4()}")
    allow = method.headers.get("allow")
    assert method.status == 405 and allow == "GET, PUT" and len(method.body) == 0, (
        f"unexpected 405 {method.status} {allow!r} {method.body!r}"
    )


async def test_cors_preflight_and_exposed_headers_follow_the_configuration() -> None:
    """Preflight, simple and foreign-origin requests follow the CORS configuration."""
    client = new_client("")
    preflight = await client.send(
        "OPTIONS",
        "/api/v1/auth/login",
        None,
        {
            "Origin": _ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    headers = preflight.headers
    assert preflight.status == 204, f"unexpected preflight {preflight.status} {dict(headers)}"
    assert headers.get("access-control-allow-origin") == _ORIGIN, f"unexpected preflight {dict(headers)}"
    assert headers.get("access-control-allow-methods") == "GET,POST,PUT,DELETE", f"unexpected preflight {dict(headers)}"
    allowed_headers = "Authorization,Content-Type,X-Organization-Id,Idempotency-Key"
    assert headers.get("access-control-allow-headers") == allowed_headers, f"unexpected preflight {dict(headers)}"
    simple = await client.send("GET", "/health/live", None, {"Origin": _ORIGIN})
    assert simple.headers.get("access-control-allow-origin") == _ORIGIN, (
        f"unexpected CORS headers {dict(simple.headers)}"
    )
    assert simple.headers.get("access-control-expose-headers") == "Idempotent-Replayed,Retry-After", (
        f"unexpected CORS headers {dict(simple.headers)}"
    )
    foreign = await client.send("GET", "/health/live", None, {"Origin": "https://attacker.example.test"})
    assert not foreign.headers.get("access-control-allow-origin"), "an unlisted origin is not allowed"


async def test_operational_endpoints_serve_health_keys_and_the_contract() -> None:
    """The anonymous health, key set and contract endpoints."""
    client = new_client("")
    for path in ("/health/live", "/health/ready"):
        response = await client.get(path)
        content_type = response.headers.get("content-type", "")
        assert response.status == 200 and response.body == b"Healthy" and content_type.startswith("text/plain"), (
            f"{path} answered {response.status} {response.body!r} {content_type!r}"
        )
    keys = expect(await client.get("/.well-known/jwks.json"), 200).json()
    key_list = keys.get("keys") if isinstance(keys, dict) else None
    assert isinstance(key_list, list) and len(key_list) == 1, f"unexpected key set {keys}"
    assert key_list[0].get("kty") == "RSA" and key_list[0].get("alg") == "RS256", f"unexpected key set {keys}"
    contract = await client.get("/openapi/v1.yaml")
    content_type = contract.headers.get("content-type")
    assert contract.status == 200 and content_type == "application/yaml; charset=utf-8", (
        f"unexpected contract response {contract.status} {content_type!r}"
    )


async def test_problem_documents_carry_the_trace_identifier_and_no_cache_headers() -> None:
    """Problem documents carry a trace identifier, no-store caching and the reference member order."""
    response = await new_client("").post(
        "/api/v1/auth/login", {"email": "nobody@example.test", "password": "Wrong-Password-1"}
    )
    document = problem(response, 401, "invalid_credentials")
    trace_id = document.get("traceId")
    assert isinstance(trace_id, str) and re.fullmatch(r"[0-9a-f]{32}", trace_id), f"unexpected problem {document}"
    assert response.headers.get("cache-control") == "no-store", f"unexpected headers {dict(response.headers)}"
    assert response.body.startswith(b'{"type":"urn:healthcare:problem:invalid_credentials","title":'), (
        f"unexpected member order {response.body!r}"
    )
