"""Tenant isolation and security scenarios: cross-tenant access, row-level security, tokens, headers and limits."""

import base64
import json
import time
import uuid
from typing import Any
from urllib.parse import quote

import asyncpg
import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from healthcare.api.app import create_app
from healthcare.application.core.caller import Scope
from tests.integration.support import (
    book,
    create_practice,
    create_user,
    env,
    expect,
    idempotency_key,
    new_client,
    problem,
    read,
    slots,
)


async def test_staff_of_one_organization_cannot_read_another_organizations_patients() -> None:
    """Cross-tenant reads return not found and lists never include other tenants."""
    alpha = await create_practice("Alpha")
    beta = await create_practice("Beta")
    problem(await alpha.owner.get(f"/api/v1/patients/{beta.patient_id}"), 404, "not_found")
    listed = read(await alpha.owner.get("/api/v1/patients?pageSize=100"), 200)
    assert all(patient["id"] != beta.patient_id for patient in listed["items"]), "another tenant's patient is listed"


async def test_selecting_another_organization_in_the_header_grants_no_staff_access() -> None:
    """Switching the organization header yields guest access only."""
    alpha = await create_practice("Alpha")
    beta = await create_practice("Beta")
    alpha.owner.organization = beta.organization_id
    try:
        problem(await alpha.owner.get("/api/v1/patients"), 403, "forbidden")
        problem(await alpha.owner.get(f"/api/v1/patients/{beta.patient_id}"), 403, "forbidden")
        problem(await alpha.owner.get("/api/v1/appointments"), 403, "forbidden")
        problem(await alpha.owner.post("/api/v1/clinics", {"name": "Injected"}), 403, "forbidden")
        problem(
            await alpha.owner.put(
                f"/api/v1/organizations/{beta.organization_id}/settings",
                {
                    "defaultAppointmentDurationMinutes": 30,
                    "minimumBookingNoticeMinutes": 0,
                    "bookingHorizonDays": 90,
                    "patientCancellationWindowHours": 0,
                    "reminderOffsetsMinutes": [60],
                    "allowPatientSelfScheduling": True,
                    "autoConfirmPatientBookings": True,
                },
            ),
            403,
            "forbidden",
        )
    finally:
        alpha.owner.organization = alpha.organization_id


async def test_organizations_that_disable_self_scheduling_refuse_non_members() -> None:
    """Non-members are refused with not_a_member when self-scheduling is off."""
    beta = await create_practice("Beta")
    expect(
        await beta.owner.put(
            f"/api/v1/organizations/{beta.organization_id}/settings",
            {
                "defaultAppointmentDurationMinutes": 30,
                "minimumBookingNoticeMinutes": 60,
                "bookingHorizonDays": 90,
                "patientCancellationWindowHours": 24,
                "reminderOffsetsMinutes": [1440],
                "allowPatientSelfScheduling": False,
                "autoConfirmPatientBookings": False,
            },
        ),
        200,
    )
    outsider = await create_user("outsider")
    outsider.organization = beta.organization_id
    problem(await outsider.get("/api/v1/providers"), 403, "not_a_member")
    problem(await outsider.get(f"/api/v1/organizations/{beta.organization_id}"), 403, "forbidden")


async def test_resources_of_another_organization_cannot_be_changed_by_identifier() -> None:
    """Cross-tenant writes are refused and leave the other tenant's data unchanged."""
    alpha = await create_practice("Alpha")
    beta = await create_practice("Beta")
    beta_slots = await slots(beta.owner, beta, 5)
    beta_appointment = await book(beta.owner, beta, beta_slots[0])
    problem(
        await alpha.owner.put(
            f"/api/v1/clinics/{beta.clinic_id}",
            {
                "name": "Taken Over",
                "status": "active",
                "version": 1,
            },
        ),
        404,
        "not_found",
    )
    problem(
        await alpha.owner.post(f"/api/v1/appointments/{beta_appointment['id']}/cancel", {"reason": "Malicious"}),
        404,
        "not_found",
    )
    book_in_beta = await alpha.owner.send(
        "POST",
        "/api/v1/appointments",
        {
            "patientId": alpha.patient_id,
            "providerId": beta.provider_id,
            "locationId": beta.location_id,
            "startsAt": beta_slots[1]["startsAt"],
            "visitType": "in_person",
        },
        {"Idempotency-Key": idempotency_key()},
    )
    assert 400 <= book_in_beta.status < 500, f"expected a client error, got {book_in_beta.status}"
    count = await env().scalar(
        "SELECT count(*) FROM scheduling.appointments WHERE organization_id = $1", uuid.UUID(beta.organization_id)
    )
    assert count == 1, f"expected one appointment in beta, got {count}"
    current = read(await beta.owner.get(f"/api/v1/appointments/{beta_appointment['id']}"), 200)
    assert current["status"] == "confirmed", "the appointment is unchanged"


