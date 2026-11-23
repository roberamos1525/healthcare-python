"""Provider profile, credential and directory use cases."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import cmp_to_key
from typing import Any, Final
from uuid import UUID

from healthcare.application.access import AccessResolver
from healthcare.application.core.audit import Args, AuditEntry, audit, where
from healthcare.application.core.caller import Caller
from healthcare.application.core.errors import (
    concurrency_conflict,
    ensure_version,
    field_error,
    forbidden,
    from_domain,
    not_found,
    require_version,
    rule,
)
from healthcare.application.core.paging import Paged, PageRequest, like_contains, like_starts_with, paged, parse_sort
from healthcare.application.core.ports import Clock, Database, Row, Tx, new_id
from healthcare.application.core.validation import is_blank, trimmed
from healthcare.application.providers.contracts import (
    Credential,
    CredentialRequest,
    CredentialReviewRequest,
    DirectoryLocation,
    DirectoryProvider,
    Provider,
    ProviderRequest,
    Specialty,
)
from healthcare.domain.authorization import Permission
from healthcare.domain.enums import CredentialType, ProviderStatus, VerificationStatus
from healthcare.domain.rules import (
    CredentialStanding,
    DomainError,
    ensure_can_activate,
    ensure_final_decision,
    has_verified_license,
)

_PROVIDER_COLUMNS: Final = (
    "id, organization_id, user_id, first_name, last_name, professional_title, npi, biography, "
    "default_appointment_duration_minutes, status, accepting_new_patients, listed_in_directory, version, created_at, "
    "updated_at"
)


def culture_compare(a: str, b: str) -> int:
    """Order text like the reference's default comparison: case-insensitive, lower case first on ties."""
    la, lb = a.lower(), b.lower()
    if la != lb:
        return -1 if la < lb else 1
    if a == b:
        return 0
    return -1 if a > b else 1


_CULTURE_KEY: Final = cmp_to_key(culture_compare)


def culture_key(text: str) -> Any:
    """Return a sort key ordering text like the reference's default string comparison."""
    return _CULTURE_KEY(text)


@dataclass(slots=True)
class StoredCredential:
    """A stored credential with its creation time."""

    view: Credential
    created_at: datetime


@dataclass(slots=True)
class StoredProvider:
    """A stored provider with its child collections."""

    row: Row
    status: ProviderStatus
    version: int
    updated_at: datetime
    user_id: UUID | None
    specialties: list[Specialty] = field(default_factory=list)
    location_ids: list[UUID] = field(default_factory=list)
    credentials: list[StoredCredential] = field(default_factory=list)

    @property
    def id(self) -> UUID:
        """Return the provider identifier."""
        return self.row.id  # type: ignore[no-any-return]

    @property
    def organization_id(self) -> UUID:
        """Return the owning organization."""
        return self.row.organization_id  # type: ignore[no-any-return]

    def standings(self) -> list[CredentialStanding]:
        """Return the fields of the credentials that decide the provider's standing."""
        return [
            CredentialStanding(c.view.type, c.view.verification_status, c.view.expires_on) for c in self.credentials
        ]

    def view(self, today: date) -> Provider:
        """Build the API representation, evaluating license standing on the given day."""
        row = self.row
        return Provider.model_construct(
            id=row.id,
            user_id=self.user_id,
            first_name=row.first_name,
            last_name=row.last_name,
            professional_title=row.professional_title,
            npi=row.npi,
            biography=row.biography,
            default_appointment_duration_minutes=row.default_appointment_duration_minutes,
            status=self.status,
            accepting_new_patients=row.accepting_new_patients,
            listed_in_directory=row.listed_in_directory,
            license_verified=has_verified_license(self.standings(), today),
            specialties=self.specialties,
            location_ids=self.location_ids,
            version=self.version,
            created_at=row.created_at,
            updated_at=self.updated_at,
        )


