"""Application core: errors, validation messages, paging, sorting, LIKE escaping, reference JSON and the caller."""

import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from healthcare.application.core.caller import SYSTEM_SCOPE, Caller, Scope, TenantAccess
from healthcare.application.core.errors import (
    AppError,
    ErrorKind,
    FieldErrors,
    ensure_version,
    from_domain,
    not_found,
    require_version,
)
from healthcare.application.core.jsonref import (
    format_offset,
    format_utc,
    parse_instant,
    reference_json,
    reference_string,
    utf16_length,
)
from healthcare.application.core.models import ApiModel
from healthcare.application.core.paging import (
    MAX_PAGE_SIZE,
    PageRequest,
    SortField,
    like_contains,
    like_starts_with,
    paged,
    parse_sort,
)
from healthcare.application.core.ports import new_id, system_clock
from healthcare.application.core.validation import (
    DURATION_MESSAGE,
    PHONE_MESSAGE,
    STATE_MESSAGE,
    TIME_ZONE_MESSAGE,
    ZIP_MESSAGE,
    Validator,
    display_name,
    is_email,
    is_us_state,
    not_empty_message,
)
from healthcare.domain.rules import DomainError, DomainErrorKind


def app_error(action: Callable[[], object]) -> AppError:
    """Run a callable and return the application error it raises."""
    with pytest.raises(AppError) as raised:
        action()
    return raised.value


def field_messages(action: Callable[[], object]) -> dict[str, list[str]]:
    """Run a callable and return the field errors of the validation error it raises."""
    error = app_error(action)
    assert error.code == "validation_failed"
    assert error.status == 400
    assert error.fields is not None
    return error.fields.as_dict()


# ---------------------------------------------------------------------------------------------------------
# Sorting, paging and LIKE patterns
# ---------------------------------------------------------------------------------------------------------

ALLOWED = ["startsAt", "createdAt"]


def test_parses_sort_expressions_case_insensitively_with_descending_prefixes() -> None:
    """Names are matched ignoring case and reported with the allowed spelling."""
    assert parse_sort("-startsat,createdAt", ALLOWED, "startsAt") == [
        SortField("startsAt", True),
        SortField("createdAt", False),
    ]
    assert parse_sort(" createdat , -STARTSAT ", ALLOWED, "startsAt") == [
        SortField("createdAt", False),
        SortField("startsAt", True),
    ]


@pytest.mark.parametrize("expression", [None, "", "   "])
def test_a_missing_sort_uses_the_default(expression: str | None) -> None:
    """Absent or blank sort expressions fall back to the default field ascending."""
    assert parse_sort(expression, ALLOWED, "startsAt") == [SortField("startsAt", False)]


def test_empty_sort_segments_are_ignored() -> None:
    """Stray commas do not produce fields."""
    assert parse_sort("createdAt,,", ALLOWED, "startsAt") == [SortField("createdAt")]
    assert parse_sort(",", ALLOWED, "startsAt") == [SortField("startsAt")]


def test_rejects_sorting_by_an_unknown_field() -> None:
    """Unknown fields are a validation error listing the allowed fields."""
    fields = field_messages(lambda: parse_sort("password", ALLOWED, "startsAt"))
    assert fields == {"sort": ["Sorting by 'password' is not supported. Allowed fields: startsAt, createdAt."]}
    fields = field_messages(lambda: parse_sort("createdAt,-secret", ALLOWED, "startsAt"))
    assert fields == {"sort": ["Sorting by 'secret' is not supported. Allowed fields: startsAt, createdAt."]}


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        (like_contains("O'Brien%_"), "%o'brien\\%\\_%"),
        (like_contains("a\\b"), "%a\\\\b%"),
        (like_starts_with(" Smi "), "smi%"),
        (like_starts_with("100%"), "100\\%%"),
        (like_contains(""), "%%"),
        (like_starts_with("_x_"), "\\_x\\_%"),
    ],
)
def test_like_patterns_escape_wildcards_and_lowercase_the_text(actual: str, expected: str) -> None:
    """LIKE wildcards and the escape character are matched literally."""
    assert actual == expected