async def set_tenant(connection: asyncpg.Connection, organization_id: str) -> None:
    """Set the tenant context on a raw connection exactly as the application does."""
    await connection.execute(
        "SELECT set_config('app.current_organization_id', $1, false), set_config('app.access_scope', 'tenant', false)",
        organization_id,
    )


async def count(connection: asyncpg.Connection, sql: str, *args: Any) -> int:
    """Run a count query on the connection."""
    return int(await connection.fetchval(sql, *args))


async def sql_state(connection: asyncpg.Connection, sql: str, *args: Any) -> str:
    """Run a statement and return the SQLSTATE of the PostgreSQL error it raises, or an empty string."""
    try:
        await connection.execute(sql, *args)
    except asyncpg.PostgresError as error:
        return str(error.sqlstate)
    return ""


async def test_row_level_security_hides_other_tenants_from_the_application_role() -> None:
    """Row-level security at the database hides and protects other tenants' rows."""
    alpha = await create_practice("Alpha")
    beta = await create_practice("Beta")
    connection = await env().app_role()
    try:
        visible = await count(connection, "SELECT count(*) FROM patient.patients")
        assert visible == 0, f"no rows without a tenant, got {visible}"
        await set_tenant(connection, alpha.organization_id)
        visible = await count(connection, "SELECT count(*) FROM patient.patients")
        assert visible == 1, f"one patient in alpha, got {visible}"
        assert (
            await count(connection, "SELECT count(*) FROM patient.patients WHERE id = $1", uuid.UUID(beta.patient_id))
            == 0
        ), "beta's patient is hidden"
        assert (
            await count(
                connection,
                "SELECT count(*) FROM organization.clinics WHERE organization_id = $1",
                uuid.UUID(beta.organization_id),
            )
            == 0
        ), "beta's clinics are hidden"
        status = await connection.execute(
            "UPDATE patient.patients SET first_name = 'Changed' WHERE id = $1", uuid.UUID(beta.patient_id)
        )
        assert status == "UPDATE 0", f"the update touches nothing: {status}"
        state = await sql_state(
            connection,
            """
            INSERT INTO organization.clinics (id, organization_id, name, status, created_at, updated_at, version)
            VALUES (gen_random_uuid(), $1, 'Cross-tenant insert', 'active', now(), now(), 1)
        """,
            uuid.UUID(beta.organization_id),
        )
        assert state == "42501", f"expected insufficient privilege, got {state!r}"
    finally:
        await connection.close()


async def test_the_application_role_cannot_bypass_security_or_rewrite_history() -> None:
    """The application role holds no privileges to bypass security or change history."""
    practice = await create_practice("Rivera")
    connection = await env().app_role()
    try:
        await set_tenant(connection, practice.organization_id)
        privileged = await env().scalar(
            "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = 'healthcare_app'"
        )
        assert not privileged, "the application role must not be privileged"
        for statement in (
            "UPDATE platform.audit_events SET action = 'tampered'",
            "DELETE FROM platform.audit_events",
            "DELETE FROM patient.patients",
            "UPDATE scheduling.appointment_status_history SET reason = 'tampered'",
            "ALTER TABLE patient.patients DISABLE ROW LEVEL SECURITY",
            "CREATE TABLE patient.shadow_copy (id uuid)",
        ):
            state = await sql_state(connection, statement)
            assert state == "42501", f"expected a privilege error for {statement!r}, got {state!r}"
    finally:
        await connection.close()


async def test_pooled_connections_carry_no_tenant_after_a_request() -> None:
    """Tenant settings never leak to the next user of a pooled connection."""
    practice = await create_practice("Rivera")
    expect(await practice.owner.get("/api/v1/patients"), 200)
    container = env().container
    assert container is not None
    for _ in range(20):
        organization, user, scope = await container.database.idle_scope()
        assert (organization, user, scope) == ("", "", ""), (
            f"a pooled connection kept the tenant context {organization!r} {user!r} {scope!r}"
        )


