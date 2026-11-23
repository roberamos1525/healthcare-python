"""Permissions, roles and the role-to-permission matrix shared by every backend of the platform."""

from collections.abc import Iterable
from typing import Final


class Permission:
    """Permission names checked by the backend for every protected operation."""

    ORGANIZATION_READ: Final = "organization.read"
    ORGANIZATION_MANAGE: Final = "organization.manage"
    MEMBERS_READ: Final = "members.read"
    MEMBERS_MANAGE: Final = "members.manage"
    CLINICS_READ: Final = "clinics.read"
    CLINICS_MANAGE: Final = "clinics.manage"
    PROVIDERS_READ: Final = "providers.read"
    PROVIDERS_MANAGE: Final = "providers.manage"
    PROVIDERS_MANAGE_OWN: Final = "providers.manage_own"
    CREDENTIALS_VERIFY: Final = "credentials.verify"
    SCHEDULES_READ: Final = "schedules.read"
    SCHEDULES_MANAGE: Final = "schedules.manage"
    SCHEDULES_MANAGE_OWN: Final = "schedules.manage_own"
    AVAILABILITY_READ: Final = "availability.read"
    PATIENTS_READ: Final = "patients.read"
    PATIENTS_READ_ASSIGNED: Final = "patients.read_assigned"
    PATIENTS_WRITE: Final = "patients.write"
    PATIENTS_SELF: Final = "patients.self"
    PATIENTS_SELF_REGISTER: Final = "patients.self_register"
    APPOINTMENTS_READ: Final = "appointments.read"
    APPOINTMENTS_READ_OWN: Final = "appointments.read_own"
    APPOINTMENTS_MANAGE: Final = "appointments.manage"
    APPOINTMENTS_MANAGE_OWN: Final = "appointments.manage_own"
    APPOINTMENTS_CONDUCT: Final = "appointments.conduct"
    APPOINTMENTS_SELF: Final = "appointments.self"
    WAITING_LIST_MANAGE: Final = "waiting_list.manage"
    AUDIT_READ: Final = "audit.read"


class Role:
    """Organization roles; patient and guest are derived and never stored on a membership."""

    OWNER: Final = "owner"
    CLINIC_ADMIN: Final = "clinic_admin"
    PROVIDER: Final = "provider"
    RECEPTIONIST: Final = "receptionist"
    STAFF: Final = "staff"
    PATIENT: Final = "patient"
    GUEST: Final = "guest"
    PLATFORM_ADMIN: Final = "platform_admin"


ASSIGNABLE_ROLES: Final = frozenset({Role.OWNER, Role.CLINIC_ADMIN, Role.PROVIDER, Role.RECEPTIONIST, Role.STAFF})

_P = Permission

_CLINIC_ADMIN: Final = (
    _P.ORGANIZATION_READ,
    _P.MEMBERS_READ,
    _P.MEMBERS_MANAGE,
    _P.CLINICS_READ,
    _P.CLINICS_MANAGE,
    _P.PROVIDERS_READ,
    _P.PROVIDERS_MANAGE,
    _P.CREDENTIALS_VERIFY,
    _P.SCHEDULES_READ,
    _P.SCHEDULES_MANAGE,
    _P.AVAILABILITY_READ,
    _P.PATIENTS_READ,
    _P.PATIENTS_WRITE,
    _P.APPOINTMENTS_READ,
    _P.APPOINTMENTS_MANAGE,
    _P.APPOINTMENTS_CONDUCT,
    _P.WAITING_LIST_MANAGE,
    _P.AUDIT_READ,
)

ROLE_PERMISSIONS: Final[dict[str, frozenset[str]]] = {
    Role.OWNER: frozenset((*_CLINIC_ADMIN, _P.ORGANIZATION_MANAGE)),
    Role.CLINIC_ADMIN: frozenset(_CLINIC_ADMIN),
    Role.PROVIDER: frozenset(
        (
            _P.ORGANIZATION_READ,
            _P.CLINICS_READ,
            _P.PROVIDERS_READ,
            _P.PROVIDERS_MANAGE_OWN,
            _P.SCHEDULES_READ,
            _P.SCHEDULES_MANAGE_OWN,
            _P.AVAILABILITY_READ,
            _P.PATIENTS_READ_ASSIGNED,
            _P.APPOINTMENTS_READ_OWN,
            _P.APPOINTMENTS_MANAGE_OWN,
            _P.APPOINTMENTS_CONDUCT,
        )
    ),
    Role.RECEPTIONIST: frozenset(
        (
            _P.ORGANIZATION_READ,
            _P.CLINICS_READ,
            _P.PROVIDERS_READ,
            _P.SCHEDULES_READ,
            _P.AVAILABILITY_READ,
            _P.PATIENTS_READ,
            _P.PATIENTS_WRITE,
            _P.APPOINTMENTS_READ,
            _P.APPOINTMENTS_MANAGE,
            _P.WAITING_LIST_MANAGE,
        )
    ),
    Role.STAFF: frozenset(
        (
            _P.ORGANIZATION_READ,
            _P.CLINICS_READ,
            _P.PROVIDERS_READ,
            _P.SCHEDULES_READ,
            _P.AVAILABILITY_READ,
            _P.PATIENTS_READ,
            _P.APPOINTMENTS_READ,
        )
    ),
    Role.PATIENT: frozenset(
        (
            _P.CLINICS_READ,
            _P.PROVIDERS_READ,
            _P.AVAILABILITY_READ,
            _P.PATIENTS_SELF,
            _P.APPOINTMENTS_SELF,
        )
    ),
    Role.GUEST: frozenset(
        (
            _P.CLINICS_READ,
            _P.PROVIDERS_READ,
            _P.AVAILABILITY_READ,
            _P.PATIENTS_SELF_REGISTER,
        )
    ),
}


def are_assignable(roles: Iterable[str]) -> bool:
    """Return whether every role may be stored on a staff membership."""
    return all(role in ASSIGNABLE_ROLES for role in roles)


def assignable_roles_text() -> str:
    """List the assignable roles in ordinal order, as validation messages show them."""
    return ", ".join(sorted(ASSIGNABLE_ROLES))


def permissions_for(roles: Iterable[str]) -> frozenset[str]:
    """Return the union of the permissions granted by the roles; unknown roles grant nothing."""
    granted: set[str] = set()
    for role in roles:
        granted |= ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(granted)


def sorted_permissions(roles: Iterable[str]) -> list[str]:
    """Return the permissions of the roles in ordinal order."""
    return sorted(permissions_for(roles))
