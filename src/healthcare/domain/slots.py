"""Slot generation: turns working hours, exceptions, holidays and bookings into open appointment slots."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from healthcare.domain.calendar import day_number, local_to_utc, weekday_sunday_zero


@dataclass(frozen=True, slots=True)
class TimeRange:
    """A half-open UTC interval [start, end)."""

    start: datetime
    end: datetime

    def overlaps(self, other: TimeRange) -> bool:
        """Return whether the two intervals share any instant."""
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True, slots=True)
class WeeklyAvailability:
    """A recurring weekly working block in the location's local time."""

    location_id: UUID
    day_of_week: int
    start: time
    end: time
    slot_minutes: int
    effective_from: date
    effective_to: date | None


@dataclass(frozen=True, slots=True)
class AdditionalAvailability:
    """A one-off block of extra working hours at a location, in UTC."""

    location_id: UUID
    range: TimeRange
    slot_minutes: int


@dataclass(frozen=True, slots=True)
class LocationCalendar:
    """The time zone and the closure dates (as day numbers) of a location."""

    location_id: UUID
    zone: ZoneInfo
    holidays: frozenset[int] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class AvailableSlot:
    """A bookable appointment slot."""

    location_id: UUID
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True, slots=True)
class SlotQuery:
    """Everything needed to compute a provider's open slots for a range of local dates."""

    date_from: date
    date_to: date
    earliest_start: datetime
    latest_start: datetime
    locations: Sequence[LocationCalendar]
    weekly: Sequence[WeeklyAvailability]
    additional: Sequence[AdditionalAvailability] = ()
    time_off: Sequence[TimeRange] = ()
    busy: Sequence[TimeRange] = ()


def generate_slots(query: SlotQuery) -> list[AvailableSlot]:
    """Return the open slots ordered by start time.

    Weekly blocks are skipped on holidays while additional hours are not; slots overlapping time off,
    existing appointments or an earlier slot, or starting outside the booking window, are removed.
    """
    candidates: list[AvailableSlot] = []
    for location in query.locations:
        _add_weekly_slots(query, location, candidates)
    for extra in query.additional:
        _add_range_slots(extra, candidates)
    candidates.sort(key=lambda slot: (slot.starts_at, slot.location_id.bytes))

    open_slots: list[AvailableSlot] = []
    for slot in candidates:
        slot_range = TimeRange(slot.starts_at, slot.ends_at)
        inside_window = query.earliest_start <= slot.starts_at <= query.latest_start
        overlaps_previous = bool(open_slots) and open_slots[-1].ends_at > slot.starts_at
        if (
            inside_window
            and not overlaps_previous
            and not _overlaps_any(slot_range, query.time_off)
            and not _overlaps_any(slot_range, query.busy)
        ):
            open_slots.append(slot)
    return open_slots


def _add_weekly_slots(query: SlotQuery, location: LocationCalendar, candidates: list[AvailableSlot]) -> None:
    """Expand the weekly blocks of one location into candidate slots for every date in range."""
    day = query.date_from
    while day <= query.date_to:
        if day_number(day) not in location.holidays:
            weekday = weekday_sunday_zero(day)
            for block in query.weekly:
                applies = (
                    block.location_id == location.location_id
                    and block.day_of_week == weekday
                    and block.effective_from <= day
                    and (block.effective_to is None or day <= block.effective_to)
                )
                if applies:
                    _add_block_slots(block, day, location, candidates)
        day += timedelta(days=1)


def _add_block_slots(
    block: WeeklyAvailability, day: date, location: LocationCalendar, candidates: list[AvailableSlot]
) -> None:
    """Split one weekly block on one date into slots, converting local wall-clock times to UTC."""
    if block.slot_minutes <= 0:
        return
    start_minute = block.start.hour * 60 + block.start.minute
    end_minute = block.end.hour * 60 + block.end.minute
    length = timedelta(minutes=block.slot_minutes)
    minute = start_minute
    while minute + block.slot_minutes <= end_minute:
        # Local times skipped by a daylight-saving transition do not exist and cannot be booked.
        start = local_to_utc(day, minute // 60, minute % 60, location.zone)
        if start is not None:
            candidates.append(AvailableSlot(location.location_id, start, start + length))
        minute += block.slot_minutes


def _add_range_slots(extra: AdditionalAvailability, candidates: list[AvailableSlot]) -> None:
    """Split a block of additional hours into consecutive slots."""
    if extra.slot_minutes <= 0:
        return
    length = timedelta(minutes=extra.slot_minutes)
    start = extra.range.start
    while start + length <= extra.range.end:
        candidates.append(AvailableSlot(extra.location_id, start, start + length))
        start += length


def _overlaps_any(candidate: TimeRange, intervals: Sequence[TimeRange]) -> bool:
    """Return whether the range overlaps any of the intervals."""
    return any(candidate.overlaps(interval) for interval in intervals)
