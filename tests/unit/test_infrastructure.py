"""Configuration, connection strings, error translation, cache helpers and HTTP pipeline helpers."""

import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy.exc import DBAPIError

from healthcare.api.context import FixedWindowLimiter
from healthcare.api.pipeline import incoming_trace_id, normalize_path
from healthcare.api.query import QueryParams, parse_guid
from healthcare.application.core.caller import Scope
from healthcare.application.core.errors import AppError
from healthcare.domain.enums import AppointmentStatus
from healthcare.infrastructure.cache import MemoryCache, redis_url
from healthcare.infrastructure.config import Values, load_settings
from healthcare.infrastructure.persistence.database import parse_connection_string, scope_parameters, translate


def test_settings_use_the_reference_defaults() -> None:
    """Without configuration the reference defaults apply."""
    settings = load_settings(Values({}))
    assert settings.environment == "Production" and settings.http_port == 8080
    assert settings.auth.access_token_minutes == 10 and settings.auth.password_hash_iterations == 600_000
    assert settings.auth.session_idle_timeout_minutes == 60 and settings.auth.session_absolute_lifetime_hours == 12
    assert settings.email.provider == "None" and settings.cors_allowed_origins == []
    assert settings.rate_limiting.general_per_minute == 600 and settings.rate_limiting.auth_per_minute == 20
    assert not settings.allows_temporary_keys


def test_settings_read_sectioned_keys_case_insensitively() -> None:
    """Section__Key variables, arrays and environment-dependent defaults are honoured."""
    settings = load_settings(
        Values(
            {
                "ASPNETCORE_ENVIRONMENT": "Development",
                "connectionstrings__database": "Host=db",
                "AUTH__ACCESSTOKENMINUTES": "5",
                "RateLimiting__Enabled": "false",
                "Cors__AllowedOrigins__1": "https://b.example.test",
                "Cors__AllowedOrigins__0": "https://a.example.test",
                "ASPNETCORE_HTTP_PORTS": "9090",
            }
        )
    )
    assert settings.database == "Host=db" and settings.auth.access_token_minutes == 5
    assert not settings.rate_limiting.enabled and settings.http_port == 9090
    assert settings.cors_allowed_origins == ["https://a.example.test", "https://b.example.test"]
    assert settings.email.provider == "File" and settings.allows_temporary_keys


def test_connection_strings_in_npgsql_and_url_form_are_parsed() -> None:
    """Both forms yield an asyncpg URL; the pool size and SSL mode are kept."""
    parsed = parse_connection_string(
        "Host=localhost;Port=54329;Database=healthcare;Username=healthcare_app;Password=p@ss;Maximum Pool Size=50;"
        "SSL Mode=VerifyFull"
    )
    assert parsed.url == "postgresql+asyncpg://healthcare_app:p%40ss@localhost:54329/healthcare"
    assert parsed.pool_size == 50 and parsed.ssl == "verify-full"
    url = parse_connection_string("postgresql://user:secret@db.example.test:5433/app")
    assert url.url == "postgresql+asyncpg://user:secret@db.example.test:5433/app" and url.pool_size == 100
    with pytest.raises(ValueError):
        parse_connection_string("  ")


def test_scopes_become_the_three_session_settings() -> None:
    """Tenant and system scopes are written as text settings."""
    organization, user = uuid.uuid4(), uuid.uuid4()
    assert scope_parameters(Scope(organization, user)) == {
        "organization": str(organization),
        "user": str(user),
        "scope": "tenant",
    }
    assert scope_parameters(Scope(system=True)) == {"organization": "", "user": "", "scope": "system"}


class _DriverError(Exception):
    """A driver error carrying a PostgreSQL code and constraint."""

    def __init__(self, sqlstate: str, constraint: str | None = None) -> None:
        """Create the error."""
        super().__init__(sqlstate)
        self.sqlstate = sqlstate
        self.constraint_name = constraint


@pytest.mark.parametrize(
    ("sqlstate", "constraint", "status", "code"),
    [
        ("23P01", None, 409, "slot_unavailable"),
        ("23505", "ux_users_email_normalized", 409, "email_in_use"),
        ("23505", "ux_provider_specialties_provider_name", 409, "duplicate_specialty"),
        ("23505", "some_other_index", 409, "duplicate_resource"),
        ("23503", None, 422, "invalid_reference"),
        ("23514", None, 422, "constraint_violation"),
        ("40P01", None, 409, "transient_conflict"),
        ("42501", None, 403, "forbidden"),
    ],
)
def test_constraint_violations_become_safe_problems(
    sqlstate: str, constraint: str | None, status: int, code: str
) -> None:
    """PostgreSQL errors map to the reference's codes without exposing constraint names."""
    error = DBAPIError("statement", {}, _DriverError(sqlstate, constraint))
    translated = translate(error)
    assert isinstance(translated, AppError) and translated.status == status and translated.code == code
    assert constraint is None or constraint not in translated.message


def test_other_database_errors_are_not_translated() -> None:
    """Unknown codes stay internal errors."""
    assert translate(DBAPIError("statement", {}, _DriverError("XX000"))) is None


