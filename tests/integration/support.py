"""Helpers of the integration suite: an HTTP client per test user and builders for realistic practices."""

import asyncio
import json
import re
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import httpx

from healthcare.domain.rules import is_valid_npi
from tests.integration.environment import Environment

DEFAULT_PASSWORD = "Synthetic-Test-Password-1"
_TOKEN = re.compile(r"token=([^\s&]+)")
_ENV: list[Environment] = []


def set_environment(env: Environment) -> None:
    """Remember the session's environment."""
    _ENV.clear()
    _ENV.append(env)


def env() -> Environment:
    """Return the session's environment."""
    return _ENV[0]


@dataclass
class Response:
    """A captured HTTP response."""

    status: int
    headers: httpx.Headers
    body: bytes

    def json(self) -> Any:
        """Decode the JSON body."""
        return json.loads(self.body)


@dataclass
class Client:
    """An HTTP client signed in as one test user, optionally scoped to an organization."""

    email: str
    password: str = DEFAULT_PASSWORD
    user_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    organization: str = ""
    _http: httpx.AsyncClient | None = field(default=None, repr=False)

    def _client(self) -> httpx.AsyncClient:
        """Return the underlying connection pool (shared by the session)."""
        return shared_http()

    async def send(
        self, method: str, path: str, body: Any = None, headers: dict[str, str | None] | None = None
    ) -> Response:
        """Issue a request with an optional JSON body (dict/list encoded, str/bytes sent as is) and headers."""
        request_headers: dict[str, str] = {}
        content: bytes | None = None
        if body is not None:
            if isinstance(body, bytes):
                content = body
            elif isinstance(body, str):
                content = body.encode("utf-8")
            else:
                content = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if self.access_token:
            request_headers["Authorization"] = f"Bearer {self.access_token}"
        if self.organization:
            request_headers["X-Organization-Id"] = self.organization
        for key, value in (headers or {}).items():
            matching = [name for name in request_headers if name.lower() == key.lower()]
            for name in matching:
                del request_headers[name]
            if value is not None:
                request_headers[key] = value
        response = await self._client().request(method, env().base_url + path, content=content, headers=request_headers)
        return Response(response.status_code, response.headers, response.content)

    async def get(self, path: str) -> Response:
        """Send GET."""
        return await self.send("GET", path)

    async def post(self, path: str, body: Any = None) -> Response:
        """Send POST with a JSON body (an empty object when None)."""
        return await self.send("POST", path, {} if body is None else body)

    async def put(self, path: str, body: Any) -> Response:
        """Send PUT with a JSON body."""
        return await self.send("PUT", path, body)

    async def delete(self, path: str) -> Response:
        """Send DELETE."""
        return await self.send("DELETE", path)


_HTTP: list[httpx.AsyncClient] = []


def shared_http() -> httpx.AsyncClient:
    """Return the HTTP connection pool of the test session."""
    if not _HTTP:
        _HTTP.append(httpx.AsyncClient(timeout=60, limits=httpx.Limits(max_connections=200)))
    return _HTTP[0]


def expect(response: Response, status: int) -> Response:
    """Fail unless the response has the status."""
    assert response.status == status, (
        f"expected {status} but received {response.status}: {response.body.decode(errors='replace')}"
    )
    return response


def read(response: Response, status: int) -> Any:
    """Require the status and decode the JSON body."""
    return expect(response, status).json()


def problem(response: Response, status: int, code: str) -> dict[str, Any]:
    """Require a problem response with the status and code and return it."""
    expect(response, status)
    content_type = response.headers.get("content-type", "")
    assert content_type.startswith("application/problem+json"), f"expected a problem document but got {content_type!r}"
    document: dict[str, Any] = response.json()
    assert document["code"] == code and document["status"] == status, (
        f"expected {status} {code} but got {document['status']} {document['code']} ({document.get('detail')})"
    )
    return document


def unique_email(prefix: str) -> str:
    """Return a unique synthetic address on the reserved example.test domain."""
    return f"{prefix}.{uuid.uuid4().hex}@example.test"


def extract_token(body: str) -> str:
    """Return the single-use token of the link in an email body."""
    match = _TOKEN.search(body)
    assert match is not None, "the email does not contain a token link"
    return unquote(match.group(1))


def new_client(email: str) -> Client:
    """Return an anonymous client for an address."""
    return Client(email=email)


