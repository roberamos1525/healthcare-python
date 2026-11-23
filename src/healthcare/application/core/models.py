"""Pydantic base classes and annotated types for the contract's data transfer objects.

Responses are Pydantic models serialized with camelCase aliases and without null members. Request models
are filled by the HTTP layer's binder (``model_construct``), which reproduces the reference platform's
binding rules; Pydantic's own coercion is never applied to client input.
"""

from datetime import date, datetime, time
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, PlainSerializer
from pydantic.alias_generators import to_camel

from healthcare.application.core.jsonref import format_offset, format_utc, reference_json
from healthcare.application.core.validation import Validator
from healthcare.domain.calendar import format_time_of_day

UtcDateTime = Annotated[datetime, PlainSerializer(format_utc, return_type=str, when_used="json")]
"""An instant written in UTC with a ``Z`` suffix."""

OffsetDateTime = Annotated[datetime, PlainSerializer(format_offset, return_type=str, when_used="json")]
"""An instant written with a ``+00:00`` offset, as stored for other backends."""

TimeOfDay = Annotated[time, PlainSerializer(format_time_of_day, return_type=str, when_used="json")]
"""A local time of day written as ``HH:mm``."""


class ApiModel(BaseModel):
    """Base class of every contract model: camelCase members, nulls omitted when serialized."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    def to_data(self) -> Any:
        """Return the JSON data of the model (aliases applied, null members removed)."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)

    def to_json(self) -> str:
        """Return the reference-escaped JSON text of the model."""
        return reference_json(self.to_data())


class RequestModel(ApiModel):
    """Base class of request bodies; ``check`` holds the request's own validation rules (stage 3)."""

    def check(self, v: Validator, today: date) -> None:
        """Record rule violations of the request; the default accepts everything."""

    def validate_rules(self, today: date) -> None:
        """Run the request's rules and raise ``validation_failed`` when any of them fails."""
        validator = Validator()
        self.check(validator, today)
        validator.raise_if_failed()


def to_data(value: Any) -> Any:
    """Convert models (also inside lists and dictionaries) into JSON data."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, list):
        return [to_data(item) for item in value]
    if isinstance(value, dict):
        return {key: to_data(item) for key, item in value.items()}
    return value
