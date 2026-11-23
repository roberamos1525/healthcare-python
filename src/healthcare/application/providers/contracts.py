"""Request and response models of the provider, credential, directory and schedule endpoints."""

from datetime import date, datetime, timedelta
from uuid import UUID

from healthcare.application.core.models import ApiModel, RequestModel, TimeOfDay, UtcDateTime
from healthcare.application.core.validation import Validator
from healthcare.domain.enums import CredentialType, ProviderStatus, ScheduleExceptionType, VerificationStatus
from healthcare.domain.rules import is_valid_npi

_NIL = UUID(int=0)


class SpecialtyInput(RequestModel):
    """One specialty in a provider request."""

    name: str
    taxonomy_code: str | None = None
    is_primary: bool


class ProviderRequest(RequestModel):
    """Body of POST /providers and PUT /providers/{providerId}."""

    first_name: str
    last_name: str
    professional_title: str | None = None
    npi: str | None = None
    biography: str | None = None
    default_appointment_duration_minutes: int | None = None
    accepting_new_patients: bool
    listed_in_directory: bool
    user_id: UUID | None = None
    specialties: list[SpecialtyInput] | None = None
    location_ids: list[UUID] | None = None
    version: int | None = None

    def check(self, v: Validator, today: date) -> None:
        """Apply the profile rules, including the NPI check digit."""
        v.text("firstName", self.first_name).not_empty().max_length(100)
        v.text("lastName", self.last_name).not_empty().max_length(100)
        v.text("professionalTitle", self.professional_title).max_length(50)
        v.ensure("npi", self.npi is None or is_valid_npi(self.npi), "Enter a valid 10-digit NPI.")
        v.text("biography", self.biography).max_length(4000)
        v.duration("defaultAppointmentDurationMinutes", self.default_appointment_duration_minutes)
        if self.specialties is not None:
            primary = sum(1 for specialty in self.specialties if specialty.is_primary)
            v.ensure(
                "specialties",
                len(self.specialties) <= 10 and primary <= 1,
                "Provide up to ten specialties with at most one marked as primary.",
            )
            for index, specialty in enumerate(self.specialties):
                v.text(f"specialties[{index}].name", specialty.name).not_empty().max_length(100)
                v.text(f"specialties[{index}].taxonomyCode", specialty.taxonomy_code).max_length(20)
        v.ensure("locationIds", self.location_ids is None or len(self.location_ids) <= 50, "Too many locations.")


class CredentialRequest(RequestModel):
    """Body of POST /providers/{providerId}/credentials."""

    type: CredentialType
    issuing_authority: str
    identifier: str
    issued_on: date | None = None
    expires_on: date | None = None

    def check(self, v: Validator, today: date) -> None:
        """Apply the credential rules."""
        v.text("issuingAuthority", self.issuing_authority).not_empty().max_length(200)
        v.text("identifier", self.identifier).not_empty().max_length(100)
        v.ensure(
            "expiresOn",
            self.expires_on is None or self.issued_on is None or self.expires_on > self.issued_on,
            "The expiry date must be after the issue date.",
        )


class CredentialReviewRequest(RequestModel):
    """Body of POST /providers/{providerId}/credentials/{credentialId}/review."""

    decision: VerificationStatus
    notes: str | None = None

    def check(self, v: Validator, today: date) -> None:
        """Require a final decision."""
        v.ensure(
            "decision",
            self.decision in (VerificationStatus.VERIFIED, VerificationStatus.REJECTED),
            "The decision must be verified or rejected.",
        )
        v.text("notes", self.notes).max_length(1000)


