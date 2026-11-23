"""Request and response models of the availability, appointment and waiting list endpoints."""

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from pydantic import Field

from healthcare.application.core.jsonref import format_offset, reference_string
from healthcare.application.core.models import ApiModel, OffsetDateTime, RequestModel, UtcDateTime
from healthcare.application.core.validation import Validator, not_empty_message
from healthcare.application.security import hash_token
from healthcare.domain.calendar import MIN_INSTANT
from healthcare.domain.enums import AppointmentStatus, BookingChannel, VisitType, WaitingListStatus


class BookRequest(RequestModel):
    """Body of POST /appointments."""

    patient_id: UUID
    provider_id: UUID
    location_id: UUID
    starts_at: datetime
    visit_type: VisitType
    reason: str | None = None
    waiting_list_entry_id: UUID | None = None

    def check(self, v: Validator, today: date) -> None:
        """Apply the booking rules."""
        v.not_empty_id("patientId", self.patient_id)
        v.not_empty_id("providerId", self.provider_id)
        v.not_empty_id("locationId", self.location_id)
        v.ensure("startsAt", self.starts_at != MIN_INSTANT, not_empty_message("startsAt"))
        v.text("reason", self.reason).max_length(500)


def canonical_request(request: BookRequest) -> str:
    """Write the booking request exactly as the reference serializer does.

    Members are camelCase in declaration order with nulls omitted; text is escaped with the reference rules
    while the start time (UTC, ``+00:00`` offset) is written raw, because the reference writes dates without
    escaping. The SHA-256 of these bytes detects a reused idempotency key, so they must match across backends.
    """
    parts = [
        '{"patientId":' + reference_string(str(request.patient_id)),
        ',"providerId":' + reference_string(str(request.provider_id)),
        ',"locationId":' + reference_string(str(request.location_id)),
        ',"startsAt":"' + format_offset(request.starts_at) + '"',
        ',"visitType":' + reference_string(request.visit_type.value),
    ]
    if request.reason is not None:
        parts.append(',"reason":' + reference_string(request.reason))
    if request.waiting_list_entry_id is not None:
        parts.append(',"waitingListEntryId":' + reference_string(str(request.waiting_list_entry_id)))
    parts.append("}")
    return "".join(parts)


def request_hash(request: BookRequest) -> str:
    """Return the lowercase hex SHA-256 of the canonical request."""
    return hash_token(canonical_request(request))


class UpdateAppointmentRequest(RequestModel):
    """Body of PUT /appointments/{appointmentId}."""

    visit_type: VisitType
    reason: str | None = None
    version: int

    def check(self, v: Validator, today: date) -> None:
        """Apply the update rules."""
        v.text("reason", self.reason).max_length(500)
        v.greater_than("version", self.version, 0)


class CancelRequest(RequestModel):
    """Body of POST /appointments/{appointmentId}/cancel."""

    reason: str | None = None

    def check(self, v: Validator, today: date) -> None:
        """Apply the cancellation rules."""
        v.text("reason", self.reason).max_length(500)


class RescheduleRequest(RequestModel):
    """Body of POST /appointments/{appointmentId}/reschedule."""

    starts_at: datetime
    location_id: UUID | None = None

    def check(self, v: Validator, today: date) -> None:
        """Apply the reschedule rules."""
        v.ensure("startsAt", self.starts_at != MIN_INSTANT, not_empty_message("startsAt"))


class WaitingListRequest(RequestModel):
    """Body of POST /waiting-list."""

    patient_id: UUID
    provider_id: UUID | None = None
    location_id: UUID | None = None
    earliest_date: date
    latest_date: date
    notes: str | None = None

    def check(self, v: Validator, today: date) -> None:
        """Apply the waiting list rules."""
        v.not_empty_id("patientId", self.patient_id)
        v.ensure(
            "latestDate",
            self.latest_date >= self.earliest_date,
            "The latest date must not be before the earliest date.",
        )
        v.text("notes", self.notes).max_length(500)


class Slot(ApiModel):
    """An open appointment slot."""

    location_id: UUID
    starts_at: UtcDateTime
    ends_at: UtcDateTime
    time_zone: str


class AvailabilityResponse(ApiModel):
    """Body of GET /availability."""

    provider_id: UUID
    from_: date = Field(alias="from")
    to: date
    slots: list[Slot]


class PatientRef(ApiModel):
    """The patient of an appointment."""

    id: UUID
    first_name: str
    last_name: str


class ProviderRef(ApiModel):
    """The provider of an appointment."""

    id: UUID
    first_name: str
    last_name: str
    professional_title: str | None = None


class LocationRef(ApiModel):
    """The location of an appointment."""

    id: UUID
    name: str
    time_zone: str


class Appointment(ApiModel):
    """API representation of an appointment."""

    id: UUID
    status: AppointmentStatus
    starts_at: UtcDateTime
    ends_at: UtcDateTime
    visit_type: VisitType
    reason: str | None = None
    booking_channel: BookingChannel
    patient: PatientRef
    provider: ProviderRef
    location: LocationRef
    checked_in_at: UtcDateTime | None = None
    started_at: UtcDateTime | None = None
    completed_at: UtcDateTime | None = None
    cancelled_at: UtcDateTime | None = None
    cancellation_reason: str | None = None
    version: int
    created_at: UtcDateTime
    updated_at: UtcDateTime


class StoredAppointment(ApiModel):
    """The reference serializer's form of an appointment, stored for idempotent replays by every backend."""

    id: UUID
    status: AppointmentStatus
    starts_at: OffsetDateTime
    ends_at: OffsetDateTime
    visit_type: VisitType
    reason: str | None = None
    booking_channel: BookingChannel
    patient: PatientRef
    provider: ProviderRef
    location: LocationRef
    checked_in_at: OffsetDateTime | None = None
    started_at: OffsetDateTime | None = None
    completed_at: OffsetDateTime | None = None
    cancelled_at: OffsetDateTime | None = None
    cancellation_reason: str | None = None
    version: int
    created_at: OffsetDateTime
    updated_at: OffsetDateTime


class History(ApiModel):
    """One status or time change of an appointment."""

    id: UUID
    change_type: str
    from_status: AppointmentStatus | None = None
    to_status: AppointmentStatus
    changed_by_user_id: UUID | None = None
    changed_at: UtcDateTime
    reason: str | None = None
    previous_starts_at: UtcDateTime | None = None
    previous_ends_at: UtcDateTime | None = None


class WaitingListEntry(ApiModel):
    """API representation of a waiting list entry."""

    id: UUID
    patient_id: UUID
    provider_id: UUID | None = None
    location_id: UUID | None = None
    earliest_date: date
    latest_date: date
    notes: str | None = None
    status: WaitingListStatus
    fulfilled_appointment_id: UUID | None = None
    created_at: UtcDateTime


@dataclass(frozen=True, slots=True)
class AppointmentFilter:
    """The appointment list filters."""

    provider_id: UUID | None = None
    patient_id: UUID | None = None
    location_id: UUID | None = None
    status: AppointmentStatus | None = None
    starts_from: datetime | None = None
    starts_to: datetime | None = None
    sort: str | None = None


@dataclass(frozen=True, slots=True)
class BookResult:
    """A booking outcome, flagging whether a stored response was replayed."""

    appointment: Appointment
    replayed: bool