def _stored(row: Row) -> StoredProvider:
    """Wrap a provider row."""
    return StoredProvider(row, ProviderStatus(row.status), row.version, row.updated_at, row.user_id)


async def load_children(tx: Tx, providers: Sequence[StoredProvider]) -> None:
    """Load specialties, locations and credentials of the providers."""
    if not providers:
        return
    by_id = {provider.id: provider for provider in providers}
    for provider in providers:
        provider.specialties, provider.location_ids, provider.credentials = [], [], []
    ids = list(by_id)
    for row in await tx.fetch(
        "SELECT provider_id, name, taxonomy_code, is_primary FROM provider.provider_specialties WHERE provider_id = ANY(:ids)",
        {"ids": ids},
    ):
        by_id[row.provider_id].specialties.append(
            Specialty.model_construct(name=row.name, taxonomy_code=row.taxonomy_code, is_primary=row.is_primary)
        )
    for row in await tx.fetch(
        """
        SELECT provider_id, clinic_location_id FROM provider.provider_locations WHERE provider_id = ANY(:ids)
        ORDER BY provider_id, clinic_location_id
        """,
        {"ids": ids},
    ):
        by_id[row.provider_id].location_ids.append(row.clinic_location_id)
    for row in await tx.fetch(
        """
        SELECT provider_id, id, type, issuing_authority, identifier, issued_on, expires_on, verification_status,
            verified_at, verification_notes, created_at
        FROM provider.provider_credentials WHERE provider_id = ANY(:ids) ORDER BY created_at, id
        """,
        {"ids": ids},
    ):
        by_id[row.provider_id].credentials.append(StoredCredential(_credential_view(row), row.created_at))
    for provider in providers:
        provider.specialties.sort(key=lambda s: (not s.is_primary, culture_key(s.name)))


def _credential_view(row: Row) -> Credential:
    """Build the API representation of a credential row."""
    return Credential.model_construct(
        id=row.id,
        type=CredentialType(row.type),
        issuing_authority=row.issuing_authority,
        identifier=row.identifier,
        issued_on=row.issued_on,
        expires_on=row.expires_on,
        verification_status=VerificationStatus(row.verification_status),
        verified_at=row.verified_at,
        verification_notes=row.verification_notes,
    )


async def load_provider(tx: Tx, organization_id: UUID | None, provider_id: UUID) -> StoredProvider:
    """Load a provider of the organization with its collections or raise not found."""
    row = await tx.fetchrow(
        f"SELECT {_PROVIDER_COLUMNS} FROM provider.providers WHERE id = :id AND organization_id = :organization",
        {"id": provider_id, "organization": organization_id},
    )
    if row is None:
        raise not_found("provider")
    provider = _stored(row)
    await load_children(tx, [provider])
    return provider


def _ensure_can_manage(caller: Caller, provider_id: UUID) -> None:
    """Allow administrators, or the provider acting on their own profile."""
    is_own = caller.has(Permission.PROVIDERS_MANAGE_OWN) and caller.is_own_provider(provider_id)
    if not caller.has(Permission.PROVIDERS_MANAGE) and not is_own:
        caller.require(Permission.PROVIDERS_MANAGE)


def _sort_clause(sort: str | None, alias: str) -> str:
    """Translate the ``lastName``/``createdAt`` sort expression into ORDER BY columns."""
    order = parse_sort(sort, ["lastName", "createdAt"], "lastName")[0]
    if order.name == "createdAt":
        return f"{alias}.created_at DESC, {alias}.id" if order.descending else f"{alias}.created_at, {alias}.id"
    if order.descending:
        return f"{alias}.last_name DESC, {alias}.first_name DESC, {alias}.id"
    return f"{alias}.last_name, {alias}.first_name, {alias}.id"


