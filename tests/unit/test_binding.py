"""Request body binding in the reference's three stages: unreadable bodies, missing properties, request rules."""

from datetime import UTC, date, datetime, time
from uuid import UUID, uuid4

import pytest

from healthcare.api.binding import (
    EMPTY_BODY,
    INVALID_VALUE,
    MAX_BODY_BYTES,
    TIME_MESSAGE,
    fields_of,
    is_json_media_type,
    unreadable,
)
from healthcare.api.binding import bind_bytes as bind_model
from healthcare.application.core.errors import AppError
from healthcare.application.core.models import RequestModel
from healthcare.application.core.validation import Validator
from healthcare.application.scheduling.contracts import BookRequest
from healthcare.domain.calendar import MIN_DATE, MIN_INSTANT
from healthcare.domain.enums import AdministrativeGender, VisitType

TODAY = date(2027, 1, 11)
CHECKED: list[str] = []
REQUEST_ENTRY = "The request field is required."


class SampleItem(RequestModel):
    """A nested list element with a required text property."""

    name: str
    is_primary: bool


class SampleRequest(RequestModel):
    """A request exercising every conversion of the binder."""

    code: str
    id: UUID
    name: str | None = None
    count: int
    enabled: bool | None = None
    born: date
    opens: time
    at: datetime
    gender: AdministrativeGender
    tags: list[str] | None = None
    items: list[SampleItem] | None = None

    def check(self, v: Validator, today: date) -> None:
        """Record that stage 3 ran and reject the name "invalid"."""
        CHECKED.append(self.code)
        v.ensure("name", self.name != "invalid", "'Name' is not valid.")


def bind(body: str | bytes) -> SampleRequest:
    """Run the binder on a JSON text or raw bytes."""
    return bind_model(body.encode() if isinstance(body, str) else body, SampleRequest, TODAY)


def bind_error(body: str | bytes) -> dict[str, list[str]]:
    """Return the field errors of a body that fails to bind."""
    with pytest.raises(AppError) as raised:
        bind(body)
    error = raised.value
    assert error.code == "validation_failed"
    assert error.status == 400
    assert error.fields is not None
    return error.fields.as_dict()


@pytest.fixture(autouse=True)
def reset_checked() -> None:
    """Forget which requests ran their rules."""
    CHECKED.clear()


def test_binds_every_value_kind_and_runs_the_rules() -> None:
    """A valid body binds completely, names match case-insensitively, and stage 3 runs."""
    identifier = uuid4()
    request = bind(
        '{"CODE":"c1","ID":"' + str(identifier).upper() + '","name":"x","count":3,"enabled":true,"born":"1990-02-03",'
        '"opens":"08:30","at":"2027-01-11T09:00:00-06:00","gender":"Female","tags":["a",null],'
        '"items":[{"name":"A","isPrimary":true}]}'
    )
    assert request.code == "c1"
    assert request.id == identifier
    assert request.name == "x"
    assert request.count == 3
    assert request.enabled is True
    assert request.born == date(1990, 2, 3)
    assert request.opens == time(8, 30)
    assert request.at == datetime(2027, 1, 11, 15, 0, tzinfo=UTC)
    assert request.gender is AdministrativeGender.FEMALE
    assert request.tags == ["a", ""]
    assert request.items == [SampleItem.model_construct(name="A", is_primary=True)]
    assert CHECKED == ["c1"]


def test_absent_properties_take_the_reference_defaults() -> None:
    """Enumerations take their first value; value types take their zero values; optionals stay None."""
    request = bind('{"code":"c"}')
    assert request.gender is AdministrativeGender.FEMALE
    assert request.enabled is None
    assert request.name is None
    assert request.tags is None
    assert request.count == 0
    assert request.id == UUID(int=0)
    assert request.born == MIN_DATE
    assert request.at == MIN_INSTANT
    assert request.opens == time(0, 0)


