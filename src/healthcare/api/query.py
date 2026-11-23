"""Query string binding with the reference's rules.

Names are case-insensitive, the first value wins, blank values count as absent, and every unparsable value
is reported in one validation problem. The reference names the property in the message only for the paging
object's ``Page`` and ``PageSize``.
"""

import re
from datetime import date, datetime
from enum import StrEnum
from typing import Final
from urllib.parse import unquote_plus
from uuid import UUID

from healthcare.application.core.errors import FieldErrors, validation
from healthcare.application.core.jsonref import parse_instant
from healthcare.application.core.paging import PageRequest
from healthcare.domain.calendar import parse_date
from healthcare.domain.enums import match_enum_query

_GUID: Final = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}|[0-9A-Fa-f]{32}"
)
_INTEGER: Final = re.compile(r"[+-]?[0-9]+")


def parse_guid(text: str) -> UUID | None:
    """Parse a GUID in the 36-character or 32-character form, optionally in braces."""
    value = text.strip()
    if value.startswith("{") and value.endswith("}"):
        value = value[1:-1]
    return UUID(value) if _GUID.fullmatch(value) else None


class QueryParams:
    """Reads typed query parameters and collects every parse failure."""

    def __init__(self, raw: str) -> None:
        """Decode the raw query string (decoding never fails at this stage)."""
        self._pairs: list[tuple[str, str]] = []
        for part in raw.split("&"):
            if not part:
                continue
            key, _, value = part.partition("=")
            self._pairs.append((unquote_plus(key).casefold(), unquote_plus(value)))
        self.errors = FieldErrors()

    def raw(self, name: str) -> str | None:
        """Return the first value of a parameter, or None when it is absent or blank."""
        folded = name.casefold()
        for key, value in self._pairs:
            if key == folded:
                return value if value.strip() else None
        return None

    def _invalid(self, name: str, value: str) -> None:
        """Record the reference message for an unparsable value."""
        suffix = f" for {name}" if name in ("Page", "PageSize") else ""
        self.errors.add(name[:1].lower() + name[1:], f"The value '{value}' is not valid{suffix}.")

    def text(self, name: str) -> str | None:
        """Return an optional text parameter."""
        return self.raw(name)

    def guid(self, name: str) -> UUID | None:
        """Return an optional identifier."""
        value = self.raw(name)
        if value is None:
            return None
        parsed = parse_guid(value)
        if parsed is None:
            self._invalid(name, value.strip())
        return parsed

    def integer(self, name: str) -> int | None:
        """Return an optional 32-bit integer."""
        value = self.raw(name)
        if value is None:
            return None
        text = value.strip()
        if _INTEGER.fullmatch(text) and -(2**31) <= int(text) <= 2**31 - 1:
            return int(text)
        self._invalid(name, text)
        return None

    def flag(self, name: str) -> bool:
        """Return a boolean parameter (false when absent)."""
        value = self.raw(name)
        if value is None:
            return False
        text = value.strip().lower()
        if text in ("true", "false"):
            return text == "true"
        self._invalid(name, value.strip())
        return False

    def date(self, name: str) -> date | None:
        """Return an optional ``YYYY-MM-DD`` date."""
        value = self.raw(name)
        if value is None:
            return None
        parsed = parse_date(value.strip())
        if parsed is None:
            self._invalid(name, value.strip())
        return parsed

    def instant(self, name: str) -> datetime | None:
        """Return an optional timestamp with any ISO 8601 offset."""
        value = self.raw(name)
        if value is None:
            return None
        parsed = parse_instant(value)
        if parsed is None:
            self._invalid(name, value.strip())
        return parsed

    def enum[E: StrEnum](self, name: str, enum_type: type[E]) -> E | None:
        """Return an optional snake_case enumeration value, listing the allowed values on failure."""
        value = self.raw(name)
        if value is None:
            return None
        member = match_enum_query(enum_type, value.strip())
        if member is None:
            allowed = ", ".join(item.value for item in enum_type)
            self.errors.add(name[:1].lower() + name[1:], f"Allowed values: {allowed}.")
        return member

    def page(self) -> PageRequest:
        """Return the ``page`` and ``pageSize`` parameters with their defaults."""
        page = self.integer("Page")
        size = self.integer("PageSize")
        return PageRequest(page if page is not None else 1, size if size is not None else 25)

    def finish(self) -> None:
        """Raise one validation problem when any parameter could not be parsed."""
        if len(self.errors):
            raise validation(self.errors)
