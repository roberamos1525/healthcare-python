"""Domain rules: appointment life cycle, permission matrix, lockout, credentials, NPI, enumerations, calendar."""

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from itertools import product
from zoneinfo import ZoneInfo

import pytest

from healthcare.domain.authorization import (
    ASSIGNABLE_ROLES,
    ROLE_PERMISSIONS,
    Permission,
    Role,
    are_assignable,
    assignable_roles_text,
    permissions_for,
    sorted_permissions,
)
from healthcare.domain.calendar import (
    add_years,
    date_in_zone,
    day_number,
    format_time_of_day,
    is_time_zone,
    load_zone,
    local_to_utc,
    parse_date,
    parse_time_of_day,
    utc_date,
    weekday_sunday_zero,
)
from healthcare.domain.enums import (
    AdministrativeGender,
    AppointmentStatus,
    ConsentStatus,
    CredentialType,
    VerificationStatus,
    first_member,
    match_enum,
    match_enum_query,
)
from healthcare.domain.rules import (
    ALLOWED_TRANSITIONS,
    CredentialStanding,
    DomainError,
    DomainErrorKind,
    WeeklyBlock,
    can_transition,
    describe_status,
    ensure_can_activate,
    ensure_final_decision,
    ensure_reschedulable,
    ensure_revocable,
    ensure_transition,
    has_verified_license,
    in_good_standing,
    is_closed,
    is_locked_out,
    is_valid_npi,
    occupies_slot,
    register_failed_sign_in,
)

S = AppointmentStatus
NOW = datetime(2027, 1, 11, 15, 0, tzinfo=UTC)
TODAY = date(2027, 1, 11)

VALID_TRANSITIONS = [
    (S.PENDING, S.CONFIRMED),
    (S.PENDING, S.CANCELLED),
    (S.CONFIRMED, S.CHECKED_IN),
    (S.CONFIRMED, S.CANCELLED),
    (S.CONFIRMED, S.NO_SHOW),
    (S.CHECKED_IN, S.IN_PROGRESS),
    (S.CHECKED_IN, S.CANCELLED),
    (S.IN_PROGRESS, S.COMPLETED),
]
INVALID_TRANSITIONS = [
    (S.PENDING, S.CHECKED_IN),
    (S.PENDING, S.COMPLETED),
    (S.CONFIRMED, S.COMPLETED),
    (S.IN_PROGRESS, S.CANCELLED),
    (S.COMPLETED, S.CANCELLED),
    (S.CANCELLED, S.CONFIRMED),
    (S.NO_SHOW, S.CHECKED_IN),
]


def domain_error(action: Callable[[], object]) -> DomainError:
    """Run a callable and return the domain error it raises."""
    with pytest.raises(DomainError) as raised:
        action()
    return raised.value


# ---------------------------------------------------------------------------------------------------------
# Appointment state machine
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("source", "target"), VALID_TRANSITIONS)
def test_allows_valid_transitions(source: AppointmentStatus, target: AppointmentStatus) -> None:
    """Every permitted life cycle step passes."""
    assert can_transition(source, target)
    ensure_transition(source, target)


@pytest.mark.parametrize(("source", "target"), INVALID_TRANSITIONS)
def test_rejects_invalid_transitions_as_conflicts(source: AppointmentStatus, target: AppointmentStatus) -> None:
    """A forbidden step is a conflict with the reference code."""
    error = domain_error(lambda: ensure_transition(source, target))
    assert error.code == "invalid_status_transition"
    assert error.kind is DomainErrorKind.CONFLICT


def test_the_transition_table_contains_exactly_the_permitted_steps() -> None:
    """Of all 49 status pairs only the eight documented steps are allowed."""
    allowed = {(source, target) for source, target in product(S, S) if can_transition(source, target)}
    assert allowed == set(VALID_TRANSITIONS)
    assert set(ALLOWED_TRANSITIONS) == set(S)


@pytest.mark.parametrize("status", [S.COMPLETED, S.CANCELLED, S.NO_SHOW])
def test_terminal_statuses_allow_no_further_step(status: AppointmentStatus) -> None:
    """Completed, cancelled and no-show appointments are final and closed."""
    assert not ALLOWED_TRANSITIONS[status]
    assert is_closed(status)


@pytest.mark.parametrize("status", [S.PENDING, S.CONFIRMED, S.CHECKED_IN, S.IN_PROGRESS])
def test_open_statuses_are_not_closed(status: AppointmentStatus) -> None:
    """Appointments that can still progress are editable."""
    assert not is_closed(status)


