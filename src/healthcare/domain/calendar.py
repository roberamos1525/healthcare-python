"""Calendar helpers with the reference platform's semantics: dates, times of day and time zones."""

import re
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Final
from zoneinfo import ZoneInfo, available_timezones

MIN_DATE: Final = date(1, 1, 1)
MIN_INSTANT: Final = datetime(1, 1, 1, tzinfo=UTC)

_DATE_PATTERN: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TIME_PATTERN: Final = re.compile(r"([0-9]{2}):([0-9]{2})(?::([0-9]{2}))?")


def parse_date(text: str) -> date | None:
    """Parse ``YYYY-MM-DD``; return None for any other form or an impossible date."""
    if _DATE_PATTERN.fullmatch(text) is None:
        return None
    try:
        return date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        return None


def parse_time_of_day(text: str) -> time | None:
    """Parse a 24-hour ``HH:mm`` or ``HH:mm:ss`` time; return None for any other form."""
    match = _TIME_PATTERN.fullmatch(text)
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    second = int(match.group(3)) if match.group(3) else 0
    if hour > 23 or minute > 59 or second > 59:
        return None
    return time(hour, minute, second)


def format_time_of_day(value: time) -> str:
    """Format a time of day as ``HH:mm``."""
    return f"{value.hour:02d}:{value.minute:02d}"


def day_number(value: date) -> int:
    """Return the number of days since 0001-01-01 (the reference's DayNumber)."""
    return value.toordinal() - 1


def weekday_sunday_zero(value: date) -> int:
    """Return the day of the week with Sunday = 0 and Saturday = 6."""
    return value.isoweekday() % 7


def add_years(value: date, years: int) -> date:
    """Add whole years, clamping 29 February to the 28th in non-leap years as the reference does."""
    year = value.year + years
    try:
        return value.replace(year=year)
    except ValueError:
        return value.replace(year=year, day=28)


def utc_date(instant: datetime) -> date:
    """Return the UTC calendar date of an instant."""
    return instant.astimezone(UTC).date()


def date_in_zone(instant: datetime, zone: ZoneInfo) -> date:
    """Return the calendar date of an instant in a time zone."""
    return instant.astimezone(zone).date()


@lru_cache(maxsize=1)
def _known_zones() -> frozenset[str]:
    """Return the IANA identifiers available from the time zone database."""
    return frozenset(available_timezones())


def is_time_zone(identifier: str) -> bool:
    """Return whether the value is a known IANA identifier such as ``America/New_York`` (or ``UTC``)."""
    if not identifier.strip() or (identifier != "UTC" and "/" not in identifier):
        return False
    return identifier in _known_zones()


@lru_cache(maxsize=512)
def load_zone(identifier: str) -> ZoneInfo:
    """Return the zone of a validated identifier, falling back to UTC."""
    if identifier in _known_zones():
        return ZoneInfo(identifier)
    return ZoneInfo("UTC")


def local_to_utc(day: date, hour: int, minute: int, zone: ZoneInfo) -> datetime | None:
    """Convert a local wall-clock time to UTC.

    Returns None for a time skipped when clocks spring forward. A time that occurs twice when clocks fall
    back is read as standard time, the later of the two instants, which is what the reference does.
    """
    best: datetime | None = None
    for fold in (0, 1):
        local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=fold)
        instant = local.astimezone(UTC)
        back = instant.astimezone(zone)
        same_wall_time = (back.year, back.month, back.day, back.hour, back.minute) == (
            day.year,
            day.month,
            day.day,
            hour,
            minute,
        )
        if same_wall_time and (best is None or instant > best):
            best = instant
    return best


def minutes(value: int) -> timedelta:
    """Return a duration of whole minutes."""
    return timedelta(minutes=value)
