"""Resolves what a signed-in user may do in an organization and whether their session is still valid.

Results are cached briefly (sessions 30 s, access 60 s) so the checks do not add database round trips to
every request; every change of roles, links or organization status invalidates the affected entries.
"""

import json
from typing import Final
from uuid import UUID

from healthcare.application.core.caller import Caller, Scope, TenantAccess
from healthcare.application.core.jsonref import reference_json
from healthcare.application.core.ports import Cache, Clock, Database
from healthcare.domain.authorization import Role, permissions_for
from healthcare.domain.enums import OrganizationStatus

SESSION_CACHE_SECONDS: Final = 30.0
ACCESS_CACHE_SECONDS: Final = 60.0


def session_key(session_id: UUID) -> str:
    """Return the cache key of a session's validity."""
    return "session:" + session_id.hex


def access_key(organization_id: UUID, user_id: UUID) -> str:
    """Return the cache key of a user's access to an organization."""
    return f"access:{organization_id.hex}:{user_id.hex}"


def _denied(code: str) -> dict[str, object]:
    """Build a snapshot recording that access was refused."""
    return {"roles": [], "patientIds": [], "deniedCode": code}


class AccessResolver:
    """Checks sessions and resolves tenant access, with short-lived caching."""

    def __init__(self, db: Database, cache: Cache, clock: Clock) -> None:
        """Create the resolver."""
        self._db = db
        self._cache = cache
        self._clock = clock

    async def is_session_active(self, session_id: UUID, user_id: UUID) -> bool:
        """Return whether the session exists, is neither revoked nor expired, and belongs to an active user."""
        key = session_key(session_id)
        cached = await self._cache.get(key)
        if cached is not None:
            try:
                return bool(json.loads(cached)["active"])
            except ValueError, KeyError, TypeError:
                pass
        now = self._clock()
        async with self._db.transaction(Scope()) as tx:
            active = bool(
                await tx.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM identity.sessions s JOIN identity.users u ON u.id = s.user_id
                        WHERE s.id = :session AND s.user_id = :user AND s.revoked_at IS NULL
                          AND s.expires_at > :now AND s.idle_expires_at > :now AND u.status = 'active')
                    """,
                    {"session": session_id, "user": user_id, "now": now},
                )
            )
        await self._cache.set(key, reference_json({"active": active}), SESSION_CACHE_SECONDS)
        return active

    async def resolve(self, caller: Caller, user_id: UUID, organization_id: UUID) -> None:
        """Load the user's access to the organization onto the caller, or record why there is none."""
        caller.set_organization(organization_id)
        key = access_key(organization_id, user_id)
        snapshot: dict[str, object] | None = None
        cached = await self._cache.get(key)
        if cached is not None:
            try:
                loaded = json.loads(cached)
                snapshot = loaded if isinstance(loaded, dict) else None
            except ValueError:
                snapshot = None
        if snapshot is None:
            snapshot = await self._load(caller.scope(), user_id, organization_id)
            await self._cache.set(key, reference_json(snapshot), ACCESS_CACHE_SECONDS)
        denied = snapshot.get("deniedCode")
        if isinstance(denied, str) and denied:
            caller.deny_access(denied)
            return
        roles = [str(role) for role in snapshot.get("roles") or []]  # type: ignore[attr-defined]
        patient_ids = [UUID(str(item)) for item in snapshot.get("patientIds") or []]  # type: ignore[attr-defined]
        membership = snapshot.get("membershipId")
        provider = snapshot.get("providerId")
        caller.set_access(
            TenantAccess(
                organization_id=organization_id,
                roles=roles,
                permissions=permissions_for(roles),
                membership_id=UUID(str(membership)) if membership else None,
                provider_id=UUID(str(provider)) if provider else None,
                patient_ids=patient_ids,
            )
        )

    async def invalidate_access(self, organization_id: UUID, user_id: UUID) -> None:
        """Drop cached access for a user so role or link changes take effect immediately."""
        await self._cache.remove(access_key(organization_id, user_id))

    async def invalidate_session(self, session_id: UUID) -> None:
        """Drop the cached state of a session after it is revoked."""
        await self._cache.remove(session_key(session_id))

    async def _load(self, scope: Scope, user_id: UUID, organization_id: UUID) -> dict[str, object]:
        """Read organization status, membership, provider profile and patient links of the user."""
        params = {"organization": organization_id, "user": user_id}
        async with self._db.transaction(scope) as tx:
            status = await tx.fetchval("SELECT status FROM organization.organizations WHERE id = :organization", params)
            if status is None:
                return _denied("not_a_member")
            if status != OrganizationStatus.ACTIVE:
                return _denied("organization_suspended")
            membership = await tx.fetchrow(
                """
                SELECT id, roles FROM organization.organization_memberships
                WHERE organization_id = :organization AND user_id = :user AND status = 'active'
                """,
                params,
            )
            roles: list[str] = list(membership.roles) if membership is not None else []
            patient_rows = await tx.fetch(
                "SELECT patient_id FROM patient.patient_account_links WHERE organization_id = :organization AND user_id = :user",
                params,
            )
            patient_ids = [str(row.patient_id) for row in patient_rows]
            provider_id = None
            if Role.PROVIDER in roles:
                provider_id = await tx.fetchval(
                    "SELECT id FROM provider.providers WHERE organization_id = :organization AND user_id = :user LIMIT 1",
                    params,
                )
            if patient_ids:
                roles.append(Role.PATIENT)
            if not roles:
                allows = await tx.fetchval(
                    """
                    SELECT allow_patient_self_scheduling FROM organization.organization_settings
                    WHERE organization_id = :organization
                    """,
                    params,
                )
                if not allows:
                    return _denied("not_a_member")
                roles.append(Role.GUEST)
        snapshot: dict[str, object] = {"roles": roles}
        if membership is not None:
            snapshot["membershipId"] = str(membership.id)
        if provider_id is not None:
            snapshot["providerId"] = str(provider_id)
        snapshot["patientIds"] = patient_ids
        return snapshot