def test_the_transition_message_describes_both_statuses() -> None:
    """The conflict message uses the human-readable status descriptions."""
    error = domain_error(lambda: ensure_transition(S.CHECKED_IN, S.NO_SHOW))
    assert error.message == "An appointment that is checked in cannot be changed to marked as a no-show."
    assert str(error) == error.message


@pytest.mark.parametrize(
    ("status", "text"),
    [
        (S.PENDING, "pending"),
        (S.CHECKED_IN, "checked in"),
        (S.IN_PROGRESS, "in progress"),
        (S.NO_SHOW, "marked as a no-show"),
        (S.CANCELLED, "cancelled"),
    ],
)
def test_statuses_are_described_for_messages(status: AppointmentStatus, text: str) -> None:
    """Status descriptions match the reference wording."""
    assert describe_status(status) == text


@pytest.mark.parametrize(
    ("status", "occupies"),
    [
        (S.CANCELLED, False),
        (S.NO_SHOW, False),
        (S.PENDING, True),
        (S.CONFIRMED, True),
        (S.CHECKED_IN, True),
        (S.IN_PROGRESS, True),
        (S.COMPLETED, True),
    ],
)
def test_only_cancelled_and_no_show_appointments_free_their_slot(status: AppointmentStatus, occupies: bool) -> None:
    """Which statuses keep the provider's time blocked."""
    assert occupies_slot(status) is occupies


def test_reschedules_pending_and_confirmed_appointments_only() -> None:
    """Only open, unstarted appointments can move, and they must end after they start."""
    start = NOW + timedelta(days=2)
    ensure_reschedulable(S.CONFIRMED, start, start + timedelta(minutes=30))
    ensure_reschedulable(S.PENDING, start, start + timedelta(minutes=30))
    for status in (S.CHECKED_IN, S.IN_PROGRESS, S.COMPLETED, S.CANCELLED, S.NO_SHOW):

        def reschedule(status: AppointmentStatus = status) -> None:
            """Try to move an appointment in the status."""
            ensure_reschedulable(status, start, start + timedelta(minutes=30))

        error = domain_error(reschedule)
        assert error.code == "appointment_not_reschedulable"
        assert error.kind is DomainErrorKind.CONFLICT
    error = domain_error(lambda: ensure_reschedulable(S.PENDING, start, start))
    assert error.code == "invalid_time_range"
    assert error.kind is DomainErrorKind.RULE


# ---------------------------------------------------------------------------------------------------------
# Permission matrix
# ---------------------------------------------------------------------------------------------------------


def test_the_owner_has_every_clinic_admin_permission_and_manages_the_organization() -> None:
    """The owner is the clinic administrator plus organization management."""
    owner = permissions_for([Role.OWNER])
    for permission in (
        Permission.ORGANIZATION_MANAGE,
        Permission.MEMBERS_MANAGE,
        Permission.CREDENTIALS_VERIFY,
        Permission.AUDIT_READ,
    ):
        assert permission in owner
    assert owner - permissions_for([Role.CLINIC_ADMIN]) == {Permission.ORGANIZATION_MANAGE}


def test_clinic_administrators_manage_the_practice_but_not_the_organization_profile() -> None:
    """Clinic administrators manage providers and appointments."""
    admin = permissions_for([Role.CLINIC_ADMIN])
    assert Permission.ORGANIZATION_MANAGE not in admin
    assert Permission.PROVIDERS_MANAGE in admin
    assert Permission.APPOINTMENTS_MANAGE in admin


def test_providers_are_limited_to_assigned_patients_and_their_own_schedule() -> None:
    """Providers read assigned patients and manage only their own schedule."""
    provider = permissions_for([Role.PROVIDER])
    assert Permission.PATIENTS_READ_ASSIGNED in provider
    assert Permission.SCHEDULES_MANAGE_OWN in provider
    assert Permission.APPOINTMENTS_CONDUCT in provider
    for permission in (Permission.PATIENTS_READ, Permission.SCHEDULES_MANAGE, Permission.CREDENTIALS_VERIFY):
        assert permission not in provider


def test_receptionists_register_patients_and_manage_appointments_but_not_staff() -> None:
    """Receptionists work the front desk only."""
    receptionist = permissions_for([Role.RECEPTIONIST])
    assert Permission.PATIENTS_WRITE in receptionist
    assert Permission.APPOINTMENTS_MANAGE in receptionist
    for permission in (Permission.MEMBERS_MANAGE, Permission.PROVIDERS_MANAGE, Permission.APPOINTMENTS_CONDUCT):
        assert permission not in receptionist


