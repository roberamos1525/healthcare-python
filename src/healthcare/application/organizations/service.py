"""Organization, booking policy, membership and invitation use cases."""

import re
from datetime import timedelta
from typing import Final
from uuid import UUID

from healthcare.application.access import AccessResolver
from healthcare.application.core.audit import Args, AuditEntry, audit, outbox, where
from healthcare.application.core.caller import Caller, Scope
from healthcare.application.core.errors import (
    AppError,
    bad_request,
    concurrency_conflict,
    conflict,
    ensure_version,
    forbidden,
    not_found,
    rule,
)
from healthcare.application.core.paging import Paged, PageRequest, like_contains, paged
from healthcare.application.core.ports import Clock, Database, Mailer, Row, Tx, new_id
from healthcare.application.core.validation import is_blank, trimmed
from healthcare.application.organizations.contracts import (
    AcceptedInvitation,
    AcceptInvitationRequest,
    CreateInvitationRequest,
    CreateOrganizationRequest,
    Invitation,
    Member,
    Organization,
    Settings,
    UpdateMemberRequest,
    UpdateOrganizationRequest,
    UpdateSettingsRequest,
    distinct,
)
from healthcare.application.security import generate_token, hash_token
from healthcare.domain.authorization import Permission, Role
from healthcare.domain.enums import InvitationStatus, MembershipStatus, OrganizationStatus, OrganizationType