def test_redis_connection_strings_are_converted_to_urls() -> None:
    """StackExchange-style strings become redis URLs."""
    assert redis_url("localhost:6379") == "redis://localhost:6379"
    assert redis_url("cache.example.test") == "redis://cache.example.test:6379"
    assert redis_url("cache:6380,password=s3cret,ssl=true,defaultDatabase=2") == "rediss://:s3cret@cache:6380/2"
    assert redis_url("redis://already") == "redis://already"


async def test_the_memory_cache_expires_and_removes_entries() -> None:
    """Entries are returned until removed or expired."""
    cache = MemoryCache()
    await cache.set("a", '{"active":true}', 60)
    await cache.set("b", "1", -1)
    assert await cache.get("a") == '{"active":true}' and await cache.get("b") is None
    await cache.remove("a")
    assert await cache.get("a") is None and await cache.probe()


def test_trace_ids_are_taken_from_valid_traceparent_headers_only() -> None:
    """Only a well-formed, non-zero W3C trace id is reused."""
    assert (
        incoming_trace_id("00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01")
        == "4bf92f3577b34da6a3ce929d0e0e4736"
    )
    for header in (None, "", "garbage", "00-00000000000000000000000000000000-00f067aa0ba902b7-01", "00-xyz-1-01"):
        assert incoming_trace_id(header) is None


def test_paths_match_case_insensitively_without_a_trailing_slash() -> None:
    """Literal segments ignore case and one trailing slash."""
    assert normalize_path("/API/V1/Auth/Me/") == "/api/v1/auth/me"
    assert normalize_path("/") == "/"


def test_the_rate_limiter_allows_the_permits_per_window_and_key() -> None:
    """Each key has its own fixed window."""
    limiter = FixedWindowLimiter(2)
    assert limiter.allow("a") and limiter.allow("a") and not limiter.allow("a")
    assert limiter.allow("b")
    expired = FixedWindowLimiter(1, window_seconds=0)
    assert expired.allow("a") and expired.allow("a")


def test_guids_are_accepted_in_the_reference_forms() -> None:
    """Hyphenated, compact and braced forms are accepted; anything else is not."""
    value = uuid.uuid4()
    for text in (str(value), value.hex, "{" + str(value) + "}", str(value).upper()):
        assert parse_guid(text) == value
    for text in (
        "not-a-guid",
        str(value)[:-1],
        value.hex + "0",
        "-".join(value.hex[i : i + 4] for i in range(0, 32, 4)),
    ):
        assert parse_guid(text) is None


def test_query_parameters_follow_the_reference_binding_rules() -> None:
    """Names are case-insensitive, the first value wins, blanks are absent and failures are collected."""
    query = QueryParams("PAGE=2&page=9&pageSize=+3&search=a%20b&status=Checked_In&empty=&flag=TRUE")
    assert query.page().page == 2 and query.page().page_size == 3
    assert query.text("search") == "a b" and query.text("empty") is None
    assert query.enum("status", AppointmentStatus) == AppointmentStatus.CHECKED_IN and query.flag("flag")
    query.finish()
    invalid = QueryParams("page=x&providerId=abc&from=yesterday&status=open&all=maybe")
    invalid.page()
    invalid.guid("providerId")
    invalid.date("from")
    invalid.enum("status", AppointmentStatus)
    invalid.flag("all")
    with pytest.raises(AppError) as failure:
        invalid.finish()
    assert failure.value.fields is not None
    assert failure.value.fields.as_dict() == {
        "page": ["The value 'x' is not valid for Page."],
        "providerId": ["The value 'abc' is not valid."],
        "from": ["The value 'yesterday' is not valid."],
        "status": ["Allowed values: pending, confirmed, checked_in, in_progress, completed, cancelled, no_show."],
        "all": ["The value 'maybe' is not valid."],
    }


def test_routes_resolve_like_the_reference_routing_tree() -> None:
    """Template literals win over GUID identifiers; 405 is decided before the GUID constraint."""
    from healthcare.api.context import RequestPreparer
    from healthcare.api.routing import Ctx, Router

    async def handler(ctx: Ctx) -> object:
        """Answer nothing (resolution only)."""
        raise AssertionError

    router = Router(cast(RequestPreparer, None), lambda: datetime.now(UTC))
    for method, path in (
        ("POST", "/api/v1/patients/self-registration"),
        ("GET", "/api/v1/patients/{patientId}"),
        ("PUT", "/api/v1/patients/{patientId}"),
    ):
        router.route(method, path)(cast(Any, handler))
    patient = str(uuid.uuid4())
    assert router.resolve("GET", f"/api/v1/patients/{patient}") is router.routes[1]
    assert router.resolve("GET", "/api/v1/patients/self-registration") == 405
    assert router.allowed_methods("/api/v1/patients/self-registration") == ["POST"]
    assert router.resolve("DELETE", "/api/v1/patients/not-a-guid") == 405
    assert router.allowed_methods("/api/v1/patients/not-a-guid") == ["GET", "PUT"]
    assert router.resolve("GET", "/api/v1/patients/not-a-guid") == 404
    assert router.resolve("GET", "/api/v1/nothing") == 404
