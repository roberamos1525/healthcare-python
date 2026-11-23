"""Request and response models of the organization, membership and clinic endpoints."""

import datetime as dt
from datetime import date
from uuid import UUID

from healthcare.application.core.models import ApiModel, RequestModel, UtcDateTime
from healthcare.application.core.validation import Validator, not_empty_message
from healthcare.domain.authorization import are_assignable, assignable_roles_text
from healthcare.domain.calendar import MIN_DATE
from healthcare.domain.enums import (
    ActiveStatus,
    InvitationStatus,
    MembershipStatus,
    OrganizationStatus,
    OrganizationType,
)


class CreateOrganizationRequest(RequestModel):
    """Body of POST /organizations."""

    name: str
    type: OrganizationType
    time_zone: str
    contact_email: str | None = None
    phone: str | None = None

    def check(self, v: Validator, today: date) -> None:
        """Apply the registration rules."""
        v.text("name", self.name).not_empty().max_length(200)
        v.text("timeZone", self.time_zone).time_zone()
        v.text("contactEmail", self.contact_email).optional_email()
        v.text("phone", self.phone).phone()


class UpdateOrganizationRequest(RequestModel):
    """Body of PUT /organizations/{organizationId}."""

    name: str
    type: OrganizationType
    time_zone: str
    contact_email: str | None = None
    phone: str | None = None
    version: int

    def check(self, v: Validator, today: date) -> None:
        """Apply the update rules."""
        v.text("name", self.name).not_empty().max_length(200)
        v.text("timeZone", self.time_zone).time_zone()
        v.text("contactEmail", self.contact_email).optional_email()
        v.text("phone", self.phone).phone()
        v.greater_than("version", self.version, 0)


class UpdateSettingsRequest(RequestModel):
    """Body of PUT /organizations/{organizationId}/settings."""

    default_appointment_duration_minutes: int
    minimum_booking_notice_minutes: int
    booking_horizon_days: int
    patient_cancellation_window_hours: int
    reminder_offsets_minutes: list[int]
    allow_patient_self_scheduling: bool
    auto_confirm_patient_bookings: bool

    def check(self, v: Validator, today: date) -> None:
        """Apply sensible bounds to each policy value."""
        v.between("defaultAppointmentDurationMinutes", self.default_appointment_duration_minutes, 5, 480)
        v.ensure(
            "defaultAppointmentDurationMinutes",
            self.default_appointment_duration_minutes % 5 == 0,
            "The duration must be a multiple of 5 minutes.",
        )
        v.between("minimumBookingNoticeMinutes", self.minimum_booking_notice_minutes, 0, 10_080)
        v.between("bookingHorizonDays", self.booking_horizon_days, 1, 365)
        v.between("patientCancellationWindowHours", self.patient_cancellation_window_hours, 0, 168)
        offsets = self.reminder_offsets_minutes
        valid = len(offsets) <= 5 and len(set(offsets)) == len(offsets) and all(15 <= o <= 20_160 for o in offsets)
        v.ensure(
            "reminderOffsetsMinutes",
            valid,
            "Provide up to five distinct reminder times between 15 minutes and 14 days before the appointment.",
        )


def _roles_message() -> str:
    """List the assignable roles for validation messages."""
    return f"Roles must be chosen from: {assignable_roles_text()}."


class UpdateMemberRequest(RequestModel):
    """Body of PUT /organizations/{organizationId}/members/{membershipId}."""

    roles: list[str]
    status: MembershipStatus

    def check(self, v: Validator, today: date) -> None:
        """Require at least one assignable role."""
        v.ensure("roles", len(self.roles) > 0, not_empty_message("roles"))
        v.ensure("roles", are_assignable(self.roles), _roles_message())


class CreateInvitationRequest(RequestModel):
    """Body of POST /organizations/{organizationId}/invitations."""

    email: str
    roles: list[str]

    def check(self, v: Validator, today: date) -> None:
        """Require a valid email and at least one assignable role."""
        v.text("email", self.email).not_empty().max_length(254).email()
        v.ensure("roles", len(self.roles) > 0, not_empty_message("roles"))
        v.ensure("roles", are_assignable(self.roles), _roles_message())


