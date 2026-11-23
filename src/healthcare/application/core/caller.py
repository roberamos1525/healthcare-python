"""The per-request caller: who is calling, which organization is selected and what they may do there."""

from dataclasses import dataclass, field
from uuid import UUID

from healthcare.application.core.errors import (
    authentication_required,
    bad_request,
    forbidden,
    permission_denied,
)
from healthcare.domain.authorization import Role


@dataclass(frozen=True, slots=True)
class Scope:
    """The row-level security context applied to a database transaction."""

    organization_id: UUID | None = None
    user_id: UUID | None = None
    system: bool = False


SYSTEM_SCOPE = Scope(system=True)


@dataclass(slots=True)
class TenantAccess:
    """What the caller may do inside the organization selected for the request."""

    organization_id: UUID
    roles: list[str]
    permissions: frozenset[str]
    membership_id: UUID | None = None
    provider_id: UUID | None = None
    patient_ids: list[UUID] = field(default_factory=list)


@dataclass(slots=True)
class Caller:
    """Per-request (or per-job) state, passed explicitly to every use case."""

    user_id: UUID | None = None
    session_id: UUID | None = None
    platform_role: str | None = None
    organization_id: UUID | None = None
    access: TenantAccess | None = None
    denied_code: str | None = None
    system: bool = False
    ip_address: str | None = None
    user_agent: str | None = None
    request_id: str | None = None

    def set_user(self, user_id: UUID, session_id: UUID | None, platform_role: str | None) -> None:
        """Record the authenticated user."""
        self.user_id = user_id
        self.session_id = session_id
        self.platform_role = platform_role

    def set_organization(self, organization_id: UUID) -> None:
        """Select the organization and drop access resolved for any other organization."""
        if self.access is not None and self.access.organization_id != organization_id:
            self.access = None
        self.organization_id = organization_id
        self.denied_code = None

    def set_access(self, access: TenantAccess) -> None:
        """Store the access resolved for the selected organization."""
        self.organization_id = access.organization_id
        self.access = access
        self.denied_code = None

    def deny_access(self, code: str) -> None:
        """Record that the user has no access to the selected organization and why."""
        self.access = None
        self.denied_code = code

    @property
    def is_platform_admin(self) -> bool:
        """Return whether the caller holds the platform administrator role."""
        return self.platform_role == Role.PLATFORM_ADMIN

    def scope(self) -> Scope:
        """Return the tenant scope of the caller for database work."""
        return Scope(self.organization_id, self.user_id, self.system)

    def system_scope(self) -> Scope:
        """Return the caller's scope widened to all organizations (jobs and narrow platform queries only)."""
        return Scope(self.organization_id, self.user_id, True)

    def require_user_id(self) -> UUID:
        """Return the authenticated user or raise an authentication error."""
        if self.user_id is None:
            raise authentication_required()
        return self.user_id

    def require_organization_id(self) -> UUID:
        """Return the selected organization or raise when none was selected."""
        if self.organization_id is None:
            raise bad_request("organization_required", "Select an organization with the X-Organization-Id header.")
        return self.organization_id

    def has(self, permission: str) -> bool:
        """Return whether the caller holds the permission in the selected organization."""
        return self.access is not None and permission in self.access.permissions

    def has_any(self, *permissions: str) -> bool:
        """Return whether the caller holds at least one of the permissions."""
        return any(self.has(permission) for permission in permissions)

    def require(self, *permissions: str) -> None:
        """Raise unless the caller holds one of the permissions, reporting a missing user or organization first."""
        if self.has_any(*permissions):
            return
        if self.organization_id is None:
            self.require_user_id()
            self.require_organization_id()
        if self.denied_code:
            raise forbidden(self.denied_code, "You do not have access to this organization.")
        raise permission_denied()

    def require_platform_admin(self) -> None:
        """Raise unless the caller is a platform administrator."""
        self.require_user_id()
        if not self.is_platform_admin:
            raise permission_denied()

    @property
    def provider_id(self) -> UUID | None:
        """Return the provider profile linked to the caller in the selected organization, if any."""
        return self.access.provider_id if self.access else None

    @property
    def patient_ids(self) -> list[UUID]:
        """Return the patient records linked to the caller in the selected organization."""
        return self.access.patient_ids if self.access else []

    def has_role(self, role: str) -> bool:
        """Return whether the caller holds the role in the selected organization."""
        return self.access is not None and role in self.access.roles

    def is_linked_patient(self, patient_id: UUID) -> bool:
        """Return whether the patient record is linked to the caller's account."""
        return patient_id in self.patient_ids

    def is_own_provider(self, provider_id: UUID) -> bool:
        """Return whether the provider profile is the caller's own."""
        own = self.provider_id
        return own is not None and own == provider_id
