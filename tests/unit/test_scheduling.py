"""Booking contracts: the canonical request and the idempotency hash shared with the reference."""

import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from healthcare.application.core.errors import AppError
from healthcare.application.scheduling.contracts import BookRequest, canonical_request, request_hash
from healthcare.domain.calendar import MIN_INSTANT
from healthcare.domain.enums import VisitType


def _book(**values: object) -> BookRequest:
    """Build a booking request without validation (as the binder does)."""
    defaults: dict[str, object] = {
        "patient_id": uuid.UUID(int=0),
        "provider_id": uuid.UUID(int=0),
        "location_id": uuid.UUID(int=0),
        "starts_at": MIN_INSTANT,
        "visit_type": VisitType.IN_PERSON,
        "reason": None,
        "waiting_list_entry_id": None,
    }
    defaults.update(values)
    return BookRequest.model_construct(**cast(dict[str, Any], defaults))


def _sha256(text: str) -> str:
    """Hash text the way the reference does for request hashes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_the_request_hash_uses_the_reference_canonical_form() -> None:
    """The idempotency hash is the SHA-256 of the reference serializer's bytes."""
    request = _book(
        patient_id=uuid.UUID("0195c0de-0000-7000-8000-000000000001"),
        provider_id=uuid.UUID("0195c0de-0000-7000-8000-000000000002"),
        location_id=uuid.UUID("0195c0de-0000-7000-8000-000000000003"),
        starts_at=datetime(2027, 1, 11, 9, 0, tzinfo=ZoneInfo("America/Chicago")),
        reason="Annual check-up & <review>",
    )
    canonical = (
        '{"patientId":"0195c0de-0000-7000-8000-000000000001","providerId":"0195c0de-0000-7000-8000-000000000002",'
        '"locationId":"0195c0de-0000-7000-8000-000000000003","startsAt":"2027-01-11T15:00:00+00:00",'
        '"visitType":"in_person","reason":"Annual check-up \\u0026 \\u003Creview\\u003E"}'
    )
    assert request_hash(request) == _sha256(canonical)
    with_entry = request.model_copy(
        update={
            "reason": None,
            "waiting_list_entry_id": uuid.UUID("0195c0de-0000-7000-8000-000000000004"),
        }
    )
    expected = (
        '{"patientId":"0195c0de-0000-7000-8000-000000000001","providerId":"0195c0de-0000-7000-8000-000000000002",'
        '"locationId":"0195c0de-0000-7000-8000-000000000003","startsAt":"2027-01-11T15:00:00+00:00",'
        '"visitType":"in_person","waitingListEntryId":"0195c0de-0000-7000-8000-000000000004"}'
    )
    assert request_hash(with_entry) == _sha256(expected)


def test_the_canonical_request_matches_the_reference_serializer() -> None:
    """Fractional seconds, escaped text and a '+' inside text match the reference bytes."""
    request = _book(
        patient_id=uuid.UUID("01a205df-0592-7b1a-980a-d9639db0bb01"),
        provider_id=uuid.UUID("01a205df-0487-794c-aac0-c127aa6aea91"),
        location_id=uuid.UUID("01a205df-045a-7127-943e-8ddc611f635b"),
        starts_at=datetime(2026, 11, 25, 15, tzinfo=UTC),
    )
    assert canonical_request(request) == (
        '{"patientId":"01a205df-0592-7b1a-980a-d9639db0bb01","providerId":"01a205df-0487-794c-aac0-c127aa6aea91",'
        '"locationId":"01a205df-045a-7127-943e-8ddc611f635b","startsAt":"2026-11-25T15:00:00+00:00",'
        '"visitType":"in_person"}'
    )
    changed = request.model_copy(
        update={
            "reason": 'Check-up <follow> & "café" +1\n',
            "visit_type": VisitType.TELEHEALTH,
            "starts_at": datetime(2026, 11, 25, 15, 0, 0, 120_000, tzinfo=UTC),
        }
    )
    canonical = canonical_request(changed)
    assert '"startsAt":"2026-11-25T15:00:00.12+00:00"' in canonical
    assert '"reason":"Check-up \\u003Cfollow\\u003E \\u0026 \\u0022caf\\u00E9\\u0022 \\u002B1\\n"' in canonical
    assert '"visitType":"telehealth"' in canonical


def test_the_request_hash_equals_a_hash_stored_by_the_reference_implementation() -> None:
    """A request hash that the reference API stored for a synthetic booking is reproduced exactly."""
    request = _book(
        patient_id=uuid.UUID("01a206ef-5f1a-7bc1-9244-06e04b1698ea"),
        provider_id=uuid.UUID("01a206ef-5bf0-7bf3-b7b1-5b933152a9a6"),
        location_id=uuid.UUID("01a206ef-5ba3-7abc-9955-0d7efaf3d6b8"),
        starts_at=datetime.fromtimestamp(1795874400, UTC),
        reason="Annual <check-up> & 'more'",
    )
    assert request_hash(request) == "583834423988a31e56e05f17a50f42ac8f0c1f740f4f7c5b24c996756e2d262a"


def test_the_request_hash_ignores_the_offset_of_the_same_instant() -> None:
    """Equal instants hash equally whatever offset the client used."""
    base = _book(
        patient_id=uuid.uuid4(),
        provider_id=uuid.uuid4(),
        location_id=uuid.uuid4(),
        visit_type=VisitType.TELEHEALTH,
        starts_at=datetime(2027, 3, 1, 15, 30, tzinfo=UTC),
    )
    shifted = base.model_copy(update={"starts_at": base.starts_at.astimezone(timezone(timedelta(hours=-6)))})
    other = base.model_copy(update={"starts_at": base.starts_at + timedelta(minutes=30)})
    assert request_hash(base) == request_hash(shifted)
    assert request_hash(base) != request_hash(other)


def test_booking_requests_are_validated() -> None:
    """An empty booking fails the rules; a complete one passes."""
    with pytest.raises(AppError) as failure:
        _book(reason="x" * 501).validate_rules(date(2026, 1, 1))
    assert failure.value.fields is not None
    assert set(failure.value.fields.as_dict()) == {"patientId", "providerId", "locationId", "startsAt", "reason"}
    _book(
        patient_id=uuid.uuid4(), provider_id=uuid.uuid4(), location_id=uuid.uuid4(), starts_at=datetime.now(UTC)
    ).validate_rules(date(2026, 1, 1))
