r"""JSON conventions of the contract, byte-compatible with the reference platform's serializer.

The reference writes compact JSON in which non-ASCII characters and the HTML-sensitive characters
``" & ' + < > ` `` are escaped as upper-case ``\\uXXXX`` sequences. Instants are written in UTC with a ``Z``
suffix in API responses and with a ``+00:00`` offset in values stored for other backends (idempotency
hashes, stored responses, outbox payloads and audit metadata).
"""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Final

_ESCAPES: Final = re.compile(r'\\\\|\\u[0-9a-f]{4}|\\"|[&\'+<>`\x7f]')
_INSTANT: Final = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})"
    r"(?:T([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.([0-9]{1,9}))?)?(Z|[+-][0-9]{2}:[0-9]{2})?)?"
)


def _escape(match: re.Match[str]) -> str:
    """Rewrite one token of the standard library's output with the reference escaping."""
    token = match.group()
    if token == "\\\\":
        return token
    if token == '\\"':
        return "\\u0022"
    if token[0] == "\\":
        return "\\u" + token[2:].upper()
    return f"\\u{ord(token):04X}"


def reference_json(value: Any) -> str:
    """Serialize plain JSON data (dicts, lists, strings, numbers) compactly with the reference escaping."""
    return _ESCAPES.sub(_escape, json.dumps(value, ensure_ascii=True, separators=(",", ":")))


def reference_string(text: str) -> str:
    """Return a JSON string literal escaped with the reference rules."""
    return reference_json(text)


def _fraction(instant: datetime) -> str:
    """Return the fractional seconds with trailing zeros removed (empty when zero)."""
    if instant.microsecond == 0:
        return ""
    return "." + f"{instant.microsecond:06d}".rstrip("0")


def _seconds_text(instant: datetime) -> str:
    """Format date and time to the second in UTC (``YYYY-MM-DDTHH:MM:SS``) with a four-digit year."""
    v = instant.astimezone(UTC) if instant.tzinfo is not None else instant
    return f"{v.year:04d}-{v.month:02d}-{v.day:02d}T{v.hour:02d}:{v.minute:02d}:{v.second:02d}"


def format_utc(instant: datetime) -> str:
    """Write an instant as the API does, for example ``2026-11-22T15:00:00Z``."""
    return _seconds_text(instant) + _fraction(instant) + "Z"


def format_offset(instant: datetime) -> str:
    """Write an instant as the reference's default serializer does, for example ``2026-11-22T15:00:00+00:00``."""
    return _seconds_text(instant) + _fraction(instant) + "+00:00"


def parse_instant(text: str) -> datetime | None:
    """Parse an ISO 8601 timestamp; without an offset it is read as UTC, a date alone means midnight UTC."""
    match = _INSTANT.fullmatch(text.strip())
    if match is None:
        return None
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    try:
        microsecond = int((fraction or "0").ljust(6, "0")[:6])
        result = datetime(
            int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0), microsecond, tzinfo=UTC
        )
        if offset and offset != "Z":
            hours, minutes = int(offset[1:3]), int(offset[4:6])
            if hours > 23 or minutes > 59:
                return None
            delta = timedelta(hours=hours, minutes=minutes)
            result = result - delta if offset[0] == "+" else result + delta
    except ValueError, OverflowError:
        return None
    return result


def utf16_length(text: str) -> int:
    """Return the length of the text in UTF-16 code units, which is how the reference counts characters."""
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)
