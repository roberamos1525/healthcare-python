"""Request body binding in the reference platform's three stages.

1. Unreadable body: wrong media type (415), oversized (413), empty, malformed JSON or a value of the wrong
   type. The failure is reported at the JSON path of the offending value together with the reference's
   ``request`` entry, and nothing else is checked.
2. Missing non-nullable properties: required properties that are absent or null are all reported with
   ``The {Property} field is required.`` under the model's (PascalCase-for-nested) path; the request's own
   rules do not run.
3. The request's own rules, with the reference's FluentValidation messages.

FastAPI's request validation and Pydantic's lax coercion ("1" becomes 1, 1.0 becomes 1, null becomes a
default) accept exactly what the reference rejects, so request models are filled by this binder instead.
"""

import json
import re
import types
import typing
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum, StrEnum
from functools import cache
from typing import Any, Final, Union
from uuid import UUID

from healthcare.application.core.errors import AppError, FieldErrors, validation
from healthcare.application.core.jsonref import parse_instant
from healthcare.application.core.models import RequestModel
from healthcare.domain.calendar import MIN_DATE, MIN_INSTANT, parse_date, parse_time_of_day
from healthcare.domain.enums import first_member, match_enum

MAX_BODY_BYTES: Final = 1 << 20
INVALID_VALUE: Final = "The value is missing or not valid."
TIME_MESSAGE: Final = "Times must use the HH:mm format."
EMPTY_BODY: Final = "A non-empty request body is required."

_UUID_PATTERN: Final = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
_NIL: Final = UUID(int=0)
_INT32: Final = (-(2**31), 2**31 - 1)


class UnsupportedMediaTypeError(Exception):
    """The body is not JSON; answered with the framework's 415 document."""


class BodyTooLargeError(Exception):
    """The body exceeds 1 MiB; answered with 413."""


class JsonObject(list[tuple[str, Any]]):
    """A parsed JSON object that keeps every member, in document order (duplicates included)."""


class FloatLiteral(str):
    """The text of a JSON number that is not an integer literal (fraction or exponent)."""

    __slots__ = ()


class _BindFailureError(Exception):
    """A stage-1 failure at a JSON path."""

    def __init__(self, path: str, message: str = INVALID_VALUE) -> None:
        """Record where binding failed and why."""
        super().__init__(message)
        self.path = path
        self.message = message


def _reject_constant(name: str) -> Any:
    """Reject NaN and Infinity, which are not JSON."""
    raise ValueError(name)


def _simple_fold(text: str) -> str:
    """Upper-case character by character with simple mappings only, like .NET's OrdinalIgnoreCase."""
    out = []
    for ch in text:
        upper = ch.upper()
        out.append(upper if len(upper) == 1 else ch)
    return "".join(out)


def _lower_first(text: str) -> str:
    """Lower-case the first character, as the reference does for model state keys."""
    return text[:1].lower() + text[1:]


def _upper_first(text: str) -> str:
    """Upper-case the first character to obtain the reference model's property name."""
    return text[:1].upper() + text[1:]


def _join(path: str, segment: str) -> str:
    """Append a property name or an index to a JSON path."""
    if not path or segment.startswith("["):
        return path + segment
    return f"{path}.{segment}"


# ---------------------------------------------------------------------------------------------------------
# JSON parsing. The standard library parses valid documents; a malformed one is re-read by a small parser
# that reports the path of the value being read when the error occurred.
# ---------------------------------------------------------------------------------------------------------