def test_the_staff_role_is_read_only() -> None:
    """Staff members cannot change anything."""
    staff = permissions_for([Role.STAFF])
    assert not [p for p in staff if p.endswith((".manage", ".write"))]
    assert Permission.PATIENTS_READ in staff


def test_patients_and_guests_have_self_service_permissions_only() -> None:
    """Patients book for themselves and guests may only self-register."""
    patient, guest = permissions_for([Role.PATIENT]), permissions_for([Role.GUEST])
    assert Permission.APPOINTMENTS_SELF in patient
    assert Permission.PATIENTS_SELF in patient
    assert Permission.PATIENTS_READ not in patient
    assert Permission.APPOINTMENTS_READ not in patient
    assert Permission.PATIENTS_SELF_REGISTER in guest
    assert Permission.APPOINTMENTS_SELF not in guest
    assert Permission.PATIENTS_SELF not in guest
    for permissions in (patient, guest):
        assert not [p for p in permissions if p.endswith((".manage", ".write", ".manage_own"))]


def test_roles_combine_and_unknown_roles_grant_nothing() -> None:
    """Permissions are the union of the roles; unknown roles add nothing."""
    combined = permissions_for([Role.PROVIDER, Role.RECEPTIONIST, "superuser"])
    assert Permission.APPOINTMENTS_CONDUCT in combined
    assert Permission.PATIENTS_WRITE in combined
    assert combined == permissions_for([Role.PROVIDER]) | permissions_for([Role.RECEPTIONIST])
    assert permissions_for(["superuser"]) == frozenset()
    assert permissions_for([]) == frozenset()
    assert permissions_for([Role.PLATFORM_ADMIN]) == frozenset()


def test_only_staff_roles_are_assignable() -> None:
    """Patient, guest and platform roles are never stored on a membership."""
    assert not are_assignable([Role.PATIENT])
    assert not are_assignable([Role.GUEST])
    assert not are_assignable([Role.OWNER, Role.PLATFORM_ADMIN])
    assert are_assignable([Role.OWNER, Role.STAFF])
    assert are_assignable([])
    assert {Role.OWNER, Role.CLINIC_ADMIN, Role.PROVIDER, Role.RECEPTIONIST, Role.STAFF} == ASSIGNABLE_ROLES
    assert assignable_roles_text() == "clinic_admin, owner, provider, receptionist, staff"


def test_every_permission_in_the_matrix_is_a_declared_permission() -> None:
    """The matrix only uses declared permission names, and every role is mapped."""
    declared = {value for name, value in vars(Permission).items() if name.isupper()}
    for role, permissions in ROLE_PERMISSIONS.items():
        assert permissions <= declared, role
    assert set(ROLE_PERMISSIONS) == {
        Role.OWNER,
        Role.CLINIC_ADMIN,
        Role.PROVIDER,
        Role.RECEPTIONIST,
        Role.STAFF,
        Role.PATIENT,
        Role.GUEST,
    }


def test_sorted_permissions_are_in_ordinal_order() -> None:
    """Permission lists are returned sorted."""
    listed = sorted_permissions([Role.STAFF])
    assert listed == sorted(listed)
    assert listed[0] == Permission.APPOINTMENTS_READ


# ---------------------------------------------------------------------------------------------------------
# Accounts, credentials, schedules, consents
# ---------------------------------------------------------------------------------------------------------


def test_locks_the_account_after_the_maximum_number_of_failed_sign_ins() -> None:
    """The fifth failure locks the account for 15 minutes and resets the counter."""
    count, until = 0, None
    for _ in range(4):
        count, until = register_failed_sign_in(count, NOW, 5, timedelta(minutes=15))
    assert count == 4
    assert not is_locked_out(until, NOW)
    count, until = register_failed_sign_in(count, NOW, 5, timedelta(minutes=15))
    assert count == 0
    assert is_locked_out(until, NOW + timedelta(minutes=14))
    assert not is_locked_out(until, NOW + timedelta(minutes=16))
    assert not is_locked_out(None, NOW)


def license_with(status: VerificationStatus, expires_on: date | None) -> CredentialStanding:
    """Create a medical license credential."""
    return CredentialStanding(CredentialType.MEDICAL_LICENSE, status, expires_on)


def test_a_provider_cannot_be_activated_without_a_verified_current_license() -> None:
    """Pending, expired or missing licenses block activation."""
    error = domain_error(lambda: ensure_can_activate([license_with(VerificationStatus.PENDING, None)], TODAY))
    assert error.code == "provider_not_verified"
    assert error.kind is DomainErrorKind.RULE
    expired = license_with(VerificationStatus.VERIFIED, TODAY - timedelta(days=1))
    assert domain_error(lambda: ensure_can_activate([expired], TODAY)).code == "provider_not_verified"
    assert domain_error(lambda: ensure_can_activate([], TODAY)).code == "provider_not_verified"
    rejected = license_with(VerificationStatus.REJECTED, None)
    assert domain_error(lambda: ensure_can_activate([rejected], TODAY)).code == "provider_not_verified"


