"""Business rules: the appointment life cycle, sign-in lockout, credential standing, schedules and NPIs."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Final

from healthcare.domain.enums import AppointmentStatus, ConsentStatus, CredentialType, VerificationStatus


class DomainErrorKind(Enum):
    """Distinguishes rule violations (HTTP 422) from conflicts with the current state (HTTP 409)."""

    RULE = "rule"
    CONFLICT = "conflict"


class DomainError(Exception):
    """Raised when an operation would violate a rule; messages are safe to show and never contain PHI."""

    def __init__(self, code: str, message: str, kind: DomainErrorKind = DomainErrorKind.RULE) -> None:
        """Create the error with a machine-readable code and a safe message."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.kind = kind


_S = AppointmentStatus

ALLOWED_TRANSITIONS: Final[dict[AppointmentStatus, frozenset[AppointmentStatus]]] = {
    _S.PENDING: frozenset({_S.CONFIRMED, _S.CANCELLED}),
    _S.CONFIRMED: frozenset({_S.CHECKED_IN, _S.CANCELLED, _S.NO_SHOW}),
    _S.CHECKED_IN: frozenset({_S.IN_PROGRESS, _S.CANCELLED}),
    _S.IN_PROGRESS: frozenset({_S.COMPLETED}),
    _S.COMPLETED: frozenset(),
    _S.CANCELLED: frozenset(),
    _S.NO_SHOW: frozenset(),
}


def can_transition(source: AppointmentStatus, target: AppointmentStatus) -> bool:
    """Return whether the life cycle permits moving from one status to another."""
    return target in ALLOWED_TRANSITIONS[source]


def describe_status(status: AppointmentStatus) -> str:
    """Format a status for user-facing messages."""
    match status:
        case _S.CHECKED_IN:
            return "checked in"
        case _S.IN_PROGRESS:
            return "in progress"
        case _S.NO_SHOW:
            return "marked as a no-show"
        case _:
            return status.value


def ensure_transition(source: AppointmentStatus, target: AppointmentStatus) -> None:
    """Raise the conflict for a status change that the life cycle forbids."""
    if not can_transition(source, target):
        raise DomainError(
            "invalid_status_transition",
            f"An appointment that is {describe_status(source)} cannot be changed to {describe_status(target)}.",
            DomainErrorKind.CONFLICT,
        )


def occupies_slot(status: AppointmentStatus) -> bool:
    """Return whether an appointment in the status blocks the provider's time."""
    return status not in (_S.CANCELLED, _S.NO_SHOW)


def is_closed(status: AppointmentStatus) -> bool:
    """Return whether an appointment can no longer be edited."""
    return status in (_S.COMPLETED, _S.CANCELLED, _S.NO_SHOW)


def ensure_reschedulable(status: AppointmentStatus, starts_at: datetime, ends_at: datetime) -> None:
    """Raise when an appointment in the status cannot move to the given times."""
    if status not in (_S.PENDING, _S.CONFIRMED):
        raise DomainError(
            "appointment_not_reschedulable",
            f"An appointment that is {describe_status(status)} cannot be rescheduled.",
            DomainErrorKind.CONFLICT,
        )
    if ends_at <= starts_at:
        raise DomainError("invalid_time_range", "The appointment must end after it starts.")


def is_locked_out(lockout_until: datetime | None, now: datetime) -> bool:
    """Return whether an account is temporarily locked after repeated failed sign-ins."""
    return lockout_until is not None and lockout_until > now


def register_failed_sign_in(
    count: int, now: datetime, max_attempts: int, lockout: timedelta
) -> tuple[int, datetime | None]:
    """Count a failed sign-in; reaching the limit locks the account and resets the counter."""
    count += 1
    if count >= max_attempts:
        return 0, now + lockout
    return count, None


@dataclass(frozen=True, slots=True)
class CredentialStanding:
    """The fields of a provider credential that decide its standing."""

    type: CredentialType
    verification_status: VerificationStatus
    expires_on: date | None


def in_good_standing(credential: CredentialStanding, today: date) -> bool:
    """Return whether the credential is verified and has not expired."""
    return credential.verification_status == VerificationStatus.VERIFIED and (
        credential.expires_on is None or credential.expires_on >= today
    )


def has_verified_license(credentials: Iterable[CredentialStanding], today: date) -> bool:
    """Return whether any credential is a verified, unexpired medical license."""
    return any(c.type == CredentialType.MEDICAL_LICENSE and in_good_standing(c, today) for c in credentials)


def ensure_can_activate(credentials: Iterable[CredentialStanding], today: date) -> None:
    """Raise unless the provider holds a verified, unexpired medical license."""
    if not has_verified_license(credentials, today):
        raise DomainError(
            "provider_not_verified",
            "A verified, unexpired medical license is required before the provider can be activated.",
        )


def ensure_final_decision(decision: VerificationStatus) -> None:
    """Raise when a credential review decision is not final."""
    if decision == VerificationStatus.PENDING:
        raise DomainError("invalid_verification_decision", "The decision must be verified or rejected.")


def ensure_revocable(status: ConsentStatus) -> None:
    """Raise when a consent has already been revoked."""
    if status == ConsentStatus.REVOKED:
        raise DomainError("consent_already_revoked", "The consent has already been revoked.", DomainErrorKind.CONFLICT)


@dataclass(frozen=True, slots=True)
class WeeklyBlock:
    """A recurring block of working hours used for overlap checks."""

    day_of_week: int
    start: time
    end: time
    effective_from: date
    effective_to: date | None

    def overlaps(self, other: WeeklyBlock) -> bool:
        """Return whether both blocks could produce overlapping hours on the same weekday and dates."""
        same_day = self.day_of_week == other.day_of_week
        times_overlap = self.start < other.end and other.start < self.end
        dates_overlap = (other.effective_to is None or self.effective_from <= other.effective_to) and (
            self.effective_to is None or other.effective_from <= self.effective_to
        )
        return same_day and times_overlap and dates_overlap


def is_valid_npi(npi: str) -> bool:
    """Return whether the text is a ten-digit NPI with a valid Luhn check digit (prefix 80840, per CMS)."""
    if len(npi) != 10 or not all("0" <= ch <= "9" for ch in npi):
        return False
    total = 0
    for index, ch in enumerate(reversed("80840" + npi)):
        digit = ord(ch) - 48
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0
