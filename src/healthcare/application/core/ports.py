"""Ports implemented by the infrastructure layer and used by the use cases."""

import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from healthcare.application.core.caller import Scope


class Row(Protocol):
    """A result row; columns are read by position or by name (``row.name``, ``row[0]``)."""

    def __getitem__(self, index: int) -> Any:
        """Return the column at the position."""
        ...

    def __getattr__(self, name: str) -> Any:
        """Return the column with the name."""
        ...


class Tx(Protocol):
    """A database transaction bound to a tenant scope; SQL uses ``:name`` parameters."""

    async def fetch(self, sql: str, params: Mapping[str, Any] | None = None) -> Sequence[Row]:
        """Run a query and return every row."""
        ...

    async def fetchrow(self, sql: str, params: Mapping[str, Any] | None = None) -> Row | None:
        """Run a query and return its first row, or None."""
        ...

    async def fetchval(self, sql: str, params: Mapping[str, Any] | None = None) -> Any:
        """Run a query and return the first column of its first row, or None."""
        ...

    async def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> int:
        """Run a statement and return the number of affected rows."""
        ...

    async def set_scope(self, scope: Scope) -> None:
        """Replace the row-level security settings for the rest of the transaction."""
        ...


class Database(Protocol):
    """The only way to reach PostgreSQL: a transaction that starts by applying the caller's scope."""

    def transaction(self, scope: Scope) -> AbstractAsyncContextManager[Tx]:
        """Open a transaction with the scope applied; it commits on success and rolls back on error."""
        ...


@dataclass(frozen=True, slots=True)
class PasswordVerification:
    """The result of checking a password against a stored hash."""

    succeeded: bool
    needs_rehash: bool = False


class PasswordHasher(Protocol):
    """Slow, salted password hashing; implementations keep the work off the event loop."""

    async def hash(self, password: str) -> str:
        """Return an encoded hash embedding the algorithm, cost and salt."""
        ...

    async def verify(self, encoded: str, password: str) -> PasswordVerification:
        """Check a password and report whether the hash should be upgraded."""
        ...

    async def simulate(self, password: str) -> None:
        """Spend the same time as a real verification (unknown accounts must not be detectable)."""
        ...


@dataclass(frozen=True, slots=True)
class AccessToken:
    """A signed access token and its lifetime in seconds."""

    value: str
    expires_in: int


class TokenIssuer(Protocol):
    """Issues short-lived signed access tokens."""

    def issue(self, user_id: UUID, platform_role: str | None, session_id: UUID) -> AccessToken:
        """Create a token bound to the user and the session."""
        ...


class FieldEncryptor(Protocol):
    """Encrypts small secrets (such as MFA seeds) before they are stored."""

    def encrypt(self, plaintext: str) -> str:
        """Return the versioned ciphertext."""
        ...

    def decrypt(self, ciphertext: str) -> str:
        """Return the plaintext or raise when the value was tampered with."""
        ...


class Cache(Protocol):
    """A small JSON cache shared by all instances; failures count as misses."""

    async def get(self, key: str) -> str | None:
        """Return the cached JSON, or None on a miss or when the cache is unavailable."""
        ...

    async def set(self, key: str, value: str, ttl_seconds: float) -> None:
        """Store JSON for the given time to live."""
        ...

    async def remove(self, key: str) -> None:
        """Delete an entry."""
        ...

    async def probe(self) -> bool:
        """Write and remove a probe entry and report whether the cache works."""
        ...


class Mailer(Protocol):
    """Queues account emails; messages never contain health information."""

    def queue_email_verification(self, email: str, first_name: str, token: str) -> None:
        """Queue the email verification link."""
        ...

    def queue_password_reset(self, email: str, first_name: str, token: str) -> None:
        """Queue the password reset link."""
        ...

    def queue_account_already_exists(self, email: str, first_name: str) -> None:
        """Queue the notice sent when someone registers with an existing address."""
        ...

    def queue_staff_invitation(self, email: str, organization_name: str, token: str) -> None:
        """Queue a staff invitation."""
        ...


class ProviderLock(Protocol):
    """Serializes booking attempts for one provider inside the current transaction."""

    async def acquire(self, tx: Tx, provider_id: UUID) -> None:
        """Take the provider's transaction-scoped lock or raise ``schedule_busy`` after five seconds."""
        ...


@dataclass(frozen=True, slots=True)
class IdempotencyClaim:
    """The outcome of claiming an idempotency key inside the booking transaction."""

    is_new: bool
    stored_request_hash: str | None = None
    stored_response: str | None = None


class IdempotencyStore(Protocol):
    """Claims idempotency keys and stores the response of the first request."""

    async def claim(
        self,
        tx: Tx,
        organization_id: UUID,
        user_id: UUID,
        operation: str,
        key: str,
        request_hash: str,
        now: datetime,
        retain_seconds: float,
    ) -> IdempotencyClaim:
        """Insert the key or return what an earlier request with the key stored."""
        ...

    async def complete(
        self, tx: Tx, organization_id: UUID, user_id: UUID, operation: str, key: str, response: str, resource_id: UUID
    ) -> None:
        """Record the response of the request that claimed the key."""
        ...


Clock = Callable[[], datetime]
"""Returns the current UTC time; injected so that time-dependent rules are testable."""


def system_clock() -> datetime:
    """Return the current UTC time (microsecond precision, as PostgreSQL stores it)."""
    return datetime.now(UTC)


def new_id() -> UUID:
    """Return a new time-ordered identifier (UUID version 7)."""
    return uuid.uuid7()