@pytest.mark.parametrize(
    ("body", "path", "message"),
    [
        ("", "body", EMPTY_BODY),
        # Observed against the reference: only a body without bytes is "empty"; whitespace is unreadable JSON.
        ("   ", "body", INVALID_VALUE),
        ("null", "body", EMPTY_BODY),
        ("[1]", "body", INVALID_VALUE),
        ('"text"', "body", INVALID_VALUE),
        ('{"count":"3"}', "count", INVALID_VALUE),
        ('{"count":3.5}', "count", INVALID_VALUE),
        ('{"count":3.0}', "count", INVALID_VALUE),
        ('{"count":1e2}', "count", INVALID_VALUE),
        ('{"count":2147483648}', "count", INVALID_VALUE),
        ('{"count":-2147483649}', "count", INVALID_VALUE),
        ('{"count":true}', "count", INVALID_VALUE),
        ('{"enabled":"yes"}', "enabled", INVALID_VALUE),
        ('{"enabled":1}', "enabled", INVALID_VALUE),
        ('{"born":"1990-13-01"}', "born", INVALID_VALUE),
        ('{"born":"02/03/1990"}', "born", INVALID_VALUE),
        ('{"opens":"8am"}', "opens", TIME_MESSAGE),
        ('{"opens":null}', "opens", TIME_MESSAGE),
        ('{"opens":"24:00"}', "opens", TIME_MESSAGE),
        ('{"opens":8}', "opens", INVALID_VALUE),
        ('{"at":"tomorrow"}', "at", INVALID_VALUE),
        ('{"at":" 2027-01-11T09:00:00Z"}', "at", INVALID_VALUE),
        ('{"gender":"robot"}', "gender", INVALID_VALUE),
        ('{"gender":0}', "gender", INVALID_VALUE),
        ('{"id":"not-a-guid"}', "id", INVALID_VALUE),
        ('{"name":3}', "name", INVALID_VALUE),
        ('{"name":1.5}', "name", INVALID_VALUE),
        ('{"tags":"a"}', "tags", INVALID_VALUE),
        ('{"tags":[1]}', "tags[0]", INVALID_VALUE),
        ('{"items":[{"isPrimary":"x"}]}', "items[0].isPrimary", INVALID_VALUE),
        ('{"items":[{"name":"a"},5]}', "items[1]", INVALID_VALUE),
        ('{"name":"a"} trailing', "body", INVALID_VALUE),
        ('{"count":}', "count", INVALID_VALUE),
        ('{"tags":["a",}', "tags[1]", INVALID_VALUE),
        ('{"name":"a",}', "body", INVALID_VALUE),
        ('{"count":NaN}', "count", INVALID_VALUE),
    ],
)
def test_unreadable_bodies_are_reported_at_the_offending_path(body: str, path: str, message: str) -> None:
    """Stage 1 reports only the offending value plus the reference's request entry."""
    assert bind_error(body) == {path: [message], "request": [REQUEST_ENTRY]}
    assert CHECKED == []


def test_bodies_that_are_not_utf8_are_unreadable() -> None:
    """Invalid UTF-8 is reported at the document root."""
    assert bind_error(b'{"code":"\xff"}') == {"body": [INVALID_VALUE], "request": [REQUEST_ENTRY]}


def test_missing_required_properties_are_reported_before_the_rules() -> None:
    """Stage 2 reports absent or null required text, also inside list elements, and skips the rules."""
    fields = bind_error('{"name":"invalid","items":[{"isPrimary":true},{"name":null}]}')
    assert fields == {
        "code": ["The Code field is required."],
        "items[0].Name": ["The Name field is required."],
        "items[1].Name": ["The Name field is required."],
    }
    assert CHECKED == []
    assert bind_error('{"code":null}') == {"code": ["The Code field is required."]}


def test_the_rules_run_once_the_body_is_complete() -> None:
    """Stage 3 reports the request's own rule failures."""
    assert bind_error('{"code":"c","name":"invalid"}') == {"name": ["'Name' is not valid."]}
    assert CHECKED == ["c"]