_WHITESPACE: Final = " \t\n\r"
_NUMBER: Final = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_STRING: Final = re.compile(r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"')


class _PathFinder:
    """Re-reads a malformed document to find the path at which it fails."""

    def __init__(self, text: str) -> None:
        """Start at the beginning of the text."""
        self.text = text
        self.pos = 0

    def _skip(self) -> None:
        """Skip insignificant whitespace."""
        while self.pos < len(self.text) and self.text[self.pos] in _WHITESPACE:
            self.pos += 1

    def _peek(self) -> str:
        """Return the next significant character ('' at the end)."""
        self._skip()
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def failure_path(self) -> str:
        """Return the path of the first error (the document root for trailing content)."""
        try:
            self._value("")
        except _BindFailureError as failure:
            return failure.path
        return ""

    def _value(self, path: str) -> None:
        """Read one value at the path or fail there."""
        ch = self._peek()
        if ch == "{":
            self._object(path)
        elif ch == "[":
            self._array(path)
        elif ch == '"':
            self._string(path)
        else:
            end = next(
                (
                    self.pos + len(literal)
                    for literal in ("true", "false", "null")
                    if self.text.startswith(literal, self.pos)
                ),
                None,
            )
            if end is None:
                match = _NUMBER.match(self.text, self.pos)
                if match is None or match.end() == self.pos:
                    raise _BindFailureError(path)
                end = match.end()
            # A scalar must be followed by a delimiter; anything else fails while the value is being read.
            if end < len(self.text) and self.text[end] not in _WHITESPACE + ",]}":
                raise _BindFailureError(path)
            self.pos = end

    def _string(self, path: str) -> None:
        """Read a string literal or fail at the path."""
        match = _STRING.match(self.text, self.pos)
        if match is None:
            raise _BindFailureError(path)
        self.pos = match.end()

    def _object(self, path: str) -> None:
        """Read an object; errors between members are reported at the object's path."""
        self.pos += 1
        if self._peek() == "}":
            self.pos += 1
            return
        while True:
            if self._peek() != '"':
                raise _BindFailureError(path)
            start = self.pos
            self._string(path)
            name = json.loads(self.text[start : self.pos])
            if self._peek() != ":":
                raise _BindFailureError(path)
            self.pos += 1
            self._value(_join(path, name))
            ch = self._peek()
            self.pos += 1
            if ch == "}":
                return
            if ch != ",":
                raise _BindFailureError(path)

    def _array(self, path: str) -> None:
        """Read an array; an error before an element is reported at that element's index."""
        self.pos += 1
        if self._peek() == "]":
            self.pos += 1
            return
        index = 0
        while True:
            self._value(_join(path, f"[{index}]"))
            index += 1
            ch = self._peek()
            self.pos += 1
            if ch == "]":
                return
            if ch != ",":
                raise _BindFailureError(_join(path, f"[{index}]"))


def parse_json(text: str) -> Any:
    """Parse a document keeping member order, duplicates and number literals; raise at the failing path."""
    try:
        return json.loads(text, object_pairs_hook=JsonObject, parse_float=FloatLiteral, parse_constant=_reject_constant)
    except ValueError, RecursionError:
        raise _BindFailureError(_PathFinder(text).failure_path()) from None


# ---------------------------------------------------------------------------------------------------------
# Stage 1: conversion of JSON values into the declared field types.
# ---------------------------------------------------------------------------------------------------------


class Kind(Enum):
    """The conversion applied to a field."""

    STR = "str"
    INT = "int"
    BOOL = "bool"
    UUID = "uuid"
    DATE = "date"
    DATETIME = "datetime"
    TIME = "time"
    ENUM = "enum"
    LIST = "list"
    MODEL = "model"


@dataclass(frozen=True, slots=True)
class TypeSpec:
    """How to convert a JSON value into a Python value of a declared type."""

    kind: Kind
    optional: bool
    enum_type: type[StrEnum] | None = None
    item: TypeSpec | None = None
    model: type[RequestModel] | None = None


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One bindable field of a request model."""

    name: str
    alias: str
    folded: str
    spec: TypeSpec
    required_name: str | None


_SCALARS: Final[dict[type, Kind]] = {
    str: Kind.STR,
    int: Kind.INT,
    bool: Kind.BOOL,
    UUID: Kind.UUID,
    date: Kind.DATE,
    datetime: Kind.DATETIME,
    time: Kind.TIME,
}


def _type_spec(annotation: Any) -> TypeSpec:
    """Describe the conversion of a field annotation."""
    optional = False
    origin = typing.get_origin(annotation)
    if origin in (Union, types.UnionType):
        arguments = [argument for argument in typing.get_args(annotation) if argument is not type(None)]
        optional = len(arguments) < len(typing.get_args(annotation))
        annotation = arguments[0]
        origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        annotation = typing.get_args(annotation)[0]
        origin = typing.get_origin(annotation)
    if origin is list:
        return TypeSpec(Kind.LIST, optional, item=_type_spec(typing.get_args(annotation)[0]))
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return TypeSpec(Kind.ENUM, optional, enum_type=annotation)
    if isinstance(annotation, type) and issubclass(annotation, RequestModel):
        return TypeSpec(Kind.MODEL, optional, model=annotation)
    if annotation in _SCALARS:
        return TypeSpec(_SCALARS[annotation], optional)
    raise TypeError(f"Unsupported request field type: {annotation!r}")


@cache
def fields_of(model: type[RequestModel]) -> tuple[FieldSpec, ...]:
    """Return the bindable fields of a request model in declaration order.

    The reference model's non-nullable reference types (text, lists and nested objects that are not
    optional) are the required properties of stage 2.
    """
    specs = []
    for name, info in model.model_fields.items():
        alias = info.alias or name
        spec = _type_spec(info.annotation)
        required = spec.kind in (Kind.STR, Kind.LIST, Kind.MODEL) and not spec.optional
        specs.append(FieldSpec(name, alias, _simple_fold(alias), spec, _upper_first(alias) if required else None))
    return tuple(specs)


def _find(document: JsonObject, field: FieldSpec) -> tuple[str, Any] | None:
    """Return the last member whose name matches the field case-insensitively (the last duplicate wins)."""
    for name, value in reversed(document):
        if name == field.alias or _simple_fold(name) == field.folded:
            return name, value
    return None


def _default(spec: TypeSpec) -> Any:
    """Return the value of an absent property; enumerations take their first member like the reference."""
    if spec.optional:
        return None
    match spec.kind:
        case Kind.STR:
            return ""
        case Kind.INT:
            return 0
        case Kind.BOOL:
            return False
        case Kind.UUID:
            return _NIL
        case Kind.DATE:
            return MIN_DATE
        case Kind.DATETIME:
            return MIN_INSTANT
        case Kind.TIME:
            return time(0, 0)
        case Kind.ENUM:
            assert spec.enum_type is not None
            return first_member(spec.enum_type)
        case _:
            return None


def _convert(value: Any, spec: TypeSpec, path: str) -> Any:
    """Convert one JSON value into the declared type or fail at its path."""
    if spec.kind is Kind.TIME:
        # The reference's time converter receives strings and null itself and reports its own message;
        # other token types fail inside the serializer with the generic message.
        if value is None or isinstance(value, str):
            parsed_time = parse_time_of_day(value) if isinstance(value, str) else None
            if parsed_time is None:
                raise _BindFailureError(path, TIME_MESSAGE)
            return parsed_time
        raise _BindFailureError(path)
    if value is None:
        if spec.optional or spec.kind is Kind.LIST:
            return None
        if spec.kind is Kind.STR:
            return ""
        raise _BindFailureError(path)
    match spec.kind:
        case Kind.STR:
            if isinstance(value, str) and not isinstance(value, FloatLiteral):
                return str(value)
        case Kind.INT:
            if type(value) is int and _INT32[0] <= value <= _INT32[1]:
                return value
        case Kind.BOOL:
            if type(value) is bool:
                return value
        case Kind.UUID:
            if type(value) is str and _UUID_PATTERN.fullmatch(value):
                return UUID(value)
        case Kind.DATE:
            if type(value) is str:
                parsed_date = parse_date(value)
                if parsed_date is not None:
                    return parsed_date
        case Kind.DATETIME:
            if type(value) is str and len(value) >= 10 and value == value.strip():
                instant = parse_instant(value)
                if instant is not None:
                    return instant
        case Kind.ENUM:
            if type(value) is str:
                assert spec.enum_type is not None
                member = match_enum(spec.enum_type, value)
                if member is not None:
                    return member
        case Kind.LIST:
            if type(value) is list:
                assert spec.item is not None
                items = []
                for index, item in enumerate(value):
                    if item is None and spec.item.kind is Kind.STR:
                        items.append("")
                        continue
                    items.append(_convert(item, spec.item, _join(path, f"[{index}]")))
                return items
        case Kind.MODEL:
            if isinstance(value, JsonObject):
                assert spec.model is not None
                return _bind_object(value, spec.model, path)
    raise _BindFailureError(path)


def _bind_object(document: JsonObject, model: type[RequestModel], path: str) -> RequestModel:
    """Bind an object onto a request model (stage 1)."""
    values: dict[str, Any] = {}
    for field in fields_of(model):
        found = _find(document, field)
        if found is None:
            values[field.name] = _default(field.spec)
        else:
            values[field.name] = _convert(found[1], field.spec, _join(path, found[0]))
    return model.model_construct(**values)


# ---------------------------------------------------------------------------------------------------------
# Stage 2: required properties that are absent or null.
# ---------------------------------------------------------------------------------------------------------


def _missing_required(document: JsonObject, model: type[RequestModel], prefix: str, errors: FieldErrors) -> None:
    """Report absent or null required properties, also inside list elements, keyed like the reference."""
    for field in fields_of(model):
        found = _find(document, field)
        if field.required_name is not None and (found is None or found[1] is None):
            errors.add(_lower_first(prefix + field.required_name), f"The {field.required_name} field is required.")
            continue
        if found is None:
            continue
        spec = field.spec
        if spec.kind is Kind.LIST and spec.item is not None and spec.item.kind is Kind.MODEL and type(found[1]) is list:
            assert spec.item.model is not None
            for index, item in enumerate(found[1]):
                if isinstance(item, JsonObject):
                    _missing_required(item, spec.item.model, f"{prefix}{_upper_first(field.alias)}[{index}].", errors)


def unreadable(path: str, message: str = INVALID_VALUE) -> AppError:
    """Build the stage-1 problem: the offending field plus the reference's ``request`` entry."""
    errors = FieldErrors()
    errors.add(_lower_first(path) or "body", message)
    errors.add("request", "The request field is required.")
    return validation(errors)


def is_json_media_type(content_type: str | None) -> bool:
    """Return whether the media type is application/json, text/json or application/*+json."""
    if not content_type:
        return False
    media = content_type.split(";", 1)[0].strip().lower()
    return media in ("application/json", "text/json") or (media.startswith("application/") and media.endswith("+json"))


def bind_bytes[M: RequestModel](body: bytes, model: type[M], today: date) -> M:
    """Run the three binding stages on a body that has already been read."""
    if body.startswith(b"\xef\xbb\xbf"):
        body = body[3:]
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise unreadable("") from None
    if not text:
        # Only a body without any bytes is "empty" for the reference; whitespace is unreadable JSON.
        raise unreadable("", EMPTY_BODY)
    try:
        document = parse_json(text)
        if document is None:
            raise unreadable("", EMPTY_BODY)
        if not isinstance(document, JsonObject):
            raise _BindFailureError("")
        bound = _bind_object(document, model, "")
    except _BindFailureError as failure:
        raise unreadable(failure.path, failure.message) from None
    missing = FieldErrors()
    _missing_required(document, model, "", missing)
    if len(missing):
        raise validation(missing)
    bound.validate_rules(today)
    return typing.cast(M, bound)
