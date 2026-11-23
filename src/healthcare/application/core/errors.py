"""The application error model; every expected failure becomes an RFC 9457 problem at the HTTP boundary."""

from enum import IntEnum

from healthcare.domain.rules import DomainError, DomainErrorKind


class ErrorKind(IntEnum):
    """Kind of an application error; the value is the HTTP status."""

    BAD_REQUEST = 400
    UNAUTHORIZED = 401
    FORBIDDEN = 403
    NOT_FOUND = 404
    CONFLICT = 409
    RULE = 422


class FieldErrors:
    """Field paths mapped to messages, keeping the order in which fields first failed."""

    __slots__ = ("_values",)

    def __init__(self) -> None:
        """Create an empty collection."""
        self._values: dict[str, list[str]] = {}

    def add(self, field: str, message: str) -> None:
        """Record a message for a field unless the same message is already recorded."""
        messages = self._values.setdefault(field, [])
        if message not in messages:
            messages.append(message)

    def __len__(self) -> int:
        """Return the number of fields with errors."""
        return len(self._values)

    def as_dict(self) -> dict[str, list[str]]:
        """Return the errors as an ordered dictionary."""
        return {key: list(messages) for key, messages in self._values.items()}


class AppError(Exception):
    """An expected failure translated into an API problem; messages are safe and contain no PHI."""

    def __init__(self, kind: ErrorKind, code: str, message: str, fields: FieldErrors | None = None) -> None:
        """Create the error."""
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.message = message
        self.fields = fields

    @property
    def status(self) -> int:
        """Return the HTTP status of the error."""
        return int(self.kind)


def bad_request(code: str, message: str) -> AppError:
    """Create a 400 error with a specific code."""
    return AppError(ErrorKind.BAD_REQUEST, code, message)


def validation(fields: FieldErrors) -> AppError:
    """Create the 400 ``validation_failed`` error carrying field errors."""
    return AppError(ErrorKind.BAD_REQUEST, "validation_failed", "One or more fields are invalid.", fields)


def field_error(field: str, message: str) -> AppError:
    """Create a validation error for one field."""
    fields = FieldErrors()
    fields.add(field, message)
    return validation(fields)


def unauthorized(code: str, message: str) -> AppError:
    """Create a 401 error."""
    return AppError(ErrorKind.UNAUTHORIZED, code, message)


def authentication_required() -> AppError:
    """Create the default 401 error."""
    return unauthorized("unauthorized", "Authentication is required.")


def forbidden(code: str, message: str) -> AppError:
    """Create a 403 error with a code."""
    return AppError(ErrorKind.FORBIDDEN, code, message)


def permission_denied() -> AppError:
    """Create the default 403 error."""
    return forbidden("forbidden", "You do not have permission to perform this action.")


def not_found(resource: str) -> AppError:
    """Create a 404 error naming the resource."""
    return AppError(ErrorKind.NOT_FOUND, "not_found", f"The requested {resource} was not found.")


def conflict(code: str, message: str) -> AppError:
    """Create a 409 error."""
    return AppError(ErrorKind.CONFLICT, code, message)


def rule(code: str, message: str) -> AppError:
    """Create a 422 error."""
    return AppError(ErrorKind.RULE, code, message)


def concurrency_conflict() -> AppError:
    """Create the error for a stale record version."""
    return conflict("concurrency_conflict", "This record was changed by someone else. Reload it and try again.")


def ensure_version(current: int, expected: int) -> None:
    """Raise a concurrency conflict when the stored version differs from the expected one."""
    if current != expected:
        raise concurrency_conflict()


def require_version(version: int | None) -> int:
    """Return the version of an update request or raise the reference's validation error."""
    if version is None:
        raise field_error("version", "The version of the record being updated is required.")
    return version


def from_domain(error: DomainError) -> AppError:
    """Convert a domain rule violation into an application error."""
    if error.kind == DomainErrorKind.CONFLICT:
        return conflict(error.code, error.message)
    return rule(error.code, error.message)