def test_page_requests_are_clamped_to_safe_values() -> None:
    """Page numbers start at 1 and sizes stay within 1..100."""
    page = PageRequest(page=-3, page_size=5000)
    assert page.safe_page == 1
    assert page.safe_page_size == MAX_PAGE_SIZE == 100
    assert PageRequest(page=2, page_size=0).safe_page_size == 1
    assert PageRequest(page=3, page_size=20).offset == 40
    assert PageRequest() == PageRequest(1, 25)


def test_pages_report_the_total_number_of_pages() -> None:
    """The page count rounds up and an empty page has an empty item list."""
    result = paged([1, 2], PageRequest(page=1, page_size=2), 5)
    assert result.total_pages == 3
    assert result.to_data() == {"items": [1, 2], "page": 1, "pageSize": 2, "totalItems": 5, "totalPages": 3}
    empty = paged(list[int](), PageRequest(), 0)
    assert empty.items == []
    assert empty.total_pages == 0
    assert paged([1], PageRequest(page=0, page_size=500), 1).to_data()["pageSize"] == 100


# ---------------------------------------------------------------------------------------------------------
# Reference JSON
# ---------------------------------------------------------------------------------------------------------


def unescape_marks(text: str) -> str:
    """Replace '#' with a backslash so expected escapes stay readable."""
    return text.replace("#", "\\")


class SampleModel(ApiModel):
    """A response model with an optional member."""

    name: str
    note: str | None = None
    count: int


def test_reference_json_escapes_like_the_reference_serializer() -> None:
    """HTML-sensitive and non-ASCII characters are upper-case escapes and null members are omitted."""
    data = SampleModel(name='O\'Brien & <Sons> "+" é 😀', count=3).to_json()
    expected = unescape_marks(
        '{"name":"O#u0027Brien #u0026 #u003CSons#u003E #u0022#u002B#u0022 #u00E9 #uD83D#uDE00","count":3}'
    )
    assert data == expected


def test_reference_json_keeps_backslashes_control_escapes_and_member_order() -> None:
    """Backslashes and newlines keep their short escapes; the backtick and DEL are escaped."""
    assert reference_json({"b": "a\\u0041\n`\x7f", "a": 1}) == unescape_marks('{"b":"a##u0041#n#u0060#u007F","a":1}')
    assert reference_string("tab\there") == unescape_marks('"tab#there"')


def test_reference_json_writes_nested_values_compactly() -> None:
    """Nested lists, objects, nulls and booleans are written without spaces."""
    value = {"a": [1, 2.5, {"b": None, "c": True}], "d": {}}
    assert reference_json(value) == '{"a":[1,2.5,{"b":null,"c":true}],"d":{}}'
    assert json.loads(reference_json(value)) == value


def test_utf16_length_counts_surrogate_pairs() -> None:
    """Characters outside the basic plane count twice."""
    assert utf16_length("a😀é") == 4
    assert utf16_length("") == 0


def test_instants_use_the_reference_formats() -> None:
    """API instants end in Z with trimmed fractions; stored instants use a +00:00 offset."""
    instant = datetime(2026, 11, 22, 15, 0, 0, 123400, tzinfo=UTC)
    assert format_utc(instant) == "2026-11-22T15:00:00.1234Z"
    assert format_offset(instant.replace(microsecond=0)) == "2026-11-22T15:00:00+00:00"
    assert format_offset(instant) == "2026-11-22T15:00:00.1234+00:00"
    central = timezone(timedelta(hours=-6))
    assert format_utc(datetime(2026, 11, 22, 9, 0, tzinfo=central)) == "2026-11-22T15:00:00Z"
    assert format_offset(datetime(2026, 11, 22, 9, 0, tzinfo=central)) == "2026-11-22T15:00:00+00:00"
    assert format_utc(datetime(5, 1, 2, 3, 4, 5, tzinfo=UTC)) == "0005-01-02T03:04:05Z"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-22T09:00:00-06:00", datetime(2026, 11, 22, 15, 0, tzinfo=UTC)),
        ("2026-11-22T15:00:00", datetime(2026, 11, 22, 15, 0, tzinfo=UTC)),
        ("2026-11-22", datetime(2026, 11, 22, 0, 0, tzinfo=UTC)),
        ("2026-11-22T15:00:00.5Z", datetime(2026, 11, 22, 15, 0, 0, 500000, tzinfo=UTC)),
        ("2026-11-22T15:00:00.123456789+01:30", datetime(2026, 11, 22, 13, 30, 0, 123456, tzinfo=UTC)),
        ("2026-11-22T15:00", datetime(2026, 11, 22, 15, 0, tzinfo=UTC)),
    ],
)
def test_parses_iso_instants(text: str, expected: datetime) -> None:
    """Offsets are applied, missing offsets mean UTC and a date alone is midnight UTC."""
    assert parse_instant(text) == expected


