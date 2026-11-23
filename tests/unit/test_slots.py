"""Slot generation: time zones, holidays, exceptions, bookings, the booking window and daylight-saving changes."""

import itertools
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from healthcare.domain.calendar import MIN_INSTANT, day_number
from healthcare.domain.slots import (
    AdditionalAvailability,
    AvailableSlot,
    LocationCalendar,
    SlotQuery,
    TimeRange,
    WeeklyAvailability,
    generate_slots,
)

LOCATION = UUID("0195c0de-0000-7000-8000-0000000000a1")
NEW_YORK = ZoneInfo("America/New_York")
CHICAGO = ZoneInfo("America/Chicago")
MONDAY = date(2027, 1, 11)
SUNDAY, MON, TUESDAY, SATURDAY = 0, 1, 2, 6
LATEST = datetime(9999, 12, 31, tzinfo=UTC)


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Create a UTC instant."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def block(day: int, start: str, end: str, minutes: int = 30, location: UUID = LOCATION) -> WeeklyAvailability:
    """Create a weekly block effective from 1 January 2027 with no end date."""
    return WeeklyAvailability(
        location, day, time.fromisoformat(start), time.fromisoformat(end), minutes, date(2027, 1, 1), None
    )


def query(
    date_from: date,
    date_to: date,
    weekly: list[WeeklyAvailability],
    zone: ZoneInfo = NEW_YORK,
    holidays: frozenset[int] = frozenset(),
) -> SlotQuery:
    """Build a query for one location with permissive booking limits."""
    return SlotQuery(date_from, date_to, MIN_INSTANT, LATEST, [LocationCalendar(LOCATION, zone, holidays)], weekly)


def starts(slots: list[AvailableSlot]) -> list[datetime]:
    """Return the start instants of the slots."""
    return [slot.starts_at for slot in slots]


def assert_no_overlap(slots: list[AvailableSlot]) -> None:
    """Fail when a slot starts before the previous one ends."""
    for previous, current in itertools.pairwise(slots):
        assert current.starts_at >= previous.ends_at


def test_generates_slots_for_the_matching_weekday_in_the_location_time_zone() -> None:
    """09:00-12:00 Eastern Standard Time on a Monday gives six 30-minute slots from 14:00 UTC."""
    slots = generate_slots(query(MONDAY, MONDAY, [block(MON, "09:00", "12:00")]))
    assert len(slots) == 6
    assert slots[0].starts_at == utc(2027, 1, 11, 14)
    assert slots[0].ends_at == utc(2027, 1, 11, 14, 30)
    assert slots[-1].starts_at == utc(2027, 1, 11, 16, 30)
    assert all(slot.location_id == LOCATION for slot in slots)
    assert all(slot.starts_at.utcoffset() == timedelta(0) for slot in slots)


def test_the_same_local_hours_map_to_a_different_instant_in_another_zone() -> None:
    """09:00 Central Standard Time is 15:00 UTC."""
    slots = generate_slots(query(MONDAY, MONDAY, [block(MON, "09:00", "10:00")], zone=CHICAGO))
    assert starts(slots) == [utc(2027, 1, 11, 15), utc(2027, 1, 11, 15, 30)]


def test_ignores_blocks_for_other_weekdays_and_outside_their_effective_dates() -> None:
    """A Tuesday block and an expired Monday block produce nothing on a Monday."""
    expired = WeeklyAvailability(LOCATION, MON, time(9), time(10), 30, date(2026, 1, 1), date(2026, 12, 31))
    future = WeeklyAvailability(LOCATION, MON, time(9), time(10), 30, date(2027, 1, 12), None)
    assert generate_slots(query(MONDAY, MONDAY, [block(TUESDAY, "09:00", "12:00"), expired, future])) == []


def test_effective_dates_are_inclusive() -> None:
    """A block starting and ending on the queried day applies."""
    single_day = WeeklyAvailability(LOCATION, MON, time(9), time(10), 30, MONDAY, MONDAY)
    assert len(generate_slots(query(MONDAY, MONDAY, [single_day]))) == 2


def test_ignores_blocks_of_other_locations() -> None:
    """A block at a location outside the query yields no slot."""
    assert generate_slots(query(MONDAY, MONDAY, [block(MON, "09:00", "12:00", location=uuid4())])) == []


def test_does_not_create_a_partial_slot_at_the_end_of_a_block() -> None:
    """Only whole slots are offered."""
    assert len(generate_slots(query(MONDAY, MONDAY, [block(MON, "09:00", "10:15", 30)]))) == 2


def test_a_non_positive_slot_length_produces_no_slots() -> None:
    """A zero-minute configuration cannot loop forever."""
    q = query(MONDAY, MONDAY, [block(MON, "09:00", "10:00", 0)])
    q = replace(
        q, additional=[AdditionalAvailability(LOCATION, TimeRange(utc(2027, 1, 11, 18), utc(2027, 1, 11, 19)), 0)]
    )
    assert generate_slots(q) == []


