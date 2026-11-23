"""Open slot computation shared by the availability endpoint and booking validation."""

from datetime import date, datetime, timedelta
from typing import Final
from uuid import UUID

from healthcare.application.core.caller import Caller
from healthcare.application.core.errors import field_error, not_found
from healthcare.application.core.ports import Clock, Database, Tx
from healthcare.application.organizations.service import load_settings
from healthcare.application.scheduling.contracts import AvailabilityResponse, Slot
from healthcare.domain.authorization import Permission
from healthcare.domain.calendar import day_number, load_zone
from healthcare.domain.enums import ProviderStatus, ScheduleExceptionType
from healthcare.domain.slots import (
    AdditionalAvailability,
    LocationCalendar,
    SlotQuery,
    TimeRange,
    WeeklyAvailability,
    generate_slots,
)

MAX_RANGE_DAYS: Final = 31


def is_staff(caller: Caller) -> bool:
    """Return whether the caller books on behalf of the organization rather than as a patient."""
    return caller.has_any(Permission.APPOINTMENTS_MANAGE, Permission.APPOINTMENTS_MANAGE_OWN)


async def open_slots(
    tx: Tx,
    caller: Caller,
    now: datetime,
    provider_id: UUID,
    location_id: UUID | None,
    date_from: date,
    date_to: date,
    patient_rules: bool,
    ignore_appointment: UUID | None,
) -> list[Slot]:
    """Compute open slots from working hours, exceptions, holidays and existing appointments.

    Patient rules add the minimum booking notice; staff may book any slot that has not ended.
    """
    organization_id = caller.organization_id
    provider = await tx.fetchrow(
        """
        SELECT status, default_appointment_duration_minutes FROM provider.providers
        WHERE id = :id AND organization_id = :organization
        """,
        {"id": provider_id, "organization": organization_id},
    )
    if provider is None:
        raise not_found("provider")
    if provider.status != ProviderStatus.ACTIVE:
        return []
    assert organization_id is not None
    settings = await load_settings(tx, organization_id)
    if settings is None:
        raise not_found("organization")
    default_minutes = (
        provider.default_appointment_duration_minutes
        if provider.default_appointment_duration_minutes is not None
        else settings.default_appointment_duration_minutes
    )
    location_rows = await tx.fetch(
        """
        SELECT l.id, l.time_zone FROM provider.provider_locations pl
        JOIN organization.clinic_locations l ON l.id = pl.clinic_location_id
        WHERE pl.organization_id = :organization AND pl.provider_id = :provider AND l.status = 'active'
          AND (CAST(:location AS uuid) IS NULL OR l.id = :location)
        """,
        {"organization": organization_id, "provider": provider_id, "location": location_id},
    )
    if not location_rows:
        return []
    zones = {row.id: row.time_zone for row in location_rows}
    location_ids = list(zones)
    params = {
        "organization": organization_id,
        "provider": provider_id,
        "ids": location_ids,
        "from": date_from,
        "to": date_to,
    }
    holidays = await tx.fetch(
        """
        SELECT date, clinic_location_id FROM organization.clinic_holidays
        WHERE organization_id = :organization AND date >= :from AND date <= :to
          AND (clinic_location_id IS NULL OR clinic_location_id = ANY(:ids))
        """,
        params,
    )
    weekly = [
        WeeklyAvailability(
            row.clinic_location_id,
            row.day_of_week,
            row.start_time,
            row.end_time,
            row.slot_duration_minutes if row.slot_duration_minutes is not None else default_minutes,
            row.effective_from,
            row.effective_to,
        )
        for row in await tx.fetch(
            """
            SELECT clinic_location_id, day_of_week, start_time, end_time, slot_duration_minutes, effective_from,
                effective_to
            FROM provider.provider_schedules
            WHERE organization_id = :organization AND provider_id = :provider AND clinic_location_id = ANY(:ids)
              AND effective_from <= :to AND (effective_to IS NULL OR effective_to >= :from)
            """,
            params,
        )
    ]
    # A one-day margin on each side covers every time zone offset.
    window = {
        "organization": organization_id,
        "provider": provider_id,
        "start": datetime.combine(date_from - timedelta(days=1), datetime.min.time(), tzinfo=now.tzinfo),
        "end": datetime.combine(date_to + timedelta(days=2), datetime.min.time(), tzinfo=now.tzinfo),
    }
    additional: list[AdditionalAvailability] = []
    time_off: list[TimeRange] = []
    for row in await tx.fetch(
        """
        SELECT type, clinic_location_id, starts_at, ends_at, slot_duration_minutes FROM provider.schedule_exceptions
        WHERE organization_id = :organization AND provider_id = :provider AND starts_at < :end AND ends_at > :start
        """,
        window,
    ):
        interval = TimeRange(row.starts_at, row.ends_at)
        if row.type == ScheduleExceptionType.TIME_OFF:
            time_off.append(interval)
        elif row.clinic_location_id is not None and zones.get(row.clinic_location_id):
            minutes = row.slot_duration_minutes if row.slot_duration_minutes is not None else default_minutes
            additional.append(AdditionalAvailability(row.clinic_location_id, interval, minutes))
    busy = [
        TimeRange(row.starts_at, row.ends_at)
        for row in await tx.fetch(
            """
            SELECT starts_at, ends_at FROM scheduling.appointments
            WHERE organization_id = :organization AND provider_id = :provider
              AND (CAST(:ignore AS uuid) IS NULL OR id <> :ignore)
              AND status NOT IN ('cancelled', 'no_show') AND starts_at < :end AND ends_at > :start
            """,
            {**window, "ignore": ignore_appointment},
        )
    ]
    earliest = (
        now + timedelta(minutes=settings.minimum_booking_notice_minutes) if patient_rules else now - timedelta(hours=8)
    )
    calendars = [
        LocationCalendar(
            location,
            load_zone(zone),
            frozenset(
                day_number(row.date)
                for row in holidays
                if row.clinic_location_id is None or row.clinic_location_id == location
            ),
        )
        for location, zone in zones.items()
    ]
    generated = generate_slots(
        SlotQuery(
            date_from=date_from,
            date_to=date_to,
            earliest_start=earliest,
            latest_start=now + timedelta(days=settings.booking_horizon_days),
            locations=calendars,
            weekly=weekly,
            additional=additional,
            time_off=time_off,
            busy=busy,
        )
    )
    return [
        Slot.model_construct(
            location_id=slot.location_id,
            starts_at=slot.starts_at,
            ends_at=slot.ends_at,
            time_zone=zones[slot.location_id],
        )
        for slot in generated
        if slot.ends_at > now
    ]


class AvailabilityService:
    """Implements the availability query."""

    def __init__(self, db: Database, clock: Clock) -> None:
        """Wire the service to its ports."""
        self._db = db
        self._clock = clock

    async def get_availability(
        self, caller: Caller, provider_id: UUID, location_id: UUID | None, date_from: date, date_to: date
    ) -> AvailabilityResponse:
        """Return open slots for the provider between two local dates (inclusive, at most 31 days)."""
        caller.require(Permission.AVAILABILITY_READ)
        if date_to < date_from:
            raise field_error("to", "The end date must not be before the start date.")
        if day_number(date_to) - day_number(date_from) >= MAX_RANGE_DAYS:
            raise field_error("to", f"The date range cannot exceed {MAX_RANGE_DAYS} days.")
        async with self._db.transaction(caller.scope()) as tx:
            slots = await open_slots(
                tx, caller, self._clock(), provider_id, location_id, date_from, date_to, not is_staff(caller), None
            )
        return AvailabilityResponse.model_construct(provider_id=provider_id, from_=date_from, to=date_to, slots=slots)