def test_nulls_are_accepted_for_reference_types_only() -> None:
    """Null text, optional values and lists bind; null numbers do not."""
    request = bind('{"code":"c","name":null,"enabled":null,"tags":null,"items":null}')
    assert request.name is None
    assert request.enabled is None
    assert request.tags is None
    assert request.items is None
    assert bind_error('{"code":"c","count":null}') == {"count": [INVALID_VALUE], "request": [REQUEST_ENTRY]}
    assert bind_error('{"code":"c","gender":null}') == {"gender": [INVALID_VALUE], "request": [REQUEST_ENTRY]}


def test_the_last_duplicate_property_wins_and_names_match_case_insensitively() -> None:
    """Duplicates resolve to the last value regardless of the spelling of the name."""
    assert bind('{"code":"c","NAME":"first","name":"second"}').name == "second"
    assert bind('{"code":"c","name":"first","NAME":"second"}').name == "second"


def test_a_byte_order_mark_is_ignored() -> None:
    """A UTF-8 BOM before the document is skipped."""
    identifier = uuid4()
    request = bind(b"\xef\xbb\xbf" + ('{"code":"c","id":"' + str(identifier) + '"}').encode())
    assert request.id == identifier


def test_unknown_properties_are_ignored() -> None:
    """Members that the model does not declare do not fail the binding."""
    assert bind('{"code":"c","unexpected":{"deep":[1,2,3]}}').code == "c"


@pytest.mark.parametrize(
    ("media", "accepted"),
    [
        ("application/json", True),
        ("application/json; charset=utf-8", True),
        ("text/json", True),
        ("application/problem+json", True),
        ("Application/JSON", True),
        ("text/plain", False),
        ("", False),
        (None, False),
        ("application/x-www-form-urlencoded", False),
        ("multipart/form-data", False),
    ],
)
def test_only_json_media_types_are_accepted(media: str | None, accepted: bool) -> None:
    """application/json, text/json and application/*+json are JSON."""
    assert is_json_media_type(media) is accepted


def test_the_body_limit_is_one_mebibyte() -> None:
    """Bodies over 1 MiB are refused before binding."""
    assert MAX_BODY_BYTES == 1024 * 1024


def test_the_unreadable_problem_lower_cases_the_first_letter_of_the_path() -> None:
    """The offending path keeps the reference model-state spelling."""
    error = unreadable("Items[0].IsPrimary")
    assert error.fields is not None
    assert error.fields.as_dict() == {"items[0].IsPrimary": [INVALID_VALUE], "request": [REQUEST_ENTRY]}


def test_required_properties_are_non_nullable_text_lists_and_objects() -> None:
    """Only non-optional text, list and nested model fields are required in stage 2."""
    required = {field.alias: field.required_name for field in fields_of(SampleRequest) if field.required_name}
    assert required == {"code": "Code"}
    assert {field.alias for field in fields_of(SampleItem) if field.required_name} == {"name"}


def test_a_booking_body_binds_with_the_contract_names() -> None:
    """A real request model binds from camelCase JSON and accepts PascalCase enumeration values."""
    patient, provider, location = uuid4(), uuid4(), uuid4()
    body = (
        f'{{"patientId":"{patient}","providerId":"{provider}","locationId":"{location}",'
        '"startsAt":"2027-01-11T09:00:00-06:00","visitType":"Telehealth","reason":"Check-up"}'
    )
    request = bind_model(body.encode(), BookRequest, TODAY)
    assert (request.patient_id, request.provider_id, request.location_id) == (patient, provider, location)
    assert request.starts_at == datetime(2027, 1, 11, 15, 0, tzinfo=UTC)
    assert request.visit_type is VisitType.TELEHEALTH
    assert request.reason == "Check-up"
    assert request.waiting_list_entry_id is None


def test_an_empty_booking_body_fails_the_booking_rules() -> None:
    """Absent identifiers and start time take zero values that the rules reject."""
    with pytest.raises(AppError) as raised:
        bind_model(b"{}", BookRequest, TODAY)
    assert raised.value.fields is not None
    assert raised.value.fields.as_dict() == {
        "patientId": ["'Patient id' must not be empty."],
        "providerId": ["'Provider id' must not be empty."],
        "locationId": ["'Location id' must not be empty."],
        "startsAt": ["'Starts at' must not be empty."],
    }