@pytest.mark.parametrize("text", ["yesterday", "2026-13-01", "2026-11-22T25:00:00Z", "2026-11-22T15:00:00+24:00", ""])
def test_rejects_text_that_is_not_an_instant(text: str) -> None:
    """Free text and impossible values are not instants."""
    assert parse_instant(text) is None


# ---------------------------------------------------------------------------------------------------------
# Validation rules and messages
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("dateOfBirth", "Date of birth"),
        ("firstName", "First name"),
        ("address.postalCode", "Postal code"),
        ("email", "Email"),
        ("specialties[0].name", "Name"),
        ("line1", "Line1"),
        ("pageSize", "Page size"),
    ],
)
def test_display_names_follow_the_reference_convention(path: str, expected: str) -> None:
    """The last path segment is split into words with only the first capitalized."""
    assert display_name(path) == expected


def test_the_not_empty_message_quotes_the_display_name() -> None:
    """The reference message for empty values."""
    assert not_empty_message("firstName") == "'First name' must not be empty."


def test_validators_collect_errors_in_order_with_the_reference_messages() -> None:
    """Every rule runs and fields are reported in the order they first failed."""
    v = Validator()
    v.text("firstName", "").not_empty().max_length(100)
    v.text("lastName", "abcdefghijk").max_length(10)
    v.text("email", "not-an-email").optional_email()
    v.between("pageSize", 500, 1, 100)
    fields = field_messages(v.raise_if_failed)
    assert fields == {
        "firstName": ["'First name' must not be empty."],
        "lastName": ["The length of 'Last name' must be 10 characters or fewer. You entered 11 characters."],
        "email": ["'Email' is not a valid email address."],
        "pageSize": ["'Page size' must be between 1 and 100. You entered 500."],
    }
    Validator().raise_if_failed()


def test_messages_are_not_repeated_for_a_field() -> None:
    """The same message is recorded once while different messages accumulate."""
    v = Validator()
    v.text("name", " ").not_empty().not_empty()
    v.add("name", "second")
    assert v.errors.as_dict() == {"name": ["'Name' must not be empty.", "second"]}
    assert len(v.errors) == 1


def test_missing_values_pass_the_optional_text_rules() -> None:
    """None means the property was absent; only not_empty rejects it."""
    v = Validator()
    v.text("phone", None).phone()
    v.text("email", None).optional_email()
    v.text("reason", None).max_length(1).min_length(5, "Too short.")
    v.duration("minutes", None)
    assert len(v.errors) == 0
    v.text("name", None).not_empty()
    assert v.errors.as_dict() == {"name": ["'Name' must not be empty."]}


def test_lengths_are_counted_in_utf16_units() -> None:
    """An emoji counts as two characters, as in the reference."""
    v = Validator()
    v.text("name", "😀😀").max_length(3)
    assert v.errors.as_dict() == {
        "name": ["The length of 'Name' must be 3 characters or fewer. You entered 4 characters."],
    }


@pytest.mark.parametrize(
    ("phone", "valid"),
    [
        ("+1 (555) 010-0199", True),
        ("555.010.0199", True),
        ("5550100", True),
        ("555-01", False),
        ("call me", False),
        ("555 010 0199\n", True),
    ],
)
def test_phone_numbers_follow_the_reference_pattern(phone: str, valid: bool) -> None:
    """Phone numbers are 7-30 digits, spaces and punctuation with an optional leading plus."""
    v = Validator()
    v.text("phone", phone).phone()
    assert v.errors.as_dict() == ({} if valid else {"phone": [PHONE_MESSAGE]})


def test_phone_numbers_are_limited_to_30_characters() -> None:
    """A plus followed by 30 digits matches the pattern but exceeds the length limit."""
    v = Validator()
    v.text("phone", "+" + "1" * 30).phone()
    assert v.errors.as_dict() == {
        "phone": ["The length of 'Phone' must be 30 characters or fewer. You entered 31 characters."],
    }