async def test_a_scoped_transaction_sees_only_its_organization_and_can_switch_scope() -> None:
    """The transaction applies the tenant settings before the first statement and set_scope rewrites them."""
    alpha = await create_practice("Alpha")
    beta = await create_practice("Beta")
    alpha_id, beta_id = uuid.UUID(alpha.organization_id), uuid.UUID(beta.organization_id)
    container = env().container
    assert container is not None
    async with container.database.transaction(Scope(organization_id=alpha_id)) as tx:
        row = await tx.fetchrow("SELECT count(*), count(DISTINCT organization_id) FROM patient.patients")
        assert row is not None
        visible, organizations = row[0], row[1]
        assert visible == 1 and organizations == 1, (
            f"alpha's scope sees {visible} patients in {organizations} organizations"
        )
        await tx.set_scope(Scope(organization_id=beta_id))
        owner = await tx.fetchval("SELECT organization_id FROM patient.patients")
        assert owner == beta_id, f"after switching the scope the transaction sees {owner}"
        await tx.set_scope(Scope())


async def test_search_input_is_treated_as_data_not_sql() -> None:
    """Search input is parameterized."""
    practice = await create_practice("Rivera")
    injection = read(
        await practice.owner.get(
            "/api/v1/patients?search=" + quote("x' OR '1'='1'; DROP TABLE patient.patients; --", safe="")
        ),
        200,
    )
    assert len(injection["items"]) == 0, "the injection matches nothing"
    every = read(await practice.owner.get("/api/v1/patients"), 200)
    assert every["totalItems"] == 1, f"expected one patient, got {every['totalItems']}"


def base64_url(data: bytes) -> str:
    """Encode bytes as unpadded base64url."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def base64_url_decode(text: str) -> bytes:
    """Decode unpadded base64url text."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


async def test_forged_or_tampered_access_tokens_are_rejected() -> None:
    """Signature, algorithm and format checks reject forged tokens."""
    user = await create_user("token")
    parts = user.access_token.split(".")
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(
        {
            "iss": "healthcare-platform",
            "aud": "healthcare-api",
            "sub": user.user_id,
            "sid": str(uuid.uuid4()),
            "exp": int(time.time()) + 300,
        },
        other_key,
        algorithm="RS256",
    )
    payload = base64_url_decode(parts[1]).decode("utf-8")
    tampered = base64_url(payload.replace(user.user_id, str(uuid.uuid4())).encode("utf-8"))
    unsigned = base64_url(json.dumps({"alg": "none", "typ": "JWT"}, separators=(",", ":")).encode("utf-8"))
    for token in (forged, f"{unsigned}.{parts[1]}.", f"{parts[0]}.{tampered}.{parts[2]}", "garbage"):
        client = new_client("")
        client.access_token = token
        problem(await client.get("/api/v1/auth/me"), 401, "unauthorized")


async def test_responses_carry_security_headers_and_are_not_cacheable() -> None:
    """Responses carry the security headers and no server banner."""
    user = await create_user("headers")
    response = await user.get("/api/v1/auth/me")
    headers = response.headers
    assert headers.get("Cache-Control") == "no-store", headers
    assert headers.get("X-Content-Type-Options") == "nosniff", headers
    assert headers.get("X-Frame-Options") == "DENY", headers
    assert (headers.get("Server") or "") == "", headers


async def test_authentication_endpoints_are_rate_limited() -> None:
    """Authentication endpoints are limited per address."""
    container = env().build({"RateLimiting:Enabled": "true", "RateLimiting:AuthPerMinute": "3"})
    statuses: dict[int, int] = {}
    try:
        transport = httpx.ASGITransport(app=create_app(container, start_workers=False))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
            for _ in range(5):
                response = await http.post(
                    "/api/v1/auth/login",
                    json={
                        "email": "limit@example.test",
                        "password": "Wrong-Password-1",
                    },
                )
                statuses[response.status_code] = statuses.get(response.status_code, 0) + 1
                if response.status_code == 429:
                    assert response.headers.get("Retry-After") == "60", response.headers
    finally:
        await container.close()
    assert statuses.get(401) == 3 and statuses.get(429) == 2, f"unexpected statuses {statuses}"