def test_a_verified_current_license_allows_activation() -> None:
    """A verified license that has not expired (or expires today or never) is enough."""
    ensure_can_activate([license_with(VerificationStatus.VERIFIED, add_years(TODAY, 1))], TODAY)
    ensure_can_activate([license_with(VerificationStatus.VERIFIED, None)], TODAY)
    assert in_good_standing(license_with(VerificationStatus.VERIFIED, TODAY), TODAY)


def test_a_board_certification_alone_does_not_count_as_a_license() -> None:
    """Only medical licenses count for activation."""
    certification = CredentialStanding(CredentialType.BOARD_CERTIFICATION, VerificationStatus.VERIFIED, None)
    assert in_good_standing(certification, TODAY)
    assert not has_verified_license([certification], TODAY)


def test_a_credential_review_requires_a_final_decision() -> None:
    """Pending is not a review decision."""
    assert (
        domain_error(lambda: ensure_final_decision(VerificationStatus.PENDING)).code == "invalid_verification_decision"
    )
    ensure_final_decision(VerificationStatus.VERIFIED)
    ensure_final_decision(VerificationStatus.REJECTED)


@pytest.mark.parametrize(
    ("npi", "expected"),
    [
        ("1234567893", True),
        ("1245319599", True),
        ("1234567890", False),
        ("123456789", False),
        ("12345678ab", False),
        ("", False),
        ("12345678931", False),
        ("１２３４５６７８９３", False),
    ],
)
def test_validates_the_npi_check_digit(npi: str, expected: bool) -> None:
    """The NPI Luhn check with the 80840 prefix."""
    assert is_valid_npi(npi) is expected


def test_detects_overlapping_weekly_schedule_blocks() -> None:
    """Blocks overlap only on the same weekday, with overlapping times and effective dates."""
    existing = WeeklyBlock(1, time(9), time(12), date(2027, 1, 1), date(2027, 6, 30))

    def candidate(day: int, start: int, end: int, effective_from: date) -> WeeklyBlock:
        """Create an open-ended candidate block."""
        return WeeklyBlock(day, time(start), time(end), effective_from, None)

    march, july = date(2027, 3, 1), date(2027, 7, 1)
    assert existing.overlaps(candidate(1, 11, 13, march))
    assert not existing.overlaps(candidate(1, 12, 13, march))
    assert not existing.overlaps(candidate(2, 9, 12, march))
    assert not existing.overlaps(candidate(1, 9, 12, july))
    assert candidate(1, 8, 10, date(2026, 1, 1)).overlaps(existing)


def test_a_consent_can_be_revoked_only_once() -> None:
    """Revoking an already revoked consent is a conflict."""
    ensure_revocable(ConsentStatus.GRANTED)
    error = domain_error(lambda: ensure_revocable(ConsentStatus.REVOKED))
    assert error.code == "consent_already_revoked"
    assert error.kind is DomainErrorKind.CONFLICT


# ---------------------------------------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------------------------------------


def test_enumerations_match_snake_and_pascal_case() -> None:
    """Bodies accept both spellings ignoring case; queries accept snake case only."""
    assert match_enum(AppointmentStatus, "CheckedIn") is S.CHECKED_IN
    assert match_enum(AppointmentStatus, "NO_SHOW") is S.NO_SHOW
    assert match_enum(AppointmentStatus, "checked_in") is S.CHECKED_IN
    assert match_enum(AppointmentStatus, "Checked_In") is S.CHECKED_IN
    assert match_enum(AppointmentStatus, "Checked_in_") is None
    assert match_enum(AppointmentStatus, "unknown") is None
    assert match_enum_query(AppointmentStatus, "CheckedIn") is None
    assert match_enum_query(AppointmentStatus, "NO_SHOW") is S.NO_SHOW


def test_the_first_member_is_the_reference_default() -> None:
    """An absent enumeration takes its first declared member."""
    assert first_member(AdministrativeGender) is AdministrativeGender.FEMALE
    assert first_member(AppointmentStatus) is S.PENDING


