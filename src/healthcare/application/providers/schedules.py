"""Weekly schedule and schedule exception use cases."""

from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from healthcare.application.core.audit import Args, AuditEntry, audit, where
from healthcare.application.core.caller import Caller
from healthcare.application.core.errors import conflict, field_error, not_found, rule
from healthcare.application.core.ports import Clock, Database, Row, Tx, new_id
from healthcare.application.core.validation import trimmed
from healthcare.application.providers.contracts import (
    Schedule,
    ScheduleException,
    ScheduleExceptionRequest,
    ScheduleRequest,
)
from healthcare.domain.authorization import Permission
from healthcare.domain.enums import ScheduleExceptionType
from healthcare.domain.rules import WeeklyBlock

_SCHEDULE_COLUMNS: Final = (
    "id, provider_id, clinic_location_id, day_of_week, start_time, end_time, slot_duration_minutes, effective_from, "
    "effective_to"
)
_EXCEPTION_COLUMNS: Final = (
    "id, provider_id, type, starts_at, ends_at, clinic_location_id, slot_duration_minutes, reason"
)


def _schedule_view(row: Row) -> Schedule:
    """Build the API representation of a schedule row."""
    return Schedule.model_construct(
        id=row.id,
        provider_id=row.provider_id,
        location_id=row.clinic_location_id,
        day_of_week=row.day_of_week,
        start_time=row.start_time,
        end_time=row.end_time,
        slot_duration_minutes=row.slot_duration_minutes,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
    )


def _exception_view(row: Row) -> ScheduleException:
    """Build the API representation of an exception row."""
    return ScheduleException.model_construct(
        id=row.id,
        provider_id=row.provider_id,
        type=ScheduleExceptionType(row.type),
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        location_id=row.clinic_location_id,
        slot_duration_minutes=row.slot_duration_minutes,
        reason=row.reason,
    )


def _ensure_can_manage_schedule(caller: Caller, provider_id: UUID) -> None:
    """Allow schedule administrators, or the provider acting on their own schedule."""
    if not (caller.has(Permission.SCHEDULES_MANAGE_OWN) and caller.is_own_provider(provider_id)):
        caller.require(Permission.SCHEDULES_MANAGE)


async def ensure_provider_at_location(
    tx: Tx, organization_id: UUID | None, provider_id: UUID, location_id: UUID
) -> None:
    """Require the provider to be assigned to the location."""
    assigned = await tx.fetchval(
        """
        SELECT EXISTS (SELECT 1 FROM provider.provider_locations
            WHERE organization_id = :organization AND provider_id = :provider AND clinic_location_id = :location)
        """,
        {"organization": organization_id, "provider": provider_id, "location": location_id},
    )
    if not assigned:
        raise rule("provider_not_at_location", "The provider does not practice at this location.")


async def _ensure_no_overlap(
    tx: Tx, organization_id: UUID | None, request: ScheduleRequest, exclude: UUID | None
) -> None:
    """Reject a block that overlaps another block of the same provider on the same weekday."""
    rows = await tx.fetch(
        f"""
        SELECT {_SCHEDULE_COLUMNS} FROM provider.provider_schedules
        WHERE organization_id = :organization AND provider_id = :provider AND day_of_week = :day
          AND (CAST(:exclude AS uuid) IS NULL OR id <> :exclude)
        """,
        {
            "organization": organization_id,
            "provider": request.provider_id,
            "day": request.day_of_week,
            "exclude": exclude,
        },
    )
    block = WeeklyBlock(
        request.day_of_week, request.start_time, request.end_time, request.effective_from, request.effective_to
    )
    for row in rows:
        other = WeeklyBlock(row.day_of_week, row.start_time, row.end_time, row.effective_from, row.effective_to)
        if other.overlaps(block):
            raise conflict("schedule_overlap", "The provider already has working hours that overlap this block.")