async def register(client: Client, first_name: str) -> None:
    """Register the client's account without verifying it."""
    expect(
        await client.post(
            "/api/v1/auth/register",
            {
                "email": client.email,
                "password": client.password,
                "firstName": first_name,
                "lastName": "Synthetic",
            },
        ),
        202,
    )


async def verify_email(client: Client) -> None:
    """Complete verification with the captured token."""
    message = await env().emails.wait_for(client.email, "Verify your email")
    expect(await client.post("/api/v1/auth/verify-email", {"token": extract_token(message.body)}), 204)


async def sign_in(client: Client) -> None:
    """Sign in and store the tokens on the client."""
    login = read(await client.post("/api/v1/auth/login", {"email": client.email, "password": client.password}), 200)
    assert not login["mfaRequired"], "unexpected MFA challenge"
    client.access_token, client.refresh_token, client.user_id = (
        login["accessToken"],
        login["refreshToken"],
        login["user"]["id"],
    )


async def create_user(prefix: str) -> Client:
    """Register, verify and sign in a new user."""
    client = new_client(unique_email(prefix))
    await register(client, prefix[:1].upper() + prefix[1:])
    await verify_email(client)
    await sign_in(client)
    return client


async def create_platform_admin() -> Client:
    """Give a new user the platform administrator role and sign them in again."""
    admin = await create_user("platform.admin")
    await env().execute(
        "UPDATE identity.users SET platform_role = 'platform_admin' WHERE id = $1", uuid.UUID(admin.user_id)
    )
    await sign_in(admin)
    return admin


async def create_organization(time_zone: str = "America/Chicago") -> tuple[Client, dict[str, Any]]:
    """Create an organization owned by a new user and select it on the owner's client."""
    owner = await create_user("owner")
    organization = read(
        await owner.post(
            "/api/v1/organizations",
            {
                "name": ("Synthetic Family Clinic " + uuid.uuid4().hex)[:40],
                "type": "clinic",
                "timeZone": time_zone,
                "contactEmail": "front.desk@example.test",
            },
        ),
        201,
    )
    owner.organization = organization["id"]
    return owner, organization


@dataclass
class Practice:
    """A practice: owner, clinic, location, active provider with weekly hours and a patient."""

    owner: Client
    organization_id: str
    clinic_id: str
    location_id: str
    provider_id: str
    patient_id: str
    time_zone: str


def random_npi() -> str:
    """Return a random, check-digit-valid synthetic NPI."""
    digits = f"1{secrets.randbelow(100_000_000):08d}"
    for check in range(10):
        if is_valid_npi(f"{digits}{check}"):
            return f"{digits}{check}"
    raise AssertionError("no NPI check digit")


def utc_today() -> date:
    """Return today's UTC date."""
    return datetime.now(UTC).date()


async def create_active_provider(admin: Client, location_id: str, last_name: str, user_id: str | None = None) -> str:
    """Create a provider with a verified license, activate it and add weekly hours 08:00-17:00 every day."""
    body: dict[str, Any] = {
        "firstName": "Elena",
        "lastName": last_name,
        "professionalTitle": "MD",
        "npi": random_npi(),
        "acceptingNewPatients": True,
        "listedInDirectory": True,
        "specialties": [{"name": "Family Medicine", "isPrimary": True}],
        "locationIds": [location_id],
    }
    if user_id is not None:
        body["userId"] = user_id
    provider = read(await admin.post("/api/v1/providers", body), 201)
    provider_id = provider["id"]
    credential = read(
        await admin.post(
            f"/api/v1/providers/{provider_id}/credentials",
            {
                "type": "medical_license",
                "issuingAuthority": "Texas Medical Board",
                "identifier": f"TEST-{uuid.uuid4().hex[:8]}",
                "expiresOn": (utc_today() + timedelta(days=730)).isoformat(),
            },
        ),
        201,
    )
    expect(
        await admin.post(
            f"/api/v1/providers/{provider_id}/credentials/{credential['id']}/review", {"decision": "verified"}
        ),
        200,
    )
    expect(await admin.post(f"/api/v1/providers/{provider_id}/activate"), 200)
    for day in range(7):
        expect(
            await admin.post(
                "/api/v1/provider-schedules",
                {
                    "providerId": provider_id,
                    "locationId": location_id,
                    "dayOfWeek": day,
                    "startTime": "08:00",
                    "endTime": "17:00",
                    "slotDurationMinutes": 30,
                    "effectiveFrom": (utc_today() - timedelta(days=1)).isoformat(),
                },
            ),
            201,
        )
    return str(provider_id)