# ---------------------------------------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------------------------------------


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Create a UTC instant."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def test_local_to_utc_handles_transitions_in_both_hemispheres() -> None:
    """Gaps do not exist and repeated times are read as standard time."""
    sydney = ZoneInfo("Australia/Sydney")
    # Sydney falls back on 4 April 2027 at 03:00 local (02:00-03:00 occurs twice); standard time wins.
    assert local_to_utc(date(2027, 4, 4), 2, 30, sydney) == utc(2027, 4, 3, 16, 30)
    # Sydney springs forward on 3 October 2027 at 02:00 local; 02:30 does not exist.
    assert local_to_utc(date(2027, 10, 3), 2, 30, sydney) is None
    assert local_to_utc(date(2027, 1, 11), 9, 0, ZoneInfo("UTC")) == utc(2027, 1, 11, 9)


def test_local_to_utc_in_chicago_skips_the_gap_and_prefers_standard_time() -> None:
    """Chicago springs forward on 14 March 2027 and falls back on 7 November 2027."""
    chicago = ZoneInfo("America/Chicago")
    assert local_to_utc(date(2027, 3, 14), 1, 59, chicago) == utc(2027, 3, 14, 7, 59)
    assert local_to_utc(date(2027, 3, 14), 2, 0, chicago) is None
    assert local_to_utc(date(2027, 3, 14), 2, 59, chicago) is None
    assert local_to_utc(date(2027, 3, 14), 3, 0, chicago) == utc(2027, 3, 14, 8)
    assert local_to_utc(date(2027, 11, 7), 0, 30, chicago) == utc(2027, 11, 7, 5, 30)
    assert local_to_utc(date(2027, 11, 7), 1, 30, chicago) == utc(2027, 11, 7, 7, 30)
    assert local_to_utc(date(2027, 11, 7), 2, 0, chicago) == utc(2027, 11, 7, 8)


def test_day_numbers_match_the_reference_calendar() -> None:
    """Day numbers count from 0001-01-01 and adding years clamps 29 February."""
    assert day_number(date(1, 1, 1)) == 0
    assert day_number(date(1970, 1, 1)) == 719162
    assert day_number(date(2027, 1, 12)) - day_number(TODAY) == 1
    assert add_years(date(2028, 2, 29), 1) == date(2029, 2, 28)
    assert add_years(date(2028, 2, 29), 4) == date(2032, 2, 29)
    assert add_years(TODAY, -130) == date(1897, 1, 11)


def test_weekdays_start_on_sunday() -> None:
    """Sunday is 0 and Saturday is 6, as in the reference."""
    assert weekday_sunday_zero(date(2027, 1, 10)) == 0
    assert weekday_sunday_zero(TODAY) == 1
    assert weekday_sunday_zero(date(2027, 1, 16)) == 6


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2027-01-11", date(2027, 1, 11)),
        ("2027-02-30", None),
        ("2027-1-11", None),
        ("11/01/2027", None),
        ("2027-01-11T00:00", None),
        ("", None),
    ],
)
def test_dates_use_the_iso_format_only(text: str, expected: date | None) -> None:
    """Only YYYY-MM-DD with a real date parses."""
    assert parse_date(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("08:30", time(8, 30)),
        ("23:59:59", time(23, 59, 59)),
        ("8:30", None),
        ("24:00", None),
        ("12:60", None),
        ("8am", None),
        ("08:30:60", None),
    ],
)
def test_times_of_day_use_the_24_hour_format(text: str, expected: time | None) -> None:
    """Only HH:mm or HH:mm:ss parses."""
    assert parse_time_of_day(text) == expected


def test_times_of_day_are_formatted_without_seconds() -> None:
    """Times are written as HH:mm."""
    assert format_time_of_day(time(8, 5, 59)) == "08:05"


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("America/Chicago", True),
        ("UTC", True),
        ("Central Standard Time", False),
        ("Mars/Olympus", False),
        ("", False),
        ("Local", False),
        ("EST", False),
        ("  ", False),
    ],
)
def test_accepts_only_iana_time_zone_identifiers(identifier: str, expected: bool) -> None:
    """Only IANA identifiers with a region (or UTC) are valid."""
    assert is_time_zone(identifier) is expected


def test_unknown_zones_fall_back_to_utc() -> None:
    """Loading an unknown identifier yields UTC instead of failing."""
    assert load_zone("Mars/Olympus") == ZoneInfo("UTC")
    assert load_zone("America/Chicago") == ZoneInfo("America/Chicago")


def test_instants_are_converted_to_calendar_dates() -> None:
    """The UTC date and the local date of an instant can differ."""
    instant = utc(2027, 1, 11, 3)
    assert utc_date(instant) == date(2027, 1, 11)
    assert date_in_zone(instant, ZoneInfo("America/Chicago")) == date(2027, 1, 10)