def test_skips_weekly_blocks_on_holidays_but_keeps_additional_hours() -> None:
    """A holiday closes the weekly hours while extra hours on that day stay bookable."""
    q = query(MONDAY, MONDAY, [block(MON, "09:00", "12:00")], holidays=frozenset({day_number(MONDAY)}))
    q = replace(
        q, additional=[AdditionalAvailability(LOCATION, TimeRange(utc(2027, 1, 11, 18), utc(2027, 1, 11, 19)), 30)]
    )
    slots = generate_slots(q)
    assert starts(slots) == [utc(2027, 1, 11, 18), utc(2027, 1, 11, 18, 30)]


def test_a_holiday_on_another_day_does_not_close_the_queried_day() -> None:
    """Holidays apply to their own date only."""
    q = query(MONDAY, MONDAY, [block(MON, "09:00", "10:00")], holidays=frozenset({day_number(MONDAY) + 1}))
    assert len(generate_slots(q)) == 2


def test_removes_slots_that_overlap_time_off_or_existing_appointments() -> None:
    """Time off and busy intervals remove the slots they touch."""
    q = replace(
        query(MONDAY, MONDAY, [block(MON, "09:00", "12:00")]),
        time_off=[TimeRange(utc(2027, 1, 11, 14), utc(2027, 1, 11, 15))],
        busy=[TimeRange(utc(2027, 1, 11, 15, 30), utc(2027, 1, 11, 16))],
    )
    assert starts(generate_slots(q)) == [utc(2027, 1, 11, 15), utc(2027, 1, 11, 16), utc(2027, 1, 11, 16, 30)]


def test_intervals_are_half_open_and_partial_overlaps_remove_a_slot() -> None:
    """Touching intervals do not overlap; a busy interval inside a slot removes it."""
    q = replace(
        query(MONDAY, MONDAY, [block(MON, "09:00", "11:00")]),
        busy=[TimeRange(utc(2027, 1, 11, 14, 40), utc(2027, 1, 11, 14, 50))],
        time_off=[TimeRange(utc(2027, 1, 11, 15, 30), utc(2027, 1, 11, 17))],
    )
    assert starts(generate_slots(q)) == [utc(2027, 1, 11, 14), utc(2027, 1, 11, 15)]


def test_time_off_covering_the_whole_day_removes_every_slot() -> None:
    """A full day of time off leaves nothing bookable, including additional hours."""
    q = replace(
        query(MONDAY, MONDAY, [block(MON, "09:00", "17:00")]),
        additional=[AdditionalAvailability(LOCATION, TimeRange(utc(2027, 1, 11, 23), utc(2027, 1, 12, 0)), 30)],
        time_off=[TimeRange(utc(2027, 1, 11, 0), utc(2027, 1, 12, 5))],
    )
    assert generate_slots(q) == []


def test_applies_the_earliest_and_latest_start_limits() -> None:
    """The booking window is inclusive at both ends."""
    q = replace(
        query(MONDAY, MONDAY, [block(MON, "09:00", "12:00")]),
        earliest_start=utc(2027, 1, 11, 15),
        latest_start=utc(2027, 1, 11, 16),
    )
    assert starts(generate_slots(q)) == [utc(2027, 1, 11, 15), utc(2027, 1, 11, 15, 30), utc(2027, 1, 11, 16)]


def test_the_minimum_notice_and_the_booking_horizon_limit_the_offered_days() -> None:
    """With two hours of notice and a two-day horizon only the reachable slots of a week remain."""
    now = utc(2027, 1, 11, 14, 10)
    blocks = [block(day, "09:00", "11:00", 60) for day in range(7)]
    q = replace(
        query(MONDAY, MONDAY + timedelta(days=6), blocks),
        earliest_start=now + timedelta(minutes=120),
        latest_start=now + timedelta(days=2),
    )
    assert starts(generate_slots(q)) == [utc(2027, 1, 12, 14), utc(2027, 1, 12, 15), utc(2027, 1, 13, 14)]


def test_skips_local_times_that_do_not_exist_when_clocks_spring_forward() -> None:
    """In New York, 02:00-03:00 does not exist on 14 March 2027."""
    sunday = date(2027, 3, 14)
    slots = generate_slots(query(sunday, sunday, [block(SUNDAY, "01:00", "04:00")]))
    assert starts(slots) == [utc(2027, 3, 14, 6), utc(2027, 3, 14, 6, 30), utc(2027, 3, 14, 7), utc(2027, 3, 14, 7, 30)]


def test_produces_no_overlapping_slots_when_clocks_fall_back() -> None:
    """In New York, 01:00-02:00 occurs twice on 7 November 2027; the repeated hour is used once."""
    sunday = date(2027, 11, 7)
    slots = generate_slots(query(sunday, sunday, [block(SUNDAY, "00:00", "03:00", 60)]))
    assert starts(slots) == [utc(2027, 11, 7, 4), utc(2027, 11, 7, 6), utc(2027, 11, 7, 7)]
    assert_no_overlap(slots)