class ProviderService:
    """Implements the provider profile, credential and directory use cases."""

    def __init__(self, db: Database, access: AccessResolver, clock: Clock) -> None:
        """Wire the service to its ports."""
        self._db = db
        self._access = access
        self._clock = clock

    def _today(self) -> date:
        """Return the current UTC date."""
        return self._clock().astimezone(UTC).date()

    async def list_providers(
        self,
        caller: Caller,
        search: str | None,
        specialty: str | None,
        location_id: UUID | None,
        status: ProviderStatus | None,
        sort: str | None,
        page: PageRequest,
    ) -> Paged[Provider]:
        """List providers of the selected organization; patients and guests only see active providers."""
        caller.require(Permission.PROVIDERS_READ)
        if not caller.has(Permission.SCHEDULES_READ):
            status = ProviderStatus.ACTIVE
        args = Args()
        conditions = [f"p.organization_id = {args.add(caller.organization_id)}"]
        if status is not None:
            conditions.append(f"p.status = {args.add(status.value)}")
        if not is_blank(search):
            pattern = args.add(like_contains(search or ""))
            conditions.append(
                f"(lower(p.last_name) LIKE {pattern} ESCAPE '\\' OR lower(p.first_name) LIKE {pattern} ESCAPE '\\')"
            )
        if not is_blank(specialty):
            conditions.append(
                "EXISTS (SELECT 1 FROM provider.provider_specialties s WHERE s.provider_id = p.id AND lower(s.name) "
                f"LIKE {args.add(like_contains(specialty or ''))} ESCAPE '\\')"
            )
        if location_id is not None:
            conditions.append(
                "EXISTS (SELECT 1 FROM provider.provider_locations l WHERE l.provider_id = p.id "
                f"AND l.clinic_location_id = {args.add(location_id)})"
            )
        order_by = _sort_clause(sort, "p")
        clause = where(conditions)
        columns = ", ".join("p." + column.strip() for column in _PROVIDER_COLUMNS.split(","))
        async with self._db.transaction(caller.scope()) as tx:
            total = await tx.fetchval(f"SELECT count(*) FROM provider.providers p{clause}", args.values)
            limit, offset = args.add(page.safe_page_size), args.add(page.offset)
            rows = await tx.fetch(
                f"SELECT {columns} FROM provider.providers p{clause} ORDER BY {order_by} LIMIT {limit} OFFSET {offset}",
                args.values,
            )
            providers = [_stored(row) for row in rows]
            await load_children(tx, providers)
        today = self._today()
        return paged([provider.view(today) for provider in providers], page, total)

    async def get(self, caller: Caller, provider_id: UUID) -> Provider:
        """Return one provider profile; inactive profiles are hidden from patients and guests."""
        caller.require(Permission.PROVIDERS_READ)
        async with self._db.transaction(caller.scope()) as tx:
            provider = await load_provider(tx, caller.organization_id, provider_id)
        if provider.status != ProviderStatus.ACTIVE and not caller.has(Permission.SCHEDULES_READ):
            raise not_found("provider")
        return provider.view(self._today())

    async def _validate_links(
        self, tx: Tx, organization_id: UUID, request: ProviderRequest, current_user: UUID | None
    ) -> list[UUID] | None:
        """Check the linked user and the locations of a request; return the distinct location ids."""
        if request.user_id != current_user and request.user_id is not None:
            is_provider_member = await tx.fetchval(
                """
                SELECT EXISTS (SELECT 1 FROM organization.organization_memberships
                    WHERE organization_id = :organization AND user_id = :user AND status = 'active'
                      AND 'provider' = ANY(roles))
                """,
                {"organization": organization_id, "user": request.user_id},
            )
            if not is_provider_member:
                raise rule(
                    "user_not_provider_member",
                    "The user must be an active member of the organization with the provider role.",
                )
        if request.location_ids is None:
            return None
        ids = list(dict.fromkeys(request.location_ids))
        known = await tx.fetchval(
            "SELECT count(*) FROM organization.clinic_locations WHERE organization_id = :organization AND id = ANY(:ids)",
            {"organization": organization_id, "ids": ids},
        )
        if known != len(ids):
            raise field_error("locationIds", "One or more locations do not exist in this organization.")
        return ids

    @staticmethod
    async def _save_collections(
        tx: Tx,
        provider_id: UUID,
        organization_id: UUID,
        request: ProviderRequest,
        location_ids: list[UUID] | None,
        existing_locations: list[UUID],
    ) -> None:
        """Replace the specialty set and reconcile the location set when the request carries them."""
        if request.specialties is not None:
            await tx.execute("DELETE FROM provider.provider_specialties WHERE provider_id = :id", {"id": provider_id})
            for specialty in request.specialties:
                await tx.execute(
                    """
                    INSERT INTO provider.provider_specialties (id, organization_id, provider_id, name, taxonomy_code,
                        is_primary)
                    VALUES (:id, :organization, :provider, :name, :code, :primary)
                    """,
                    {
                        "id": new_id(),
                        "organization": organization_id,
                        "provider": provider_id,
                        "name": specialty.name.strip(),
                        "code": trimmed(specialty.taxonomy_code),
                        "primary": specialty.is_primary,
                    },
                )
        if location_ids is not None:
            await tx.execute(
                """
                DELETE FROM provider.provider_locations
                WHERE provider_id = :id AND NOT (clinic_location_id = ANY(:ids))
                """,
                {"id": provider_id, "ids": location_ids},
            )
            for location_id in location_ids:
                if location_id in existing_locations:
                    continue
                await tx.execute(
                    """
                    INSERT INTO provider.provider_locations (provider_id, clinic_location_id, organization_id)
                    VALUES (:provider, :location, :organization)
                    """,
                    {"provider": provider_id, "location": location_id, "organization": organization_id},
                )

    async def create(self, caller: Caller, request: ProviderRequest) -> Provider:
        """Create a provider profile in the onboarding state; activation later needs a verified license."""
        caller.require(Permission.PROVIDERS_MANAGE)
        organization_id = caller.require_organization_id()
        now = self._clock()
        provider_id = new_id()
        async with self._db.transaction(caller.scope()) as tx:
            location_ids = await self._validate_links(tx, organization_id, request, None)
            await tx.execute(
                """
                INSERT INTO provider.providers (id, organization_id, user_id, first_name, last_name, professional_title,
                    npi, biography, default_appointment_duration_minutes, status, accepting_new_patients,
                    listed_in_directory, created_at, updated_at, version)
                VALUES (:id, :organization, :user, :first, :last, :title, :npi, :biography, :duration, 'onboarding',
                    :accepting, :listed, :now, :now, 1)
                """,
                {
                    "id": provider_id,
                    "organization": organization_id,
                    "user": request.user_id,
                    "first": request.first_name.strip(),
                    "last": request.last_name.strip(),
                    "title": trimmed(request.professional_title),
                    "npi": request.npi,
                    "biography": trimmed(request.biography),
                    "duration": request.default_appointment_duration_minutes,
                    "accepting": request.accepting_new_patients,
                    "listed": request.listed_in_directory,
                    "now": now,
                },
            )
            await self._save_collections(tx, provider_id, organization_id, request, location_ids, [])
            await audit(tx, caller, now, AuditEntry("provider.created", "provider", provider_id))
            provider = await load_provider(tx, organization_id, provider_id)
        await self._invalidate_linked_user(caller, request.user_id)
        return provider.view(self._today())

    async def update(self, caller: Caller, provider_id: UUID, request: ProviderRequest) -> Provider:
        """Update a provider profile; providers may edit their own profile but not the linked account."""
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            provider = await load_provider(tx, caller.organization_id, provider_id)
            is_administrator = caller.has(Permission.PROVIDERS_MANAGE)
            _ensure_can_manage(caller, provider.id)
            if not is_administrator and request.user_id != provider.user_id:
                raise forbidden("forbidden", "Only an administrator can change the linked user account.")
            ensure_version(provider.version, require_version(request.version))
            previous_user = provider.user_id
            location_ids = await self._validate_links(tx, provider.organization_id, request, provider.user_id)
            row = provider.row
            values = (
                request.first_name.strip(),
                request.last_name.strip(),
                trimmed(request.professional_title),
                request.npi,
                trimmed(request.biography),
                request.default_appointment_duration_minutes,
                request.accepting_new_patients,
                request.listed_in_directory,
                request.user_id,
            )
            stored = (
                row.first_name,
                row.last_name,
                row.professional_title,
                row.npi,
                row.biography,
                row.default_appointment_duration_minutes,
                row.accepting_new_patients,
                row.listed_in_directory,
                row.user_id,
            )
            if values != stored:
                affected = await tx.execute(
                    """
                    UPDATE provider.providers SET user_id = :user, first_name = :first, last_name = :last,
                        professional_title = :title, npi = :npi, biography = :biography,
                        default_appointment_duration_minutes = :duration, accepting_new_patients = :accepting,
                        listed_in_directory = :listed, version = version + 1, updated_at = :now
                    WHERE id = :id AND version = :expected
                    """,
                    {
                        "id": provider.id,
                        "expected": provider.version,
                        "user": values[8],
                        "first": values[0],
                        "last": values[1],
                        "title": values[2],
                        "npi": values[3],
                        "biography": values[4],
                        "duration": values[5],
                        "accepting": values[6],
                        "listed": values[7],
                        "now": now,
                    },
                )
                if affected == 0:
                    raise concurrency_conflict()
            await self._save_collections(
                tx, provider.id, provider.organization_id, request, location_ids, list(provider.location_ids)
            )
            await audit(tx, caller, now, AuditEntry("provider.updated", "provider", provider.id))
            updated = await load_provider(tx, caller.organization_id, provider.id)
        await self._invalidate_linked_user(caller, previous_user)
        await self._invalidate_linked_user(caller, updated.user_id)
        return updated.view(self._today())

    async def _invalidate_linked_user(self, caller: Caller, user_id: UUID | None) -> None:
        """Refresh cached access for a user whose provider link changed."""
        if user_id is not None and caller.organization_id is not None:
            await self._access.invalidate_access(caller.organization_id, user_id)

    async def _set_status(
        self, caller: Caller, provider_id: UUID, action: str, decide: Callable[[StoredProvider], ProviderStatus]
    ) -> Provider:
        """Apply a status change computed from the loaded provider."""
        caller.require(Permission.PROVIDERS_MANAGE)
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            provider = await load_provider(tx, caller.organization_id, provider_id)
            try:
                status = decide(provider)
            except DomainError as error:
                raise from_domain(error) from error
            if status != provider.status:
                await tx.execute(
                    """
                    UPDATE provider.providers SET status = :status, version = version + 1, updated_at = :now
                    WHERE id = :id
                    """,
                    {"id": provider.id, "status": status.value, "now": now},
                )
                provider.status, provider.version, provider.updated_at = status, provider.version + 1, now
            await audit(tx, caller, now, AuditEntry(action, "provider", provider.id))
        return provider.view(self._today())

    async def activate(self, caller: Caller, provider_id: UUID) -> Provider:
        """Activate a provider so appointments can be booked; requires a verified, current license."""
        today = self._today()

        def decide(provider: StoredProvider) -> ProviderStatus:
            """Allow activation only with a verified license."""
            ensure_can_activate(provider.standings(), today)
            return ProviderStatus.ACTIVE

        return await self._set_status(caller, provider_id, "provider.activated", decide)

    async def deactivate(self, caller: Caller, provider_id: UUID) -> Provider:
        """Deactivate a provider; existing appointments stay but no new ones can be booked."""
        return await self._set_status(caller, provider_id, "provider.deactivated", lambda _: ProviderStatus.INACTIVE)

    async def list_credentials(self, caller: Caller, provider_id: UUID) -> list[Credential]:
        """List a provider's credentials for administrators, reviewers and the provider."""
        if not caller.has(Permission.CREDENTIALS_VERIFY):
            _ensure_can_manage(caller, provider_id)
        async with self._db.transaction(caller.scope()) as tx:
            provider = await load_provider(tx, caller.organization_id, provider_id)
        return [credential.view for credential in provider.credentials]

    async def add_credential(self, caller: Caller, provider_id: UUID, request: CredentialRequest) -> Credential:
        """Add a credential awaiting verification."""
        _ensure_can_manage(caller, provider_id)
        now = self._clock()
        credential = Credential.model_construct(
            id=new_id(),
            type=request.type,
            issuing_authority=request.issuing_authority.strip(),
            identifier=request.identifier.strip(),
            issued_on=request.issued_on,
            expires_on=request.expires_on,
            verification_status=VerificationStatus.PENDING,
        )
        async with self._db.transaction(caller.scope()) as tx:
            provider = await load_provider(tx, caller.organization_id, provider_id)
            await tx.execute(
                """
                INSERT INTO provider.provider_credentials (id, organization_id, provider_id, type, issuing_authority,
                    identifier, issued_on, expires_on, verification_status, created_at, updated_at)
                VALUES (:id, :organization, :provider, :type, :authority, :identifier, :issued, :expires, 'pending',
                    :now, :now)
                """,
                {
                    "id": credential.id,
                    "organization": provider.organization_id,
                    "provider": provider.id,
                    "type": request.type.value,
                    "authority": credential.issuing_authority,
                    "identifier": credential.identifier,
                    "issued": request.issued_on,
                    "expires": request.expires_on,
                    "now": now,
                },
            )
            await audit(
                tx,
                caller,
                now,
                AuditEntry(
                    "credential.added",
                    "provider_credential",
                    credential.id,
                    metadata={"providerId": str(provider_id), "type": request.type.value},
                ),
            )
        return credential

    async def review_credential(
        self, caller: Caller, provider_id: UUID, credential_id: UUID, request: CredentialReviewRequest
    ) -> Credential:
        """Record the outcome of verifying a credential; reviewers cannot verify their own credentials."""
        caller.require(Permission.CREDENTIALS_VERIFY)
        reviewer_id = caller.require_user_id()
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            provider = await load_provider(tx, caller.organization_id, provider_id)
            if provider.user_id is not None and provider.user_id == reviewer_id:
                raise forbidden("self_verification_not_allowed", "You cannot verify your own credentials.")
            stored = next((c for c in provider.credentials if c.view.id == credential_id), None)
            if stored is None:
                raise not_found("credential")
            try:
                ensure_final_decision(request.decision)
            except DomainError as error:
                raise from_domain(error) from error
            notes = trimmed(request.notes)
            stored.view = stored.view.model_copy(
                update={"verification_status": request.decision, "verified_at": now, "verification_notes": notes}
            )
            await tx.execute(
                """
                UPDATE provider.provider_credentials SET verification_status = :decision, verified_by_user_id = :reviewer,
                    verified_at = :now, verification_notes = :notes, updated_at = :now
                WHERE id = :id
                """,
                {
                    "id": credential_id,
                    "decision": request.decision.value,
                    "reviewer": reviewer_id,
                    "now": now,
                    "notes": notes,
                },
            )
            if provider.status == ProviderStatus.ACTIVE and not has_verified_license(
                provider.standings(), self._today()
            ):
                await tx.execute(
                    """
                    UPDATE provider.providers SET status = 'onboarding', version = version + 1, updated_at = :now
                    WHERE id = :id
                    """,
                    {"id": provider.id, "now": now},
                )
            await audit(
                tx,
                caller,
                now,
                AuditEntry(
                    "credential.reviewed",
                    "provider_credential",
                    credential_id,
                    metadata={"providerId": str(provider_id), "decision": request.decision.value},
                ),
            )
        return stored.view

    async def search_directory(
        self,
        caller: Caller,
        search: str | None,
        specialty: str | None,
        city: str | None,
        state: str | None,
        page: PageRequest,
    ) -> Paged[DirectoryProvider]:
        """Search listed, active providers of organizations that accept online scheduling (public data only)."""
        caller.require_user_id()
        args = Args()
        conditions = [
            "p.status = 'active'",
            "p.listed_in_directory",
            "o.status = 'active'",
            "st.allow_patient_self_scheduling",
        ]
        if not is_blank(search):
            pattern = args.add(like_contains(search or ""))
            conditions.append(
                f"(lower(p.last_name) LIKE {pattern} ESCAPE '\\' OR lower(p.first_name) LIKE {pattern} ESCAPE '\\')"
            )
        if not is_blank(specialty):
            conditions.append(
                "EXISTS (SELECT 1 FROM provider.provider_specialties s WHERE s.provider_id = p.id AND lower(s.name) "
                f"LIKE {args.add(like_contains(specialty or ''))} ESCAPE '\\')"
            )
        if not is_blank(city) or not is_blank(state):
            city_pattern = like_starts_with(city or "") if not is_blank(city) else "%"
            state_code = state.strip().upper() if state is not None else None
            state_param = args.add(state_code)
            conditions.append(
                "EXISTS (SELECT 1 FROM provider.provider_locations pl "
                "JOIN organization.clinic_locations l ON l.id = pl.clinic_location_id "
                f"WHERE pl.provider_id = p.id AND l.status = 'active' AND (CAST({state_param} AS text) IS NULL "
                f"OR l.state = {state_param}) AND lower(l.city) LIKE {args.add(city_pattern)} ESCAPE '\\')"
            )
        source = (
            " FROM provider.providers p JOIN organization.organizations o ON o.id = p.organization_id "
            "JOIN organization.organization_settings st ON st.organization_id = p.organization_id" + where(conditions)
        )
        async with self._db.transaction(caller.system_scope()) as tx:
            total = await tx.fetchval("SELECT count(*)" + source, args.values)
            limit, offset = args.add(page.safe_page_size), args.add(page.offset)
            rows = await tx.fetch(
                "SELECT p.id, p.organization_id, o.name AS organization_name, p.first_name, p.last_name, "
                "p.professional_title, p.accepting_new_patients"
                + source
                + f" ORDER BY p.last_name, p.first_name, p.id LIMIT {limit} OFFSET {offset}",
                args.values,
            )
            specialties: dict[UUID, list[str]] = {row.id: [] for row in rows}
            locations: dict[UUID, list[DirectoryLocation]] = {row.id: [] for row in rows}
            if rows:
                ids = list(specialties)
                for row in await tx.fetch(
                    "SELECT provider_id, name FROM provider.provider_specialties WHERE provider_id = ANY(:ids)",
                    {"ids": ids},
                ):
                    specialties[row.provider_id].append(row.name)
                for row in await tx.fetch(
                    """
                    SELECT pl.provider_id, l.id, l.name, l.city, l.state, l.time_zone
                    FROM provider.provider_locations pl JOIN organization.clinic_locations l ON l.id = pl.clinic_location_id
                    WHERE pl.provider_id = ANY(:ids) AND l.status = 'active'
                    ORDER BY pl.provider_id, l.id
                    """,
                    {"ids": ids},
                ):
                    locations[row.provider_id].append(
                        DirectoryLocation.model_construct(
                            id=row.id, name=row.name, city=row.city, state=row.state, time_zone=row.time_zone
                        )
                    )
        items = [
            DirectoryProvider.model_construct(
                id=row.id,
                organization_id=row.organization_id,
                organization_name=row.organization_name,
                first_name=row.first_name,
                last_name=row.last_name,
                professional_title=row.professional_title,
                accepting_new_patients=row.accepting_new_patients,
                specialties=sorted(specialties[row.id], key=culture_key),
                locations=locations[row.id],
            )
            for row in rows
        ]
        return paged(items, page, total)
