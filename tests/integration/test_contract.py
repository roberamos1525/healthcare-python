"""Contract scenarios: the served OpenAPI document, its operations, security and response schemas."""

import re
import uuid
from pathlib import Path
from typing import Any

import yaml

from tests.integration.support import (
    book,
    create_practice,
    create_user,
    expect,
    local_date,
    new_client,
    sign_in,
    slots,
)

ANONYMOUS_OPERATIONS: list[str] = [
    "post /api/v1/auth/register",
    "post /api/v1/auth/verify-email",
    "post /api/v1/auth/resend-verification",
    "post /api/v1/auth/login",
    "post /api/v1/auth/mfa/challenge",
    "post /api/v1/auth/refresh",
    "post /api/v1/auth/password/forgot",
    "post /api/v1/auth/password/reset",
]
CONTRACT_METHODS: list[str] = ["get", "post", "put", "delete", "patch"]
_PATH_PARAMETER = re.compile(r"\{[^}]+\}")


def contract_path() -> Path:
    """Return the path of the committed docs/openapi.yaml."""
    return Path(__file__).resolve().parents[2] / "docs" / "openapi.yaml"


def normalize(text: str) -> str:
    """Make the comparison independent of line endings."""
    return text.replace("\r\n", "\n").rstrip(" \t\r\n") + "\n"


def as_map(node: Any) -> dict[str, Any]:
    """Return a YAML node as a mapping (empty when it is not one)."""
    return node if isinstance(node, dict) else {}


class Operation:
    """One documented operation."""

    def __init__(self, method: str, path: str, spec: dict[str, Any]) -> None:
        """Store the method, the templated path and the operation object."""
        self.method = method
        self.path = path
        self.spec = spec

    def key(self) -> str:
        """Return "method path"."""
        return f"{self.method} {self.path}"


def schema_types(schema: dict[str, Any]) -> list[str]:
    """Return the declared JSON types of a schema."""
    value = schema.get("type")
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def allows_null(schema: dict[str, Any]) -> bool:
    """Report whether the schema accepts null."""
    return "null" in schema_types(schema) or "nullable" in schema


def is_null_only(schema: dict[str, Any]) -> bool:
    """Report whether a schema only accepts null."""
    return schema_types(schema) == ["null"]


def type_name(value: Any) -> str:
    """Return a readable name of a JSON value's type."""
    return type(value).__name__