_ORGANIZATION_COLUMNS: Final = (
    "id, name, slug, type, status, time_zone, contact_email, phone, version, created_at, updated_at"
)
_MEMBER_SELECT: Final = """
    SELECT m.id, u.id AS user_id, u.email, u.first_name, u.last_name, m.roles, m.status, m.created_at
    FROM organization.organization_memberships m JOIN identity.users u ON u.id = m.user_id
"""
_INVITATION_COLUMNS: Final = "id, email, roles, status, expires_at, created_at"
_NON_ALPHANUMERIC: Final = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Turn a display name into a lowercase URL-safe slug of at most 60 characters."""
    slug = _NON_ALPHANUMERIC.sub("-", name.strip().lower()).strip("-")
    if len(slug) > 60:
        slug = slug[:60].rstrip("-")
    return slug or "organization"


def organization_view(row: Row) -> Organization:
    """Build the API representation of an organization row."""
    return Organization.model_construct(
        id=row.id,
        name=row.name,
        slug=row.slug,
        type=OrganizationType(row.type),
        status=OrganizationStatus(row.status),
        time_zone=row.time_zone,
        contact_email=row.contact_email,
        phone=row.phone,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _member_view(row: Row) -> Member:
    """Build the API representation of a membership row."""
    return Member.model_construct(
        membership_id=row.id,
        user_id=row.user_id,
        email=row.email,
        first_name=row.first_name,
        last_name=row.last_name,
        roles=list(row.roles),
        status=MembershipStatus(row.status),
        joined_at=row.created_at,
    )


def _invitation_view(row: Row) -> Invitation:
    """Build the API representation of an invitation row."""
    return Invitation.model_construct(
        id=row.id,
        email=row.email,
        roles=list(row.roles),
        status=InvitationStatus(row.status),
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


async def load_settings(tx: Tx, organization_id: UUID) -> Settings | None:
    """Read the booking policy of an organization (None when absent)."""
    row = await tx.fetchrow(
        """
        SELECT default_appointment_duration_minutes, minimum_booking_notice_minutes, booking_horizon_days,
            patient_cancellation_window_hours, reminder_offsets_minutes, allow_patient_self_scheduling,
            auto_confirm_patient_bookings
        FROM organization.organization_settings WHERE organization_id = :organization
        """,
        {"organization": organization_id},
    )
    if row is None:
        return None
    return Settings.model_construct(
        default_appointment_duration_minutes=row.default_appointment_duration_minutes,
        minimum_booking_notice_minutes=row.minimum_booking_notice_minutes,
        booking_horizon_days=row.booking_horizon_days,
        patient_cancellation_window_hours=row.patient_cancellation_window_hours,
        reminder_offsets_minutes=list(row.reminder_offsets_minutes),
        allow_patient_self_scheduling=row.allow_patient_self_scheduling,
        auto_confirm_patient_bookings=row.auto_confirm_patient_bookings,
    )


def _invalid_invitation() -> AppError:
    """Return the generic error for unusable invitation tokens."""
    return bad_request("invalid_invitation", "This invitation is invalid or has expired. Ask for a new one.")


class OrganizationService:
    """Implements the organization, settings, membership and invitation use cases."""

    def __init__(
        self, db: Database, access: AccessResolver, mailer: Mailer, clock: Clock, invitation_life: timedelta
    ) -> None:
        """Wire the service to its ports."""
        self._db = db
        self._access = access
        self._mailer = mailer
        self._clock = clock
        self._invitation_life = invitation_life

    async def create(self, caller: Caller, request: CreateOrganizationRequest) -> Organization:
        """Register a new organization and make the signed-in user its owner."""
        user_id = caller.require_user_id()
        now = self._clock()
        organization_id = new_id()
        suffix = generate_token(4).lower().replace("_", "x").replace("-", "x")
        organization = Organization.model_construct(
            id=organization_id,
            name=request.name.strip(),
            slug=f"{slugify(request.name)}-{suffix}",
            type=request.type,
            status=OrganizationStatus.ACTIVE,
            time_zone=request.time_zone,
            contact_email=trimmed(request.contact_email),
            phone=trimmed(request.phone),
            version=1,
            created_at=now,
            updated_at=now,
        )
        # The new organization becomes the tenant of this unit of work so row-level security accepts its rows.
        caller.set_organization(organization_id)
        async with self._db.transaction(caller.scope()) as tx:
            await tx.execute(
                """
                INSERT INTO organization.organizations (id, name, slug, type, status, contact_email, phone, time_zone,
                    created_at, updated_at, version)
                VALUES (:id, :name, :slug, :type, 'active', :email, :phone, :zone, :now, :now, 1)
                """,
                {
                    "id": organization_id,
                    "name": organization.name,
                    "slug": organization.slug,
                    "type": request.type.value,
                    "email": organization.contact_email,
                    "phone": organization.phone,
                    "zone": request.time_zone,
                    "now": now,
                },
            )
            await tx.execute(
                """
                INSERT INTO organization.organization_settings (organization_id, default_appointment_duration_minutes,
                    minimum_booking_notice_minutes, booking_horizon_days, patient_cancellation_window_hours,
                    reminder_offsets_minutes, allow_patient_self_scheduling, auto_confirm_patient_bookings, updated_at)
                VALUES (:id, 30, 60, 90, 24, ARRAY[1440, 120], true, false, :now)
                """,
                {"id": organization_id, "now": now},
            )
            await tx.execute(
                """
                INSERT INTO organization.organization_memberships (id, organization_id, user_id, roles, status,
                    created_at, updated_at)
                VALUES (:id, :organization, :user, ARRAY['owner'], 'active', :now, :now)
                """,
                {"id": new_id(), "organization": organization_id, "user": user_id, "now": now},
            )
            await audit(tx, caller, now, AuditEntry("organization.created", "organization", organization_id))
            await outbox(
                tx,
                caller.organization_id,
                now,
                "organization.created",
                "organization",
                organization_id,
                {"organizationId": str(organization_id)},
            )
        return organization

    async def list_organizations(
        self, caller: Caller, include_all: bool, search: str | None, page: PageRequest
    ) -> Paged[Organization]:
        """List the caller's organizations; platform administrators may list every organization."""
        user_id = caller.require_user_id()
        scope = caller.scope()
        args = Args()
        conditions: list[str] = []
        if include_all:
            caller.require_platform_admin()
            scope = caller.system_scope()
        else:
            conditions.append(
                "id IN (SELECT organization_id FROM organization.organization_memberships "
                f"WHERE user_id = {args.add(user_id)} AND status = 'active')"
            )
        if not is_blank(search):
            conditions.append(f"lower(name) LIKE {args.add(like_contains(search or ''))} ESCAPE '\\'")
        clause = where(conditions)
        async with self._db.transaction(scope) as tx:
            total = await tx.fetchval(f"SELECT count(*) FROM organization.organizations{clause}", args.values)
            limit, offset = args.add(page.safe_page_size), args.add(page.offset)
            rows = await tx.fetch(
                f"SELECT {_ORGANIZATION_COLUMNS} FROM organization.organizations{clause} "
                f"ORDER BY name, id LIMIT {limit} OFFSET {offset}",
                args.values,
            )
        return paged([organization_view(row) for row in rows], page, total)

    @staticmethod
    def _scope_for(caller: Caller, permission: str) -> Scope:
        """Check a permission for members; platform administrators get a platform-wide scope instead."""
        if caller.has(permission):
            return caller.scope()
        caller.require_platform_admin()
        return caller.system_scope()

    async def get(self, caller: Caller, organization_id: UUID) -> Organization:
        """Return one organization to its members or to a platform administrator."""
        scope = self._scope_for(caller, Permission.ORGANIZATION_READ)
        async with self._db.transaction(scope) as tx:
            row = await tx.fetchrow(
                f"SELECT {_ORGANIZATION_COLUMNS} FROM organization.organizations WHERE id = :id",
                {"id": organization_id},
            )
        if row is None:
            raise not_found("organization")
        return organization_view(row)

    async def update(self, caller: Caller, organization_id: UUID, request: UpdateOrganizationRequest) -> Organization:
        """Update the organization profile (owners only), checking the version the client last read."""
        caller.require(Permission.ORGANIZATION_MANAGE)
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            current = await tx.fetchrow(
                f"SELECT {_ORGANIZATION_COLUMNS} FROM organization.organizations WHERE id = :id AND id = :selected",
                {"id": organization_id, "selected": caller.organization_id},
            )
            if current is None:
                raise not_found("organization")
            ensure_version(current.version, request.version)
            name, zone = request.name.strip(), request.time_zone
            email, phone = trimmed(request.contact_email), trimmed(request.phone)
            changed = (name, request.type.value, zone, email, phone) != (
                current.name,
                current.type,
                current.time_zone,
                current.contact_email,
                current.phone,
            )
            version, updated_at = current.version, current.updated_at
            if changed:
                version, updated_at = current.version + 1, now
                affected = await tx.execute(
                    """
                    UPDATE organization.organizations SET name = :name, type = :type, time_zone = :zone,
                        contact_email = :email, phone = :phone, version = :version, updated_at = :now
                    WHERE id = :id AND version = :expected
                    """,
                    {
                        "id": organization_id,
                        "expected": current.version,
                        "name": name,
                        "type": request.type.value,
                        "zone": zone,
                        "email": email,
                        "phone": phone,
                        "version": version,
                        "now": now,
                    },
                )
                if affected == 0:
                    raise concurrency_conflict()
            await audit(tx, caller, now, AuditEntry("organization.updated", "organization", organization_id))
        return Organization.model_construct(
            id=current.id,
            name=name,
            slug=current.slug,
            type=request.type,
            status=OrganizationStatus(current.status),
            time_zone=zone,
            contact_email=email,
            phone=phone,
            version=version,
            created_at=current.created_at,
            updated_at=updated_at,
        )

    async def get_settings(self, caller: Caller, organization_id: UUID) -> Settings:
        """Return the organization's booking policy."""
        caller.require(Permission.ORGANIZATION_READ)
        async with self._db.transaction(caller.scope()) as tx:
            settings = await load_settings(tx, organization_id)
        if settings is None:
            raise not_found("organization")
        return settings

    async def update_settings(self, caller: Caller, organization_id: UUID, request: UpdateSettingsRequest) -> Settings:
        """Update the booking policy (owners only)."""
        caller.require(Permission.ORGANIZATION_MANAGE)
        now = self._clock()
        offsets = sorted(request.reminder_offsets_minutes, reverse=True)
        async with self._db.transaction(caller.scope()) as tx:
            affected = await tx.execute(
                """
                UPDATE organization.organization_settings SET default_appointment_duration_minutes = :duration,
                    minimum_booking_notice_minutes = :notice, booking_horizon_days = :horizon,
                    patient_cancellation_window_hours = :window, reminder_offsets_minutes = :offsets,
                    allow_patient_self_scheduling = :self_scheduling, auto_confirm_patient_bookings = :auto_confirm,
                    updated_at = :now
                WHERE organization_id = :id
                """,
                {
                    "id": organization_id,
                    "duration": request.default_appointment_duration_minutes,
                    "notice": request.minimum_booking_notice_minutes,
                    "horizon": request.booking_horizon_days,
                    "window": request.patient_cancellation_window_hours,
                    "offsets": offsets,
                    "self_scheduling": request.allow_patient_self_scheduling,
                    "auto_confirm": request.auto_confirm_patient_bookings,
                    "now": now,
                },
            )
            if affected == 0:
                raise not_found("organization")
            await audit(tx, caller, now, AuditEntry("organization.settings_updated", "organization", organization_id))
        return Settings.model_construct(
            default_appointment_duration_minutes=request.default_appointment_duration_minutes,
            minimum_booking_notice_minutes=request.minimum_booking_notice_minutes,
            booking_horizon_days=request.booking_horizon_days,
            patient_cancellation_window_hours=request.patient_cancellation_window_hours,
            reminder_offsets_minutes=offsets,
            allow_patient_self_scheduling=request.allow_patient_self_scheduling,
            auto_confirm_patient_bookings=request.auto_confirm_patient_bookings,
        )

    async def set_status(self, caller: Caller, organization_id: UUID, status: OrganizationStatus) -> Organization:
        """Suspend or reinstate an organization (platform administrators only), with immediate effect."""
        caller.require_platform_admin()
        caller.set_organization(organization_id)
        now = self._clock()
        async with self._db.transaction(caller.system_scope()) as tx:
            current = await tx.fetchrow(
                f"SELECT {_ORGANIZATION_COLUMNS} FROM organization.organizations WHERE id = :id",
                {"id": organization_id},
            )
            if current is None:
                raise not_found("organization")
            if current.status == status:
                raise conflict("organization_status_unchanged", "The organization is already in that state.")
            updated = await tx.fetchrow(
                f"""
                UPDATE organization.organizations SET status = :status, version = version + 1, updated_at = :now
                WHERE id = :id RETURNING {_ORGANIZATION_COLUMNS}
                """,
                {"id": organization_id, "status": status.value, "now": now},
            )
            action = "organization.suspended" if status == OrganizationStatus.SUSPENDED else "organization.reinstated"
            await audit(tx, caller, now, AuditEntry(action, "organization", organization_id))
            user_rows = await tx.fetch(
                """
                SELECT user_id FROM organization.organization_memberships WHERE organization_id = :id
                UNION SELECT user_id FROM patient.patient_account_links WHERE organization_id = :id
                """,
                {"id": organization_id},
            )
        # Cached access decisions must not outlive a suspension.
        for row in user_rows:
            await self._access.invalidate_access(organization_id, row.user_id)
        assert updated is not None
        return organization_view(updated)

    async def list_members(self, caller: Caller, page: PageRequest) -> Paged[Member]:
        """List the members of the selected organization (removed members excluded)."""
        caller.require(Permission.MEMBERS_READ)
        organization_id = caller.require_organization_id()
        async with self._db.transaction(caller.scope()) as tx:
            total = await tx.fetchval(
                """
                SELECT count(*) FROM organization.organization_memberships m JOIN identity.users u ON u.id = m.user_id
                WHERE m.organization_id = :organization AND m.status <> 'removed'
                """,
                {"organization": organization_id},
            )
            rows = await tx.fetch(
                _MEMBER_SELECT
                + """
                WHERE m.organization_id = :organization AND m.status <> 'removed'
                ORDER BY u.last_name, u.first_name, m.id LIMIT :limit OFFSET :offset
                """,
                {"organization": organization_id, "limit": page.safe_page_size, "offset": page.offset},
            )
        return paged([_member_view(row) for row in rows], page, total)

    @staticmethod
    async def _ensure_another_owner(tx: Tx, organization_id: UUID, membership_id: UUID) -> None:
        """Raise when the membership is the organization's only active owner."""
        exists = await tx.fetchval(
            """
            SELECT EXISTS (SELECT 1 FROM organization.organization_memberships
                WHERE organization_id = :organization AND id <> :membership AND status = 'active' AND 'owner' = ANY(roles))
            """,
            {"organization": organization_id, "membership": membership_id},
        )
        if not exists:
            raise rule("last_owner", "An organization must keep at least one active owner.")

    @staticmethod
    async def _find_membership(tx: Tx, organization_id: UUID, membership_id: UUID) -> Row | None:
        """Load a membership of the organization."""
        return await tx.fetchrow(
            """
            SELECT id, user_id, roles, status FROM organization.organization_memberships
            WHERE id = :id AND organization_id = :organization
            """,
            {"id": membership_id, "organization": organization_id},
        )

    async def update_member(self, caller: Caller, membership_id: UUID, request: UpdateMemberRequest) -> Member:
        """Change a member's roles or status; only owners touch owners, and one active owner must remain."""
        caller.require(Permission.MEMBERS_MANAGE)
        organization_id = caller.require_organization_id()
        now = self._clock()
        roles = distinct(request.roles)
        async with self._db.transaction(caller.scope()) as tx:
            current = await self._find_membership(tx, organization_id, membership_id)
            if current is None:
                raise not_found("member")
            current_roles = list(current.roles)
            if (Role.OWNER in current_roles or Role.OWNER in roles) and not caller.has_role(Role.OWNER):
                raise forbidden("owner_required", "Only an owner can change another owner's membership.")
            remains_active_owner = Role.OWNER in roles and request.status == MembershipStatus.ACTIVE
            if Role.OWNER in current_roles and not remains_active_owner:
                await self._ensure_another_owner(tx, organization_id, current.id)
            await tx.execute(
                """
                UPDATE organization.organization_memberships SET roles = :roles, status = :status, updated_at = :now
                WHERE id = :id
                """,
                {"id": current.id, "roles": roles, "status": request.status.value, "now": now},
            )
            await audit(
                tx,
                caller,
                now,
                AuditEntry(
                    "member.updated",
                    "membership",
                    current.id,
                    metadata={"roles": roles, "status": request.status.value},
                ),
            )
            row = await tx.fetchrow(_MEMBER_SELECT + " WHERE m.id = :id", {"id": current.id})
        await self._access.invalidate_access(organization_id, current.user_id)
        assert row is not None
        return _member_view(row)

    async def remove_member(self, caller: Caller, membership_id: UUID) -> None:
        """Remove a member from the organization while keeping their history."""
        caller.require(Permission.MEMBERS_MANAGE)
        organization_id = caller.require_organization_id()
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            current = await self._find_membership(tx, organization_id, membership_id)
            if current is None:
                raise not_found("member")
            if Role.OWNER in current.roles:
                if not caller.has_role(Role.OWNER):
                    raise forbidden("owner_required", "Only an owner can remove another owner.")
                await self._ensure_another_owner(tx, organization_id, current.id)
            await tx.execute(
                "UPDATE organization.organization_memberships SET status = 'removed', updated_at = :now WHERE id = :id",
                {"id": current.id, "now": now},
            )
            await audit(tx, caller, now, AuditEntry("member.removed", "membership", current.id))
        await self._access.invalidate_access(organization_id, current.user_id)

    async def invite(self, caller: Caller, request: CreateInvitationRequest) -> Invitation:
        """Invite a person by email to join the organization with the given roles."""
        caller.require(Permission.MEMBERS_MANAGE)
        organization_id = caller.require_organization_id()
        user_id = caller.require_user_id()
        now = self._clock()
        email = request.email.strip().lower()
        roles = distinct(request.roles)
        if Role.OWNER in roles and not caller.has_role(Role.OWNER):
            raise forbidden("owner_required", "Only an owner can invite another owner.")
        # The organization id is embedded so the invitation can be looked up inside the right tenant.
        token = f"{organization_id.hex}.{generate_token(32)}"
        invitation = Invitation.model_construct(
            id=new_id(),
            email=request.email.strip(),
            roles=roles,
            status=InvitationStatus.PENDING,
            expires_at=now + self._invitation_life,
            created_at=now,
        )
        async with self._db.transaction(caller.scope()) as tx:
            already_member = await tx.fetchval(
                """
                SELECT EXISTS (SELECT 1 FROM organization.organization_memberships m
                    JOIN identity.users u ON u.id = m.user_id
                    WHERE m.organization_id = :organization AND m.status = 'active' AND u.email_normalized = :email)
                """,
                {"organization": organization_id, "email": email},
            )
            if already_member:
                raise conflict("already_member", "This person is already a member of the organization.")
            await tx.execute(
                """
                UPDATE organization.staff_invitations SET status = 'revoked'
                WHERE organization_id = :organization AND email_normalized = :email AND status = 'pending'
                """,
                {"organization": organization_id, "email": email},
            )
            await tx.execute(
                """
                INSERT INTO organization.staff_invitations (id, organization_id, email, email_normalized, roles,
                    token_hash, status, invited_by_user_id, expires_at, created_at)
                VALUES (:id, :organization, :email, :normalized, :roles, :hash, 'pending', :user, :expires, :now)
                """,
                {
                    "id": invitation.id,
                    "organization": organization_id,
                    "email": invitation.email,
                    "normalized": email,
                    "roles": roles,
                    "hash": hash_token(token),
                    "user": user_id,
                    "expires": invitation.expires_at,
                    "now": now,
                },
            )
            await audit(
                tx,
                caller,
                now,
                AuditEntry("invitation.created", "invitation", invitation.id, metadata={"roles": roles}),
            )
            organization_name = await tx.fetchval(
                "SELECT name FROM organization.organizations WHERE id = :id", {"id": organization_id}
            )
        self._mailer.queue_staff_invitation(invitation.email, str(organization_name), token)
        return invitation

    async def list_invitations(self, caller: Caller, page: PageRequest) -> Paged[Invitation]:
        """List the invitations sent by the selected organization, newest first."""
        caller.require(Permission.MEMBERS_READ)
        organization_id = caller.require_organization_id()
        async with self._db.transaction(caller.scope()) as tx:
            total = await tx.fetchval(
                "SELECT count(*) FROM organization.staff_invitations WHERE organization_id = :organization",
                {"organization": organization_id},
            )
            rows = await tx.fetch(
                f"""
                SELECT {_INVITATION_COLUMNS} FROM organization.staff_invitations WHERE organization_id = :organization
                ORDER BY created_at DESC, id LIMIT :limit OFFSET :offset
                """,
                {"organization": organization_id, "limit": page.safe_page_size, "offset": page.offset},
            )
        return paged([_invitation_view(row) for row in rows], page, total)

    async def revoke_invitation(self, caller: Caller, invitation_id: UUID) -> None:
        """Withdraw a pending invitation."""
        caller.require(Permission.MEMBERS_MANAGE)
        organization_id = caller.require_organization_id()
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            status = await tx.fetchval(
                "SELECT status FROM organization.staff_invitations WHERE id = :id AND organization_id = :organization",
                {"id": invitation_id, "organization": organization_id},
            )
            if status is None:
                raise not_found("invitation")
            if status != InvitationStatus.PENDING:
                raise conflict("invitation_not_pending", "Only pending invitations can be revoked.")
            await tx.execute(
                "UPDATE organization.staff_invitations SET status = 'revoked' WHERE id = :id", {"id": invitation_id}
            )
            await audit(tx, caller, now, AuditEntry("invitation.revoked", "invitation", invitation_id))

    async def accept_invitation(self, caller: Caller, request: AcceptInvitationRequest) -> AcceptedInvitation:
        """Accept an invitation for the signed-in user, whose verified email must match the invited address."""
        user_id = caller.require_user_id()
        now = self._clock()
        prefix, separator, _ = request.token.partition(".")
        if not separator or not prefix:
            raise _invalid_invitation()
        organization_id = _parse_compact_uuid(prefix)
        if organization_id is None:
            raise _invalid_invitation()
        caller.set_organization(organization_id)
        async with self._db.transaction(caller.scope()) as tx:
            invitation = await tx.fetchrow(
                """
                SELECT id, email_normalized, roles, status, expires_at FROM organization.staff_invitations
                WHERE organization_id = :organization AND token_hash = :hash
                """,
                {"organization": organization_id, "hash": hash_token(request.token)},
            )
            if invitation is None or invitation.status != InvitationStatus.PENDING or invitation.expires_at <= now:
                raise _invalid_invitation()
            user = await tx.fetchrow(
                "SELECT email_normalized, email_verified_at IS NOT NULL AS verified FROM identity.users WHERE id = :id",
                {"id": user_id},
            )
            if user is None or user.email_normalized != invitation.email_normalized or not user.verified:
                raise forbidden(
                    "invitation_email_mismatch", "Sign in with the email address the invitation was sent to."
                )
            membership = await tx.fetchrow(
                """
                SELECT id, roles, status FROM organization.organization_memberships
                WHERE organization_id = :organization AND user_id = :user
                """,
                {"organization": organization_id, "user": user_id},
            )
            invited_roles = list(invitation.roles)
            if membership is None:
                membership_id = new_id()
                final_roles = invited_roles
                await tx.execute(
                    """
                    INSERT INTO organization.organization_memberships (id, organization_id, user_id, roles, status,
                        created_at, updated_at)
                    VALUES (:id, :organization, :user, :roles, 'active', :now, :now)
                    """,
                    {
                        "id": membership_id,
                        "organization": organization_id,
                        "user": user_id,
                        "roles": final_roles,
                        "now": now,
                    },
                )
            else:
                membership_id = membership.id
                final_roles = (
                    distinct([*membership.roles, *invited_roles])
                    if membership.status == MembershipStatus.ACTIVE
                    else invited_roles
                )
                await tx.execute(
                    """
                    UPDATE organization.organization_memberships SET roles = :roles, status = 'active', updated_at = :now
                    WHERE id = :id
                    """,
                    {"id": membership_id, "roles": final_roles, "now": now},
                )
            await tx.execute(
                """
                UPDATE organization.staff_invitations SET status = 'accepted', accepted_at = :now,
                    accepted_by_user_id = :user
                WHERE id = :id
                """,
                {"id": invitation.id, "now": now, "user": user_id},
            )
            await audit(tx, caller, now, AuditEntry("invitation.accepted", "invitation", invitation.id))
        await self._access.invalidate_access(organization_id, user_id)
        return AcceptedInvitation.model_construct(
            organization_id=organization_id, membership_id=membership_id, roles=final_roles
        )


def _parse_compact_uuid(text: str) -> UUID | None:
    """Parse a 32-character hexadecimal identifier (the invitation token prefix)."""
    if len(text) != 32:
        return None
    try:
        return UUID(hex=text)
    except ValueError:
        return None