async def create_patient(staff: Client, last_name: str) -> dict[str, Any]:
    """Register a synthetic patient through the staff endpoint."""
    patient: dict[str, Any] = read(
        await staff.post(
            "/api/v1/patients",
            {
                "firstName": "Jordan",
                "lastName": last_name,
                "dateOfBirth": "1984-05-17",
                "administrativeGender": "unknown",
                "email": unique_email("patient"),
                "phone": "+1 555 010 0100",
            },
        ),
        201,
    )
    return patient


async def create_practice(provider_last_name: str = "Practitioner") -> Practice:
    """Create an organization with a clinic, a location, an active provider and a patient."""
    owner, organization = await create_organization("America/Chicago")
    clinic = read(await owner.post("/api/v1/clinics", {"name": "Main Clinic", "status": "active"}), 201)
    location = read(
        await owner.post(
            f"/api/v1/clinics/{clinic['id']}/locations",
            {
                "name": "Downtown",
                "addressLine1": "100 Test Street",
                "city": "Austin",
                "state": "TX",
                "postalCode": "78701",
                "timeZone": "America/Chicago",
                "status": "active",
            },
        ),
        201,
    )
    provider_id = await create_active_provider(owner, location["id"], provider_last_name)
    patient = await create_patient(owner, "Sample")
    return Practice(
        owner, organization["id"], clinic["id"], location["id"], provider_id, patient["id"], "America/Chicago"
    )


async def add_member(owner: Client, organization_id: str, *roles: str) -> Client:
    """Invite a new user with the roles and have them accept; return their client scoped to the organization."""
    member = await create_user(roles[0].replace("_", "."))
    expect(
        await owner.post(
            f"/api/v1/organizations/{organization_id}/invitations", {"email": member.email, "roles": list(roles)}
        ),
        201,
    )
    invitation = await env().emails.wait_for(member.email, "invited")
    expect(await member.post("/api/v1/invitations/accept", {"token": extract_token(invitation.body)}), 200)
    member.organization = organization_id
    return member


async def create_portal_patient(practice: Practice) -> tuple[Client, dict[str, Any]]:
    """Sign up a user and register them as a patient of the practice through the portal."""
    client = await create_user("portal")
    client.organization = practice.organization_id
    patient = read(
        await client.post(
            "/api/v1/patients/self-registration", {"dateOfBirth": "1990-02-03", "administrativeGender": "female"}
        ),
        201,
    )
    return client, patient


def local_date(time_zone: str, days_ahead: int) -> str:
    """Return the local date in the time zone a number of days from today."""
    return (datetime.now(ZoneInfo(time_zone)).date() + timedelta(days=days_ahead)).isoformat()


async def slots(client: Client, practice: Practice, days_ahead: int) -> list[dict[str, Any]]:
    """Return the open slots of the practice's provider on the local date a number of days from today."""
    day = local_date(practice.time_zone, days_ahead)
    availability = read(
        await client.get(f"/api/v1/availability?providerId={practice.provider_id}&from={day}&to={day}"), 200
    )
    result: list[dict[str, Any]] = availability["slots"]
    return result


def idempotency_key() -> str:
    """Return a fresh idempotency key."""
    return "test-" + uuid.uuid4().hex


async def book_raw(
    client: Client, practice: Practice, slot: dict[str, Any], patient_id: str = "", key: str = ""
) -> Response:
    """Send a booking request for a slot with a fresh or given idempotency key."""
    return await client.send(
        "POST",
        "/api/v1/appointments",
        {
            "patientId": patient_id or practice.patient_id,
            "providerId": practice.provider_id,
            "locationId": slot["locationId"],
            "startsAt": slot["startsAt"],
            "visitType": "in_person",
            "reason": "Annual check-up",
        },
        {"Idempotency-Key": key or idempotency_key()},
    )


async def book(client: Client, practice: Practice, slot: dict[str, Any], patient_id: str = "") -> dict[str, Any]:
    """Book a slot and require success."""
    appointment: dict[str, Any] = read(await book_raw(client, practice, slot, patient_id), 201)
    return appointment


async def parallel(count: int, action: Callable[[int], Awaitable[Response]]) -> list[Response]:
    """Run an action concurrently ``count`` times and return the responses."""
    return list(await asyncio.gather(*(action(index) for index in range(count))))


def parse_instant(text: str) -> datetime:
    """Parse an API timestamp (``Z`` suffix)."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))