class Contract:
    """The parsed OpenAPI document with a small structural validator."""

    def __init__(self, root: dict[str, Any]) -> None:
        """Keep the parsed document."""
        self.root = root

    def operations(self) -> list[Operation]:
        """Return every operation sorted by path and method."""
        result = [
            Operation(method, path, as_map(spec))
            for path, item in as_map(self.root.get("paths")).items()
            for method, spec in as_map(item).items()
            if method in CONTRACT_METHODS
        ]
        return sorted(result, key=lambda op: op.key())

    def schema(self, name: str) -> dict[str, Any]:
        """Return a named component schema."""
        return as_map(as_map(as_map(self.root.get("components")).get("schemas")).get(name))

    def response_schema(self, path: str, method: str, status: str) -> dict[str, Any]:
        """Return the JSON schema of a documented response."""
        spec = as_map(as_map(as_map(self.root.get("paths")).get(path)).get(method))
        response = as_map(as_map(spec.get("responses")).get(status))
        return as_map(as_map(as_map(response.get("content")).get("application/json")).get("schema"))

    def resolve(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Follow a $ref, or the first non-null branch of oneOf/anyOf/allOf, to the concrete schema."""
        reference = schema.get("$ref")
        if isinstance(reference, str):
            return self.resolve(self.schema(reference.split("/")[-1]))
        for keyword in ("oneOf", "anyOf", "allOf"):
            branches = schema.get(keyword)
            if not isinstance(branches, list):
                continue
            concrete: dict[str, Any] | None = None
            has_null = False
            for branch in branches:
                if is_null_only(as_map(branch)):
                    has_null = True
                elif concrete is None:
                    concrete = as_map(branch)
            if concrete is not None:
                resolved = dict(self.resolve(concrete))
                if has_null:
                    resolved["nullable"] = True
                return resolved
        return schema

    def validate(self, value: Any, schema: dict[str, Any], location: str, errors: list[str]) -> None:
        """Check the documented type, the required non-nullable properties and undocumented properties."""
        schema = self.resolve(schema)
        if value is None:
            if not allows_null(schema):
                errors.append(f"{location}: null is not allowed.")
            return
        declared = schema_types(schema)
        if "object" in declared or "properties" in schema:
            self._validate_object(value, schema, location, errors)
        elif "array" in declared:
            if not isinstance(value, list):
                errors.append(f"{location}: expected an array but found {type_name(value)}.")
                return
            for index, item in enumerate(value):
                self.validate(item, as_map(schema.get("items")), f"{location}[{index}]", errors)
        elif "string" in declared:
            if not isinstance(value, str):
                errors.append(f"{location}: expected a string but found {type_name(value)}.")
                return
            allowed = schema.get("enum")
            if isinstance(allowed, list) and not any(str(item) == value for item in allowed):
                errors.append(f"{location}: '{value}' is not one of the documented values.")
        elif "integer" in declared or "number" in declared:
            if isinstance(value, bool) or not isinstance(value, int | float):
                errors.append(f"{location}: expected a number but found {type_name(value)}.")
        elif "boolean" in declared and not isinstance(value, bool):
            errors.append(f"{location}: expected a boolean but found {type_name(value)}.")

    def _validate_object(self, value: Any, schema: dict[str, Any], location: str, errors: list[str]) -> None:
        """Validate the properties of an object."""
        if not isinstance(value, dict):
            errors.append(f"{location}: expected an object but found {type_name(value)}.")
            return
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return
        required_list = schema.get("required")
        required = [str(item) for item in required_list] if isinstance(required_list, list) else []
        for name, item in value.items():
            if name not in properties:
                errors.append(f"{location}.{name}: the property is not documented.")
                continue
            self.validate(item, as_map(properties[name]), f"{location}.{name}", errors)
        for name, property_schema in properties.items():
            if name not in value and name in required and not allows_null(self.resolve(as_map(property_schema))):
                errors.append(f"{location}.{name}: the required property is missing.")


def load_contract() -> Contract:
    """Parse the committed document."""
    root = yaml.safe_load(contract_path().read_text(encoding="utf-8"))
    assert isinstance(root, dict), "the contract is not a mapping"
    return Contract(root)


def concrete_path(path: str) -> str:
    """Fill every path parameter with a random identifier."""
    return _PATH_PARAMETER.sub(lambda _: str(uuid.uuid4()), path)


def request_body(method: str) -> dict[str, Any] | None:
    """Return an empty JSON object for POST and PUT, no body otherwise."""
    return {} if method in ("post", "put") else None


async def test_the_served_contract_matches_the_committed_openapi_document() -> None:
    """The served document is the committed one."""
    response = expect(await new_client("").get("/openapi/v1.yaml"), 200)
    committed = contract_path().read_bytes().decode("utf-8")
    assert normalize(response.body.decode("utf-8")) == normalize(committed), (
        "the served contract differs from docs/openapi.yaml"
    )


async def test_every_operation_declares_errors_and_authentication() -> None:
    """Every operation documents its errors and security."""
    operations = load_contract().operations()
    assert len(operations) >= 80, f"expected the full API surface, found {len(operations)} operations"
    failures: list[str] = []
    for op in operations:
        if "default" not in as_map(op.spec.get("responses")):
            failures.append(f"{op.key()} does not document its error response")
        secured = "security" in op.spec
        if secured == (op.key() in ANONYMOUS_OPERATIONS):
            failures.append(f"{op.key()} has an unexpected security requirement")
    assert not failures, "\n".join(failures)


async def test_every_documented_operation_is_implemented() -> None:
    """Each operation reaches a handler: an unmatched route answers with an empty 404, an implemented one not."""
    operations = load_contract().operations()
    user = await create_user("contract")
    user.organization = str(uuid.uuid4())
    implemented = 0
    failures: list[str] = []
    for op in operations:
        body = request_body(op.method)
        path = concrete_path(op.path)
        response = await user.send(op.method.upper(), path, body)
        if response.status == 401 and op.key() not in ANONYMOUS_OPERATIONS:
            # An earlier operation (logout, session revocation) ended the session: sign in again and retry.
            await sign_in(user)
            response = await user.send(op.method.upper(), path, body)
        if response.status == 404 and len(response.body) == 0:
            failures.append(f"{op.key()} is not implemented (unmatched route)")
            continue
        if response.status == 405:
            failures.append(f"{op.key()} answers 405")
            continue
        content_type = response.headers.get("content-type", "")
        if response.status >= 400 and not content_type.startswith("application/problem+json"):
            failures.append(f"{op.key()} answered {response.status} without a problem document")
            continue
        implemented += 1
    assert not failures, "\n".join(failures)
    assert implemented == len(operations), f"{implemented} of {len(operations)} operations are implemented"


async def test_every_protected_operation_requires_a_bearer_token() -> None:
    """Only the documented anonymous operations can be called without a token."""
    anonymous = new_client("")
    failures: list[str] = []
    for op in load_contract().operations():
        if op.key() in ANONYMOUS_OPERATIONS:
            continue
        response = await anonymous.send(
            op.method.upper(), concrete_path(op.path), request_body(op.method), {"X-Organization-Id": str(uuid.uuid4())}
        )
        if response.status != 401:
            failures.append(f"{op.key()} answered {response.status} without a token")
            continue
        try:
            code = response.json().get("code")
        except ValueError:
            code = None
        if code != "unauthorized" or response.headers.get("www-authenticate") != "Bearer":
            failures.append(f"{op.key()} did not answer with the unauthorized problem: {response.body!r}")
    assert not failures, "\n".join(failures)


async def test_responses_conform_to_the_documented_schemas() -> None:
    """Real responses validate against the documented schemas."""
    contract = load_contract()
    practice = await create_practice("Rivera")
    owner = practice.owner
    appointment = await book(owner, practice, (await slots(owner, practice, 5))[0])
    day = local_date(practice.time_zone, 5)
    organization = f"/api/v1/organizations/{practice.organization_id}"
    samples: list[tuple[str, str]] = [
        ("/api/v1/auth/me", "/api/v1/auth/me"),
        ("/api/v1/auth/sessions", "/api/v1/auth/sessions"),
        ("/api/v1/organizations/{organizationId}", organization),
        ("/api/v1/organizations/{organizationId}/settings", f"{organization}/settings"),
        ("/api/v1/organizations/{organizationId}/members", f"{organization}/members"),
        ("/api/v1/clinics", "/api/v1/clinics"),
        ("/api/v1/locations/{locationId}", f"/api/v1/locations/{practice.location_id}"),
        ("/api/v1/providers", "/api/v1/providers"),
        ("/api/v1/providers/{providerId}", f"/api/v1/providers/{practice.provider_id}"),
        ("/api/v1/provider-schedules", f"/api/v1/provider-schedules?providerId={practice.provider_id}"),
        ("/api/v1/patients", "/api/v1/patients"),
        ("/api/v1/patients/{patientId}", f"/api/v1/patients/{practice.patient_id}"),
        ("/api/v1/appointments", "/api/v1/appointments"),
        ("/api/v1/appointments/{appointmentId}", f"/api/v1/appointments/{appointment['id']}"),
        ("/api/v1/appointments/{appointmentId}/history", f"/api/v1/appointments/{appointment['id']}/history"),
        ("/api/v1/availability", f"/api/v1/availability?providerId={practice.provider_id}&from={day}&to={day}"),
        ("/api/v1/audit-events", "/api/v1/audit-events"),
        ("/api/v1/directory/providers", "/api/v1/directory/providers"),
    ]
    errors: list[str] = []
    for documented, concrete in samples:
        value = expect(await owner.get(concrete), 200).json()
        contract.validate(value, contract.response_schema(documented, "get", "200"), f"GET {documented}", errors)
    assert not errors, "\n".join(errors)


async def test_error_responses_conform_to_the_problem_schema() -> None:
    """A problem document validates against the Problem schema."""
    contract = load_contract()
    response = expect(await new_client("").post("/api/v1/auth/register", {"email": "invalid"}), 400)
    errors: list[str] = []
    contract.validate(response.json(), contract.schema("Problem"), "problem", errors)
    assert not errors, "\n".join(errors)