class ScheduleService:
    """Implements the weekly schedule and schedule exception use cases."""

    def __init__(self, db: Database, clock: Clock) -> None:
        """Wire the service to its ports."""
        self._db = db
        self._clock = clock

    async def list_schedules(self, caller: Caller, provider_id: UUID) -> list[Schedule]:
        """List the weekly schedule blocks of a provider."""
        caller.require(Permission.SCHEDULES_READ)
        async with self._db.transaction(caller.scope()) as tx:
            rows = await tx.fetch(
                f"""
                SELECT {_SCHEDULE_COLUMNS} FROM provider.provider_schedules
                WHERE organization_id = :organization AND provider_id = :provider ORDER BY day_of_week, start_time, id
                """,
                {"organization": caller.organization_id, "provider": provider_id},
            )
        return [_schedule_view(row) for row in rows]

    async def create_schedule(self, caller: Caller, request: ScheduleRequest) -> Schedule:
        """Add a weekly working block; blocks of one provider may not overlap, even across locations."""
        _ensure_can_manage_schedule(caller, request.provider_id)
        now = self._clock()
        schedule = Schedule.model_construct(
            id=new_id(),
            provider_id=request.provider_id,
            location_id=request.location_id,
            day_of_week=request.day_of_week,
            start_time=request.start_time,
            end_time=request.end_time,
            slot_duration_minutes=request.slot_duration_minutes,
            effective_from=request.effective_from,
            effective_to=request.effective_to,
        )
        async with self._db.transaction(caller.scope()) as tx:
            await ensure_provider_at_location(tx, caller.organization_id, request.provider_id, request.location_id)
            await _ensure_no_overlap(tx, caller.organization_id, request, None)
            organization_id = caller.require_organization_id()
            await tx.execute(
                """
                INSERT INTO provider.provider_schedules (id, organization_id, provider_id, clinic_location_id,
                    day_of_week, start_time, end_time, slot_duration_minutes, effective_from, effective_to, created_at,
                    updated_at)
                VALUES (:id, :organization, :provider, :location, :day, :start, :end, :slot, :from, :to, :now, :now)
                """,
                {
                    "id": schedule.id,
                    "organization": organization_id,
                    "provider": request.provider_id,
                    "location": request.location_id,
                    "day": request.day_of_week,
                    "start": request.start_time,
                    "end": request.end_time,
                    "slot": request.slot_duration_minutes,
                    "from": request.effective_from,
                    "to": request.effective_to,
                    "now": now,
                },
            )
            await audit(
                tx,
                caller,
                now,
                AuditEntry(
                    "schedule.created",
                    "provider_schedule",
                    schedule.id,
                    metadata={"providerId": str(request.provider_id)},
                ),
            )
        return schedule

    @staticmethod
    async def _find_schedule(tx: Tx, organization_id: UUID | None, schedule_id: UUID) -> Row:
        """Load a schedule of the organization or raise not found."""
        row = await tx.fetchrow(
            f"""
            SELECT {_SCHEDULE_COLUMNS} FROM provider.provider_schedules
            WHERE id = :id AND organization_id = :organization
            """,
            {"id": schedule_id, "organization": organization_id},
        )
        if row is None:
            raise not_found("schedule")
        return row

    async def update_schedule(self, caller: Caller, schedule_id: UUID, request: ScheduleRequest) -> Schedule:
        """Change a weekly working block; the provider of a block cannot be changed."""
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            schedule = await self._find_schedule(tx, caller.organization_id, schedule_id)
            _ensure_can_manage_schedule(caller, schedule.provider_id)
            if request.provider_id != schedule.provider_id:
                raise field_error("providerId", "A schedule cannot be moved to another provider.")
            await ensure_provider_at_location(tx, caller.organization_id, schedule.provider_id, request.location_id)
            await _ensure_no_overlap(tx, caller.organization_id, request, schedule.id)
            await tx.execute(
                """
                UPDATE provider.provider_schedules SET clinic_location_id = :location, day_of_week = :day,
                    start_time = :start, end_time = :end, slot_duration_minutes = :slot, effective_from = :from,
                    effective_to = :to, updated_at = :now
                WHERE id = :id
                """,
                {
                    "id": schedule.id,
                    "location": request.location_id,
                    "day": request.day_of_week,
                    "start": request.start_time,
                    "end": request.end_time,
                    "slot": request.slot_duration_minutes,
                    "from": request.effective_from,
                    "to": request.effective_to,
                    "now": now,
                },
            )
            await audit(
                tx,
                caller,
                now,
                AuditEntry(
                    "schedule.updated",
                    "provider_schedule",
                    schedule.id,
                    metadata={"providerId": str(schedule.provider_id)},
                ),
            )
        return Schedule.model_construct(
            id=schedule.id,
            provider_id=schedule.provider_id,
            location_id=request.location_id,
            day_of_week=request.day_of_week,
            start_time=request.start_time,
            end_time=request.end_time,
            slot_duration_minutes=request.slot_duration_minutes,
            effective_from=request.effective_from,
            effective_to=request.effective_to,
        )

    async def delete_schedule(self, caller: Caller, schedule_id: UUID) -> None:
        """Remove a weekly working block; existing appointments are not affected."""
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            schedule = await self._find_schedule(tx, caller.organization_id, schedule_id)
            _ensure_can_manage_schedule(caller, schedule.provider_id)
            await tx.execute("DELETE FROM provider.provider_schedules WHERE id = :id", {"id": schedule.id})
            await audit(
                tx,
                caller,
                now,
                AuditEntry(
                    "schedule.deleted",
                    "provider_schedule",
                    schedule.id,
                    metadata={"providerId": str(schedule.provider_id)},
                ),
            )

    async def list_exceptions(
        self, caller: Caller, provider_id: UUID, starts: datetime | None, ends: datetime | None
    ) -> list[ScheduleException]:
        """List a provider's schedule exceptions that overlap the period (at most 500)."""
        caller.require(Permission.SCHEDULES_READ)
        args = Args()
        conditions = [f"organization_id = {args.add(caller.organization_id)}", f"provider_id = {args.add(provider_id)}"]
        if starts is not None:
            conditions.append(f"ends_at > {args.add(starts)}")
        if ends is not None:
            conditions.append(f"starts_at < {args.add(ends)}")
        async with self._db.transaction(caller.scope()) as tx:
            rows = await tx.fetch(
                f"SELECT {_EXCEPTION_COLUMNS} FROM provider.schedule_exceptions{where(conditions)} "
                "ORDER BY starts_at, id LIMIT 500",
                args.values,
            )
        return [_exception_view(row) for row in rows]

    async def create_exception(self, caller: Caller, request: ScheduleExceptionRequest) -> ScheduleException:
        """Record time off or additional hours; time off reports how many booked appointments it affects."""
        _ensure_can_manage_schedule(caller, request.provider_id)
        now = self._clock()
        starts_at, ends_at = request.starts_at.astimezone(UTC), request.ends_at.astimezone(UTC)
        additional = request.type == ScheduleExceptionType.ADDITIONAL_HOURS
        location_id = request.location_id if additional else None
        exception_id = new_id()
        affected: int | None = None
        async with self._db.transaction(caller.scope()) as tx:
            exists = await tx.fetchval(
                "SELECT EXISTS (SELECT 1 FROM provider.providers WHERE id = :id AND organization_id = :organization)",
                {"id": request.provider_id, "organization": caller.organization_id},
            )
            if not exists:
                raise not_found("provider")
            if additional and location_id is not None:
                await ensure_provider_at_location(tx, caller.organization_id, request.provider_id, location_id)
            organization_id = caller.require_organization_id()
            await tx.execute(
                """
                INSERT INTO provider.schedule_exceptions (id, organization_id, provider_id, clinic_location_id, type,
                    starts_at, ends_at, slot_duration_minutes, reason, created_by_user_id, created_at)
                VALUES (:id, :organization, :provider, :location, :type, :starts, :ends, :slot, :reason, :user, :now)
                """,
                {
                    "id": exception_id,
                    "organization": organization_id,
                    "provider": request.provider_id,
                    "location": location_id,
                    "type": request.type.value,
                    "starts": starts_at,
                    "ends": ends_at,
                    "slot": request.slot_duration_minutes,
                    "reason": trimmed(request.reason),
                    "user": caller.user_id,
                    "now": now,
                },
            )
            await audit(
                tx,
                caller,
                now,
                AuditEntry(
                    "schedule_exception.created",
                    "schedule_exception",
                    exception_id,
                    metadata={"providerId": str(request.provider_id), "type": request.type.value},
                ),
            )
            if request.type == ScheduleExceptionType.TIME_OFF:
                affected = int(
                    await tx.fetchval(
                        """
                    SELECT count(*) FROM scheduling.appointments
                    WHERE organization_id = :organization AND provider_id = :provider
                      AND status NOT IN ('cancelled', 'no_show') AND starts_at < :ends AND ends_at > :starts
                    """,
                        {
                            "organization": organization_id,
                            "provider": request.provider_id,
                            "starts": starts_at,
                            "ends": ends_at,
                        },
                    )
                )
        return ScheduleException.model_construct(
            id=exception_id,
            provider_id=request.provider_id,
            type=request.type,
            starts_at=starts_at,
            ends_at=ends_at,
            location_id=location_id,
            slot_duration_minutes=request.slot_duration_minutes,
            reason=trimmed(request.reason),
            affected_appointments=affected,
        )

    async def delete_exception(self, caller: Caller, exception_id: UUID) -> None:
        """Remove a schedule exception."""
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            provider_id = await tx.fetchval(
                "SELECT provider_id FROM provider.schedule_exceptions WHERE id = :id AND organization_id = :organization",
                {"id": exception_id, "organization": caller.organization_id},
            )
            if provider_id is None:
                raise not_found("schedule exception")
            _ensure_can_manage_schedule(caller, provider_id)
            await tx.execute("DELETE FROM provider.schedule_exceptions WHERE id = :id", {"id": exception_id})
            await audit(
                tx,
                caller,
                now,
                AuditEntry(
                    "schedule_exception.deleted",
                    "schedule_exception",
                    exception_id,
                    metadata={"providerId": str(provider_id)},
                ),
            )