@pytest.mark.parametrize(
    ("code", "valid"), [("75001", True), ("75001-1234", True), ("7500", False), ("75001-12", False), ("ABCDE", False)]
)
def test_postal_codes_are_zip_or_zip_plus_four(code: str, valid: bool) -> None:
    """ZIP codes are five digits with an optional four-digit extension."""
    v = Validator()
    v.text("postalCode", code).postal_code()
    assert v.errors.as_dict() == ({} if valid else {"postalCode": [ZIP_MESSAGE]})


@pytest.mark.parametrize(("code", "expected"), [("TX", True), ("PR", True), ("DC", True), ("tx", False), ("ZZ", False)])
def test_validates_us_state_codes(code: str, expected: bool) -> None:
    """US states and territories use upper-case two-letter codes."""
    assert is_us_state(code) is expected


def test_state_and_time_zone_rules_report_both_empty_and_invalid_values() -> None:
    """Empty values get the not-empty message, invalid ones the rule's own message."""
    v = Validator()
    v.text("state", "ZZ").us_state()
    v.text("timeZone", "Central Standard Time").time_zone()
    v.text("region", "").us_state()
    assert v.errors.as_dict() == {
        "state": [STATE_MESSAGE],
        "timeZone": [TIME_ZONE_MESSAGE],
        "region": ["'Region' must not be empty.", STATE_MESSAGE],
    }
    assert TIME_ZONE_MESSAGE == "Enter a valid IANA time zone, for example America/Chicago."


@pytest.mark.parametrize(
    ("minutes", "valid"),
    [(5, True), (480, True), (45, True), (0, False), (4, False), (481, False), (485, False), (32, False)],
)
def test_durations_are_5_to_480_minutes_in_5_minute_steps(minutes: int, valid: bool) -> None:
    """Appointment lengths follow the reference rule."""
    v = Validator()
    v.duration("durationMinutes", minutes)
    assert v.errors.as_dict() == ({} if valid else {"durationMinutes": [DURATION_MESSAGE]})


@pytest.mark.parametrize(
    ("text", "valid"),
    [
        ("jane@example.test", True),
        ("a@b", True),
        ("not-an-email", False),
        ("@example.test", False),
        ("jane@", False),
        ("a@b@example.test", False),
    ],
)
def test_emails_use_the_lenient_reference_check(text: str, valid: bool) -> None:
    """Exactly one '@' that is neither first nor last."""
    assert is_email(text) is valid


def test_numeric_and_identifier_rules_use_the_reference_messages() -> None:
    """greater_than and not_empty_id produce the FluentValidation wording."""
    v = Validator()
    v.greater_than("version", 0, 0)
    v.not_empty_id("patientId", UUID(int=0))
    v.not_empty_id("providerId", uuid4())
    v.ensure("other", True, "never")
    assert v.errors.as_dict() == {
        "version": ["'Version' must be greater than '0'."],
        "patientId": ["'Patient id' must not be empty."],
    }


# ---------------------------------------------------------------------------------------------------------
# Errors and versions
# ---------------------------------------------------------------------------------------------------------


def test_versions_are_checked_for_optimistic_concurrency() -> None:
    """A stale version conflicts and a missing version is a validation error."""
    ensure_version(3, 3)
    error = app_error(lambda: ensure_version(3, 2))
    assert (error.code, error.status) == ("concurrency_conflict", 409)
    assert require_version(4) == 4
    assert field_messages(lambda: require_version(None)) == {
        "version": ["The version of the record being updated is required."],
    }


def test_domain_errors_map_to_conflicts_or_rule_violations() -> None:
    """Conflicts become 409 and rule violations 422 with the same code and message."""
    conflict = from_domain(DomainError("invalid_status_transition", "No.", DomainErrorKind.CONFLICT))
    assert (conflict.status, conflict.code, conflict.message) == (409, "invalid_status_transition", "No.")
    rule = from_domain(DomainError("provider_not_verified", "Verify first."))
    assert (rule.status, rule.kind) == (422, ErrorKind.RULE)


def test_not_found_names_the_resource() -> None:
    """The 404 message names the missing resource."""
    error = not_found("patient")
    assert (error.status, error.code, error.message) == (404, "not_found", "The requested patient was not found.")