def test_chicago_spring_forward_gap_is_skipped() -> None:
    """In Chicago, 02:00-03:00 does not exist on 14 March 2027; 01:30 CST is followed by 03:00 CDT."""
    sunday = date(2027, 3, 14)
    slots = generate_slots(query(sunday, sunday, [block(SUNDAY, "01:00", "04:00")], zone=CHICAGO))
    assert starts(slots) == [utc(2027, 3, 14, 7), utc(2027, 3, 14, 7, 30), utc(2027, 3, 14, 8), utc(2027, 3, 14, 8, 30)]
    assert all(slot.ends_at - slot.starts_at == timedelta(minutes=30) for slot in slots)
    local_starts = [slot.starts_at.astimezone(CHICAGO).strftime("%H:%M") for slot in slots]
    assert local_starts == ["01:00", "01:30", "03:00", "03:30"]


def test_chicago_fall_back_repeated_hour_is_used_once() -> None:
    """In Chicago, 01:00-02:00 occurs twice on 7 November 2027; only the standard-time hour is offered."""
    sunday = date(2027, 11, 7)
    slots = generate_slots(query(sunday, sunday, [block(SUNDAY, "00:00", "03:00", 30)], zone=CHICAGO))
    assert starts(slots) == [
        utc(2027, 11, 7, 5),
        utc(2027, 11, 7, 5, 30),
        utc(2027, 11, 7, 7),
        utc(2027, 11, 7, 7, 30),
        utc(2027, 11, 7, 8),
        utc(2027, 11, 7, 8, 30),
    ]
    assert_no_overlap(slots)
    local_starts = [slot.starts_at.astimezone(CHICAGO).strftime("%H:%M") for slot in slots]
    assert len(local_starts) == len(set(local_starts))


def test_chicago_hourly_slots_across_the_fall_back_do_not_overlap() -> None:
    """Hourly slots across the repeated hour skip the daylight-time occurrence."""
    sunday = date(2027, 11, 7)
    slots = generate_slots(query(sunday, sunday, [block(SUNDAY, "00:00", "03:00", 60)], zone=CHICAGO))
    assert starts(slots) == [utc(2027, 11, 7, 5), utc(2027, 11, 7, 7), utc(2027, 11, 7, 8)]
    assert_no_overlap(slots)


def test_drops_candidates_that_overlap_an_earlier_slot() -> None:
    """Additional hours overlapping weekly slots do not create overlapping slots."""
    q = replace(
        query(MONDAY, MONDAY, [block(MON, "09:00", "10:00")]),
        additional=[
            AdditionalAvailability(LOCATION, TimeRange(utc(2027, 1, 11, 14, 15), utc(2027, 1, 11, 15, 15)), 30)
        ],
    )
    assert starts(generate_slots(q)) == [utc(2027, 1, 11, 14), utc(2027, 1, 11, 14, 30)]


def test_covers_every_day_in_a_multi_day_range() -> None:
    """Each date of the range is expanded."""
    blocks = [block(day, "09:00", "10:00") for day in range(SUNDAY, SATURDAY + 1)]
    slots = generate_slots(query(MONDAY, MONDAY + timedelta(days=6), blocks))
    assert len(slots) == 14
    assert len({slot.starts_at.date() for slot in slots}) == 7


def test_slots_of_several_locations_are_merged_in_start_order() -> None:
    """Two locations in different zones produce one ordered, non-overlapping list."""
    second = UUID("0195c0de-0000-7000-8000-0000000000a2")
    q = SlotQuery(
        MONDAY,
        MONDAY,
        MIN_INSTANT,
        LATEST,
        [LocationCalendar(LOCATION, NEW_YORK), LocationCalendar(second, CHICAGO)],
        [block(MON, "09:00", "10:00", 60), block(MON, "09:00", "10:00", 60, location=second)],
    )
    slots = generate_slots(q)
    assert [(slot.location_id, slot.starts_at) for slot in slots] == [
        (LOCATION, utc(2027, 1, 11, 14)),
        (second, utc(2027, 1, 11, 15)),
    ]


def test_additional_hours_are_split_into_consecutive_slots() -> None:
    """Additional hours become back-to-back slots without a partial slot at the end."""
    q = replace(
        query(MONDAY, MONDAY, []),
        additional=[AdditionalAvailability(LOCATION, TimeRange(utc(2027, 1, 11, 18), utc(2027, 1, 11, 19, 50)), 45)],
    )
    slots = generate_slots(q)
    assert starts(slots) == [utc(2027, 1, 11, 18), utc(2027, 1, 11, 18, 45)]
    assert slots[-1].ends_at == utc(2027, 1, 11, 19, 30)


def test_a_reversed_date_range_produces_no_slots() -> None:
    """When the start date is after the end date nothing is generated."""
    assert generate_slots(query(MONDAY, MONDAY - timedelta(days=1), [block(MON, "09:00", "10:00")])) == []