class AcceptInvitationRequest(RequestModel):
    """Body of POST /invitations/accept."""

    token: str

    def check(self, v: Validator, today: date) -> None:
        """Require the token."""
        v.text("token", self.token).not_empty().max_length(200)


class ClinicRequest(RequestModel):
    """Body of POST /clinics and PUT /clinics/{clinicId}."""

    name: str
    description: str | None = None
    phone: str | None = None
    email: str | None = None
    status: ActiveStatus
    version: int | None = None

    def check(self, v: Validator, today: date) -> None:
        """Apply the clinic rules."""
        v.text("name", self.name).not_empty().max_length(200)
        v.text("description", self.description).max_length(2000)
        v.text("phone", self.phone).phone()
        v.text("email", self.email).optional_email()


class LocationRequest(RequestModel):
    """Body of POST /clinics/{clinicId}/locations and PUT /locations/{locationId}."""

    name: str
    address_line1: str
    address_line2: str | None = None
    city: str
    state: str
    postal_code: str
    time_zone: str
    phone: str | None = None
    status: ActiveStatus
    version: int | None = None

    def check(self, v: Validator, today: date) -> None:
        """Apply the address and time zone rules."""
        v.text("name", self.name).not_empty().max_length(200)
        v.text("addressLine1", self.address_line1).not_empty().max_length(200)
        v.text("addressLine2", self.address_line2).max_length(200)
        v.text("city", self.city).not_empty().max_length(100)
        v.text("state", self.state).us_state()
        v.text("postalCode", self.postal_code).postal_code()
        v.text("timeZone", self.time_zone).time_zone()
        v.text("phone", self.phone).phone()


class HolidayRequest(RequestModel):
    """Body of POST /clinic-holidays."""

    date: dt.date
    name: str
    location_id: UUID | None = None

    def check(self, v: Validator, today: dt.date) -> None:
        """Apply the holiday rules."""
        v.text("name", self.name).not_empty().max_length(100)
        v.ensure("date", self.date != MIN_DATE, not_empty_message("date"))


class Organization(ApiModel):
    """API representation of an organization."""

    id: UUID
    name: str
    slug: str
    type: OrganizationType
    status: OrganizationStatus
    time_zone: str
    contact_email: str | None = None
    phone: str | None = None
    version: int
    created_at: UtcDateTime
    updated_at: UtcDateTime


class Settings(ApiModel):
    """API representation of the booking policy."""

    default_appointment_duration_minutes: int
    minimum_booking_notice_minutes: int
    booking_horizon_days: int
    patient_cancellation_window_hours: int
    reminder_offsets_minutes: list[int]
    allow_patient_self_scheduling: bool
    auto_confirm_patient_bookings: bool


class Member(ApiModel):
    """API representation of a membership."""

    membership_id: UUID
    user_id: UUID
    email: str
    first_name: str
    last_name: str
    roles: list[str]
    status: MembershipStatus
    joined_at: UtcDateTime


class Invitation(ApiModel):
    """API representation of a staff invitation."""

    id: UUID
    email: str
    roles: list[str]
    status: InvitationStatus
    expires_at: UtcDateTime
    created_at: UtcDateTime


class AcceptedInvitation(ApiModel):
    """Result of accepting an invitation."""

    organization_id: UUID
    membership_id: UUID
    roles: list[str]


class Clinic(ApiModel):
    """API representation of a clinic."""

    id: UUID
    name: str
    description: str | None = None
    phone: str | None = None
    email: str | None = None
    status: ActiveStatus
    version: int
    created_at: UtcDateTime
    updated_at: UtcDateTime


class Location(ApiModel):
    """API representation of a clinic location."""

    id: UUID
    clinic_id: UUID
    name: str
    address_line1: str
    address_line2: str | None = None
    city: str
    state: str
    postal_code: str
    country: str
    time_zone: str
    phone: str | None = None
    status: ActiveStatus
    version: int
    created_at: UtcDateTime
    updated_at: UtcDateTime


class Holiday(ApiModel):
    """API representation of a closure day."""

    id: UUID
    date: dt.date
    name: str
    location_id: UUID | None = None


def distinct(values: list[str]) -> list[str]:
    """Return the values without duplicates, keeping their first order."""
    return list(dict.fromkeys(values))
