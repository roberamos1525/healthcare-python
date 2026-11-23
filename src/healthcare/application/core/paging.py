"""Paging, sorting and safe LIKE patterns for list endpoints."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from healthcare.application.core.errors import field_error
from healthcare.application.core.models import ApiModel

MAX_PAGE_SIZE: Final = 100
LIKE_ESCAPE: Final = "\\"


@dataclass(frozen=True, slots=True)
class PageRequest:
    """The page selected with the ``page`` and ``pageSize`` query parameters."""

    page: int = 1
    page_size: int = 25

    @property
    def safe_page(self) -> int:
        """Return the page number clamped to at least 1."""
        return max(self.page, 1)

    @property
    def safe_page_size(self) -> int:
        """Return the page size clamped to 1..100."""
        return min(max(self.page_size, 1), MAX_PAGE_SIZE)

    @property
    def offset(self) -> int:
        """Return the number of items before the page."""
        return (self.safe_page - 1) * self.safe_page_size


class Paged[T](ApiModel):
    """One page of results with the totals needed for pagination controls."""

    items: list[T]
    page: int
    page_size: int
    total_items: int
    total_pages: int


def paged[T](items: Sequence[T], page: PageRequest, total: int) -> Paged[T]:
    """Build a page from the items and the total count."""
    size = page.safe_page_size
    # Constructed unparametrized: items are already built models, and serialization inspects them directly.
    return Paged.model_construct(
        items=list(items),
        page=page.safe_page,
        page_size=size,
        total_items=total,
        total_pages=(total + size - 1) // size,
    )


@dataclass(frozen=True, slots=True)
class SortField:
    """One parsed sort instruction."""

    name: str
    descending: bool = False


def parse_sort(sort: str | None, allowed: Sequence[str], default: str) -> list[SortField]:
    """Parse a sort expression such as ``-startsAt,lastName``; unknown fields are rejected."""
    expression = sort if sort is not None and sort.strip() else default
    fields: list[SortField] = []
    for raw in expression.split(","):
        part = raw.strip()
        if not part:
            continue
        descending = part.startswith("-")
        name = part.removeprefix("-")
        match = next((candidate for candidate in allowed if candidate.casefold() == name.casefold()), None)
        if match is None:
            raise field_error("sort", f"Sorting by '{name}' is not supported. Allowed fields: {', '.join(allowed)}.")
        fields.append(SortField(match, descending))
    return fields or [SortField(default)]


def _escape_like(text: str) -> str:
    """Lower-case and trim text and escape the LIKE wildcards so it is matched literally."""
    return text.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def like_contains(text: str) -> str:
    """Return a lower-case pattern matching values that contain the text."""
    return "%" + _escape_like(text) + "%"


def like_starts_with(text: str) -> str:
    """Return a lower-case pattern matching values that start with the text."""
    return _escape_like(text) + "%"
