"""Validation rules with the reference platform's FluentValidation messages and display names."""

import re
from typing import Final
from uuid import UUID

from healthcare.application.core.errors import FieldErrors, validation
from healthcare.application.core.jsonref import utf16_length
from healthcare.domain.calendar import is_time_zone

# Python's "$" (like .NET's) also matches before a final newline, and "\d" matches any Unicode digit as
# .NET's does, so these expressions accept exactly what the reference's do.
PHONE_PATTERN: Final = re.compile(r"^\+?[0-9 ().-]{7,30}$")
ZIP_PATTERN: Final = re.compile(r"^\d{5}(-\d{4})?$")

PHONE_MESSAGE: Final = "Enter a valid phone number."
ZIP_MESSAGE: Final = "Enter a valid ZIP code."
STATE_MESSAGE: Final = "Enter a two-letter US state code, for example TX."
TIME_ZONE_MESSAGE: Final = "Enter a valid IANA time zone, for example America/Chicago."
DURATION_MESSAGE: Final = "The duration must be between 5 and 480 minutes, in 5-minute steps."

US_STATES: Final = frozenset(
    [
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "DC",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "AS",
        "GU",
        "MP",
        "PR",
        "VI",
    ]
)

_NIL = UUID(int=0)


def is_us_state(code: str) -> bool:
    """Return whether the value is a two-letter US state or territory code."""
    return code in US_STATES


def is_email(text: str) -> bool:
    """Apply the reference's lenient email check: exactly one '@' that is neither first nor last."""
    first = text.find("@")
    return first > 0 and first == text.rfind("@") and first != len(text) - 1


def is_blank(text: str | None) -> bool:
    """Return whether an optional text is missing, empty or whitespace only."""
    return text is None or not text.strip()


def trimmed(text: str | None) -> str | None:
    """Return the text without surrounding whitespace (None stays None)."""
    return None if text is None else text.strip()


def display_name(path: str) -> str:
    """Return the human-readable name of the last segment of a field path ("dateOfBirth" is "Date of birth")."""
    last = path.rsplit(".", 1)[-1].split("[", 1)[0]
    words: list[str] = []
    previous = ""
    for index, ch in enumerate(last):
        if index > 0 and ch.isupper() and (previous.islower() or previous.isdigit()):
            words.append(" ")
        words.append(ch.lower())
        previous = ch
    text = "".join(words)
    return text[:1].upper() + text[1:]


def not_empty_message(field: str) -> str:
    """Return the reference message for an empty value."""
    return f"'{display_name(field)}' must not be empty."


class Validator:
    """Collects the validation failures of one request; every rule runs and messages are de-duplicated."""

    __slots__ = ("errors",)

    def __init__(self) -> None:
        """Create an empty validator."""
        self.errors = FieldErrors()

    def add(self, field: str, message: str) -> None:
        """Record a failure."""
        self.errors.add(field, message)

    def ensure(self, field: str, condition: bool, message: str) -> None:
        """Record the message when the condition does not hold."""
        if not condition:
            self.add(field, message)

    def raise_if_failed(self) -> None:
        """Raise the validation error when any rule failed."""
        if len(self.errors):
            raise validation(self.errors)

    def text(self, field: str, value: str | None) -> TextRule:
        """Start the rules of a text field; None means the property was absent or null."""
        return TextRule(self, field, value)

    def between(self, field: str, value: int, low: int, high: int) -> None:
        """Require an integer within an inclusive range."""
        if value < low or value > high:
            self.add(field, f"'{display_name(field)}' must be between {low} and {high}. You entered {value}.")

    def greater_than(self, field: str, value: int, bound: int) -> None:
        """Require an integer greater than a bound."""
        if value <= bound:
            self.add(field, f"'{display_name(field)}' must be greater than '{bound}'.")

    def not_empty_id(self, field: str, value: UUID) -> None:
        """Require an identifier other than the all-zero value."""
        if value == _NIL:
            self.add(field, not_empty_message(field))

    def duration(self, field: str, value: int | None) -> None:
        """Require an optional appointment length of 5-480 minutes in 5-minute steps."""
        if value is not None and (value < 5 or value > 480 or value % 5 != 0):
            self.add(field, DURATION_MESSAGE)


class TextRule:
    """Chains the rules of one text field."""

    __slots__ = ("field", "v", "value")

    def __init__(self, validator: Validator, field: str, value: str | None) -> None:
        """Bind the rule chain to a field."""
        self.v = validator
        self.field = field
        self.value = value

    def not_empty(self) -> TextRule:
        """Require a value that is not empty or whitespace only."""
        if is_blank(self.value):
            self.v.add(self.field, not_empty_message(self.field))
        return self

    def max_length(self, maximum: int) -> TextRule:
        """Limit the length in UTF-16 units; a missing value passes."""
        if self.value is not None:
            length = utf16_length(self.value)
            if length > maximum:
                self.v.add(
                    self.field,
                    f"The length of '{display_name(self.field)}' must be {maximum} characters or fewer. "
                    f"You entered {length} characters.",
                )
        return self

    def min_length(self, minimum: int, message: str) -> TextRule:
        """Require a minimum length with a custom message; a missing value passes."""
        if self.value is not None and utf16_length(self.value) < minimum:
            self.v.add(self.field, message)
        return self

    def email(self) -> TextRule:
        """Require an email-like value; a missing value passes."""
        if self.value is not None and not is_email(self.value):
            self.v.add(self.field, f"'{display_name(self.field)}' is not a valid email address.")
        return self

    def matches(self, pattern: re.Pattern[str], message: str) -> TextRule:
        """Require the value to match the expression; a missing value passes."""
        if self.value is not None and pattern.search(self.value) is None:
            self.v.add(self.field, message)
        return self

    def must(self, condition: bool, message: str) -> TextRule:
        """Record the message when the condition does not hold."""
        if not condition:
            self.v.add(self.field, message)
        return self

    def phone(self) -> TextRule:
        """Require an optional phone number to look valid."""
        return self.max_length(30).matches(PHONE_PATTERN, PHONE_MESSAGE)

    def optional_email(self) -> TextRule:
        """Require an optional email address of at most 254 characters."""
        return self.max_length(254).email()

    def time_zone(self) -> TextRule:
        """Require a valid IANA time zone."""
        return self.not_empty().must(self.value is not None and is_time_zone(self.value), TIME_ZONE_MESSAGE)

    def us_state(self) -> TextRule:
        """Require a two-letter US state or territory code."""
        return self.not_empty().must(self.value is not None and is_us_state(self.value), STATE_MESSAGE)

    def postal_code(self) -> TextRule:
        """Require a ZIP or ZIP+4 code."""
        return self.not_empty().matches(ZIP_PATTERN, ZIP_MESSAGE)