class ScheduleRequest(RequestModel):
    """Body of POST /provider-schedules and PUT /provider-schedules/{scheduleId}."""

    provider_id: UUID
    location_id: UUID
    day_of_week: int
    start_time: TimeOfDay
    end_time: TimeOfDay
    slot_duration_minutes: int | None = None
    effective_from: date
    effective_to: date | None = None

    def check(self, v: Validator, today: date) -> None:
        """Apply the schedule rules."""
        v.not_empty_id("providerId", self.provider_id)
        v.not_empty_id("locationId", self.location_id)
        v.ensure(
            "dayOfWeek", 0 <= self.day_of_week <= 6, "The day of week must be between 0 (Sunday) and 6 (Saturday)."
        )
        v.ensure("endTime", self.end_time > self.start_time, "The end time must be after the start time.")
        v.duration("slotDurationMinutes", self.slot_duration_minutes)
        v.ensure(
            "effectiveTo",
            self.effective_to is None or self.effective_to >= self.effective_from,
            "The end date must not be before the start date.",
        )


class ScheduleExceptionRequest(RequestModel):
    """Body of POST /schedule-exceptions."""

    provider_id: UUID
    type: ScheduleExceptionType
    starts_at: datetime
    ends_at: datetime
    location_id: UUID | None = None
    slot_duration_minutes: int | None = None
    reason: str | None = None

    def check(self, v: Validator, today: date) -> None:
        """Apply the exception rules."""
        v.not_empty_id("providerId", self.provider_id)
        v.ensure("endsAt", self.ends_at > self.starts_at, "The end must be after the start.")
        v.ensure(
            "endsAt",
            self.ends_at - self.starts_at <= timedelta(days=366),
            "An exception cannot be longer than one year.",
        )
        if self.type == ScheduleExceptionType.ADDITIONAL_HOURS:
            v.ensure(
                "locationId",
                self.location_id is not None and self.location_id != _NIL,
                "A location is required for additional hours.",
            )
        v.duration("slotDurationMinutes", self.slot_duration_minutes)
        v.text("reason", self.reason).max_length(200)


class Specialty(ApiModel):
    """A specialty of a provider profile."""

    name: str
    taxonomy_code: str | None = None
    is_primary: bool


class Provider(ApiModel):
    """API representation of a provider profile."""

    id: UUID
    user_id: UUID | None = None
    first_name: str
    last_name: str
    professional_title: str | None = None
    npi: str | None = None
    biography: str | None = None
    default_appointment_duration_minutes: int | None = None
    status: ProviderStatus
    accepting_new_patients: bool
    listed_in_directory: bool
    license_verified: bool
    specialties: list[Specialty]
    location_ids: list[UUID]
    version: int
    created_at: UtcDateTime
    updated_at: UtcDateTime


class Credential(ApiModel):
    """API representation of a credential."""

    id: UUID
    type: CredentialType
    issuing_authority: str
    identifier: str
    issued_on: date | None = None
    expires_on: date | None = None
    verification_status: VerificationStatus
    verified_at: UtcDateTime | None = None
    verification_notes: str | None = None


class DirectoryLocation(ApiModel):
    """A location shown in the provider directory."""

    id: UUID
    name: str
    city: str
    state: str
    time_zone: str


class DirectoryProvider(ApiModel):
    """The public profile shown in the cross-organization directory."""

    id: UUID
    organization_id: UUID
    organization_name: str
    first_name: str
    last_name: str
    professional_title: str | None = None
    accepting_new_patients: bool
    specialties: list[str]
    locations: list[DirectoryLocation]


class Schedule(ApiModel):
    """API representation of a weekly schedule block."""

    id: UUID
    provider_id: UUID
    location_id: UUID
    day_of_week: int
    start_time: TimeOfDay
    end_time: TimeOfDay
    slot_duration_minutes: int | None = None
    effective_from: date
    effective_to: date | None = None


class ScheduleException(ApiModel):
    """API representation of a schedule exception."""

    id: UUID
    provider_id: UUID
    type: ScheduleExceptionType
    starts_at: UtcDateTime
    ends_at: UtcDateTime
    location_id: UUID | None = None
    slot_duration_minutes: int | None = None
    reason: str | None = None
    affected_appointments: int | None = None