def test_field_errors_keep_insertion_order() -> None:
    """Fields are kept in the order they first failed."""
    errors = FieldErrors()
    errors.add("b", "1")
    errors.add("a", "2")
    errors.add("b", "3")
    assert list(errors.as_dict()) == ["b", "a"]
    assert errors.as_dict()["b"] == ["1", "3"]


# ---------------------------------------------------------------------------------------------------------
# The caller
# ---------------------------------------------------------------------------------------------------------


def staff_access(organization: UUID) -> TenantAccess:
    """Create read-only staff access to an organization."""
    return TenantAccess(organization, ["staff"], frozenset({"patients.read"}))


def test_the_caller_enforces_permissions_and_resets_access_for_another_organization() -> None:
    """Permissions apply to the selected organization only and denials keep their reason."""
    caller = Caller()
    organization = uuid4()
    caller.set_access(staff_access(organization))
    assert caller.organization_id == organization
    assert caller.system_scope().system
    assert not caller.scope().system
    assert caller.has("patients.read")
    assert caller.has_role("staff")
    assert app_error(lambda: caller.require("patients.write")).code == "forbidden"
    caller.require("patients.write", "patients.read")
    assert app_error(caller.require_user_id).code == "unauthorized"
    caller.set_organization(organization)
    assert caller.has("patients.read")
    caller.set_organization(uuid4())
    assert not caller.has("patients.read")
    caller.deny_access("not_a_member")
    error = app_error(lambda: caller.require("patients.read"))
    assert (error.code, error.status) == ("not_a_member", 403)


def test_an_anonymous_caller_must_authenticate_first() -> None:
    """Without a user or organization the caller is asked to sign in."""
    error = app_error(lambda: Caller().require("patients.read"))
    assert (error.code, error.status) == ("unauthorized", 401)


def test_a_signed_in_caller_without_an_organization_must_select_one() -> None:
    """The organization header is required once the user is known."""
    caller = Caller()
    caller.set_user(uuid4(), uuid4(), None)
    error = app_error(lambda: caller.require("patients.read"))
    assert (error.code, error.status) == ("organization_required", 400)


def test_platform_administrators_are_recognised() -> None:
    """Only the platform_admin role passes the platform check."""
    caller = Caller()
    assert app_error(caller.require_platform_admin).code == "unauthorized"
    caller.set_user(uuid4(), uuid4(), None)
    assert app_error(caller.require_platform_admin).code == "forbidden"
    caller.set_user(uuid4(), uuid4(), "platform_admin")
    caller.require_platform_admin()
    assert caller.is_platform_admin


def test_scopes_carry_the_organization_and_user() -> None:
    """The tenant scope and the system scope differ only in the system flag."""
    caller = Caller()
    user, organization = uuid4(), uuid4()
    caller.set_user(user, uuid4(), None)
    caller.set_organization(organization)
    assert caller.scope() == Scope(organization, user, False)
    assert caller.system_scope() == Scope(organization, user, True)
    assert Scope(None, None, True) == SYSTEM_SCOPE


def test_linked_patients_and_own_provider_profiles_are_recognised() -> None:
    """Patient links and the provider profile come from the resolved access."""
    caller = Caller()
    patient, provider = uuid4(), uuid4()
    assert caller.patient_ids == []
    assert caller.provider_id is None
    caller.set_access(TenantAccess(uuid4(), ["provider"], frozenset(), provider_id=provider, patient_ids=[patient]))
    assert caller.is_linked_patient(patient)
    assert not caller.is_linked_patient(uuid4())
    assert caller.is_own_provider(provider)
    assert not caller.is_own_provider(uuid4())


# ---------------------------------------------------------------------------------------------------------
# Identifiers and clock
# ---------------------------------------------------------------------------------------------------------


def test_new_identifiers_are_unique_time_ordered_version_7() -> None:
    """Identifiers are UUIDv7 values that sort by creation time."""
    first, second = new_id(), new_id()
    assert first.version == 7
    assert first != second
    assert first.int < second.int
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", str(first))


def test_the_system_clock_is_utc() -> None:
    """The clock returns an aware UTC instant."""
    now = system_clock()
    assert now.tzinfo is UTC
    assert now.date() >= date(2026, 1, 1)
