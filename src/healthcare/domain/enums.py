"""Enumerations of the contract, persisted and serialized as snake_case strings.

Members are declared in the reference order; the first member is the default the reference platform uses
when a request omits the property.
"""

from enum import StrEnum


class UserStatus(StrEnum):
    """Lifecycle state of a user account."""

    PENDING_VERIFICATION = "pending_verification"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DEACTIVATED = "deactivated"


class UserTokenPurpose(StrEnum):
    """Purpose of a single-use account token."""

    EMAIL_VERIFICATION = "email_verification"
    PASSWORD_RESET = "password_reset"
    MFA_CHALLENGE = "mfa_challenge"


class MfaFactorStatus(StrEnum):
    """Enrollment state of an MFA factor."""

    PENDING = "pending"
    ACTIVE = "active"


class OrganizationType(StrEnum):
    """Kind of healthcare organization."""

    INDEPENDENT_PRACTICE = "independent_practice"
    CLINIC = "clinic"
    MULTI_LOCATION_GROUP = "multi_location_group"


class OrganizationStatus(StrEnum):
    """Lifecycle state of an organization."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class MembershipStatus(StrEnum):
    """State of a staff membership."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    REMOVED = "removed"


class InvitationStatus(StrEnum):
    """State of a staff invitation."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    EXPIRED = "expired"


class ActiveStatus(StrEnum):
    """Generic active/inactive flag of clinics, locations and patients."""

    ACTIVE = "active"
    INACTIVE = "inactive"


class ProviderStatus(StrEnum):
    """Onboarding state of a provider."""

    ONBOARDING = "onboarding"
    ACTIVE = "active"
    INACTIVE = "inactive"


class CredentialType(StrEnum):
    """Type of a professional credential."""

    MEDICAL_LICENSE = "medical_license"
    BOARD_CERTIFICATION = "board_certification"
    DEA_REGISTRATION = "dea_registration"
    OTHER = "other"


class VerificationStatus(StrEnum):
    """Outcome of reviewing a credential."""

    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class ScheduleExceptionType(StrEnum):
    """Kind of deviation from the weekly schedule."""

    TIME_OFF = "time_off"
    ADDITIONAL_HOURS = "additional_hours"


class AdministrativeGender(StrEnum):
    """HL7 FHIR R4 AdministrativeGender value set."""

    FEMALE = "female"
    MALE = "male"
    OTHER = "other"
    UNKNOWN = "unknown"


class AccountRelationship(StrEnum):
    """How a user account relates to a patient record."""

    SELF = "self"
    AUTHORIZED_REPRESENTATIVE = "authorized_representative"


class CareRelationshipType(StrEnum):
    """Nature of a patient-provider relationship."""

    PRIMARY_CARE = "primary_care"
    SPECIALIST = "specialist"
    CONSULTING = "consulting"


class ConsentType(StrEnum):
    """Category of a patient consent."""

    TREATMENT = "treatment"
    PRIVACY_PRACTICES_ACKNOWLEDGEMENT = "privacy_practices_acknowledgement"
    TELEHEALTH = "telehealth"
    EMAIL_COMMUNICATION = "email_communication"
    SMS_COMMUNICATION = "sms_communication"
    INFORMATION_SHARING = "information_sharing"


class ConsentStatus(StrEnum):
    """Whether a consent is in force."""

    GRANTED = "granted"
    REVOKED = "revoked"


class ConsentMethod(StrEnum):
    """How a consent was captured."""

    ELECTRONIC = "electronic"
    PAPER = "paper"
    VERBAL = "verbal"


class AppointmentStatus(StrEnum):
    """Lifecycle state of an appointment."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    CHECKED_IN = "checked_in"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class VisitType(StrEnum):
    """How a visit takes place."""

    IN_PERSON = "in_person"
    TELEHEALTH = "telehealth"


class BookingChannel(StrEnum):
    """Who created a booking."""

    STAFF = "staff"
    PATIENT_PORTAL = "patient_portal"


class ReminderChannel(StrEnum):
    """Delivery channel of a reminder."""

    EMAIL = "email"
    SMS = "sms"
    IN_APP = "in_app"


class ReminderStatus(StrEnum):
    """Processing state of a reminder."""

    SCHEDULED = "scheduled"
    QUEUED = "queued"
    CANCELLED = "cancelled"


class WaitingListStatus(StrEnum):
    """State of a waiting list entry."""

    ACTIVE = "active"
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class AuditOutcome(StrEnum):
    """Result of an audited action."""

    SUCCESS = "success"
    DENIED = "denied"
    FAILURE = "failure"


def match_enum[E: StrEnum](enum_type: type[E], text: str) -> E | None:
    """Return the member matching ``text`` by snake_case or PascalCase name, ignoring case.

    The reference serializer accepts both spellings ("checked_in", "CheckedIn"); integers are rejected
    before this function is reached.
    """
    lowered = text.casefold()
    for member in enum_type:
        if member.value.casefold() == lowered:
            return member
    if "_" in text:
        return None
    for member in enum_type:
        if member.value.replace("_", "").casefold() == lowered:
            return member
    return None


def match_enum_query[E: StrEnum](enum_type: type[E], text: str) -> E | None:
    """Return the member whose snake_case name equals ``text`` ignoring case (query string rule)."""
    lowered = text.casefold()
    for member in enum_type:
        if member.value.casefold() == lowered:
            return member
    return None


def first_member[E: StrEnum](enum_type: type[E]) -> E:
    """Return the first declared member, the reference default for an absent property."""
    return next(iter(enum_type))
