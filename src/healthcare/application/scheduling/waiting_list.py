"""Waiting list use cases: staff manage every entry, patients manage their own."""

from typing import Final
from uuid import UUID

from healthcare.application.core.audit import Args, AuditEntry, audit, where
from healthcare.application.core.caller import Caller
from healthcare.application.core.errors import conflict, field_error, not_found
from healthcare.application.core.paging import Paged, PageRequest, paged
from healthcare.application.core.ports import Clock, Database, Row, Tx, new_id
from healthcare.application.core.validation import trimmed
from healthcare.application.scheduling.contracts import WaitingListEntry, WaitingListRequest
from healthcare.domain.authorization import Permission
from healthcare.domain.enums import WaitingListStatus

_COLUMNS: Final = (
    "e.id, e.patient_id, e.provider_id, e.clinic_location_id, e.earliest_date, e.latest_date, e.notes, e.status, "
    "e.fulfilled_appointment_id, e.created_at"
)


def _entry_view(row: Row) -> WaitingListEntry:
    """Build the API representation of a waiting list row."""
    return WaitingListEntry.model_construct(
        id=row.id,
        patient_id=row.patient_id,
        provider_id=row.provider_id,
        location_id=row.clinic_location_id,
        earliest_date=row.earliest_date,
        latest_date=row.latest_date,
        notes=row.notes,
        status=WaitingListStatus(row.status),
        fulfilled_appointment_id=row.fulfilled_appointment_id,
        created_at=row.created_at,
    )


def _visible(caller: Caller, args: Args) -> str:
    """Limit entries (alias ``e``) to those the caller may see: all for staff, own for patients."""
    organization = f"e.organization_id = {args.add(caller.organization_id)}"
    if caller.has_any(Permission.WAITING_LIST_MANAGE, Permission.APPOINTMENTS_READ):
        return organization
    caller.require(Permission.APPOINTMENTS_SELF)
    return f"{organization} AND e.patient_id = ANY({args.add(list(caller.patient_ids))})"


def _ensure_can_manage(caller: Caller, patient_id: UUID) -> None:
    """Allow staff with waiting list access, or the patient acting for themselves."""
    if caller.has(Permission.APPOINTMENTS_SELF) and caller.is_linked_patient(patient_id):
        return
    caller.require(Permission.WAITING_LIST_MANAGE)


async def _exists(tx: Tx, table: str, record_id: UUID, organization_id: UUID | None) -> bool:
    """Return whether a row of the organization exists in the table."""
    return bool(
        await tx.fetchval(
            f"SELECT EXISTS (SELECT 1 FROM {table} WHERE id = :id AND organization_id = :organization)",
            {"id": record_id, "organization": organization_id},
        )
    )


class WaitingListService:
    """Implements the waiting list use cases."""

    def __init__(self, db: Database, clock: Clock) -> None:
        """Wire the service to its ports."""
        self._db = db
        self._clock = clock

    async def list_entries(
        self, caller: Caller, status: WaitingListStatus | None, provider_id: UUID | None, page: PageRequest
    ) -> Paged[WaitingListEntry]:
        """List waiting list entries: all for staff, only their own for patients."""
        args = Args()
        conditions = [_visible(caller, args)]
        if status is not None:
            conditions.append(f"e.status = {args.add(status.value)}")
        if provider_id is not None:
            conditions.append(f"(e.provider_id = {args.add(provider_id)} OR e.provider_id IS NULL)")
        clause = where(conditions)
        async with self._db.transaction(caller.scope()) as tx:
            total = await tx.fetchval(f"SELECT count(*) FROM scheduling.waiting_list_entries e{clause}", args.values)
            limit, offset = args.add(page.safe_page_size), args.add(page.offset)
            rows = await tx.fetch(
                f"SELECT {_COLUMNS} FROM scheduling.waiting_list_entries e{clause} ORDER BY e.created_at, e.id "
                f"LIMIT {limit} OFFSET {offset}",
                args.values,
            )
        return paged([_entry_view(row) for row in rows], page, total)

    async def create(self, caller: Caller, request: WaitingListRequest) -> WaitingListEntry:
        """Add a patient to the waiting list; patients may add themselves."""
        _ensure_can_manage(caller, request.patient_id)
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            if not await _exists(tx, "patient.patients", request.patient_id, caller.organization_id):
                raise field_error("patientId", "The patient does not exist in this organization.")
            if request.provider_id is not None and not await _exists(
                tx, "provider.providers", request.provider_id, caller.organization_id
            ):
                raise field_error("providerId", "The provider does not exist in this organization.")
            if request.location_id is not None and not await _exists(
                tx, "organization.clinic_locations", request.location_id, caller.organization_id
            ):
                raise field_error("locationId", "The location does not exist in this organization.")
            organization_id = caller.require_organization_id()
            entry = WaitingListEntry.model_construct(
                id=new_id(),
                patient_id=request.patient_id,
                provider_id=request.provider_id,
                location_id=request.location_id,
                earliest_date=request.earliest_date,
                latest_date=request.latest_date,
                notes=trimmed(request.notes),
                status=WaitingListStatus.ACTIVE,
                created_at=now,
            )
            await tx.execute(
                """
                INSERT INTO scheduling.waiting_list_entries (id, organization_id, patient_id, provider_id,
                    clinic_location_id, earliest_date, latest_date, notes, status, created_by_user_id, created_at,
                    updated_at)
                VALUES (:id, :organization, :patient, :provider, :location, :earliest, :latest, :notes, 'active', :user,
                    :now, :now)
                """,
                {
                    "id": entry.id,
                    "organization": organization_id,
                    "patient": request.patient_id,
                    "provider": request.provider_id,
                    "location": request.location_id,
                    "earliest": request.earliest_date,
                    "latest": request.latest_date,
                    "notes": entry.notes,
                    "user": caller.user_id,
                    "now": now,
                },
            )
            await audit(
                tx,
                caller,
                now,
                AuditEntry(
                    "waiting_list.created",
                    "waiting_list_entry",
                    entry.id,
                    metadata={"patientId": str(request.patient_id)},
                ),
            )
        return entry

    async def cancel(self, caller: Caller, entry_id: UUID) -> None:
        """Withdraw an active waiting list entry."""
        args = Args()
        condition = _visible(caller, args)
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            row = await tx.fetchrow(
                f"SELECT {_COLUMNS} FROM scheduling.waiting_list_entries e WHERE {condition} AND e.id = {args.add(entry_id)}",
                args.values,
            )
            if row is None:
                raise not_found("waiting list entry")
            _ensure_can_manage(caller, row.patient_id)
            if row.status != WaitingListStatus.ACTIVE:
                raise conflict("waiting_list_entry_closed", "Only active waiting list entries can be cancelled.")
            await tx.execute(
                "UPDATE scheduling.waiting_list_entries SET status = 'cancelled', updated_at = :now WHERE id = :id",
                {"id": row.id, "now": now},
            )
            await audit(tx, caller, now, AuditEntry("waiting_list.cancelled", "waiting_list_entry", row.id))
