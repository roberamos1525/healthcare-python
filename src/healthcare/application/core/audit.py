"""Audit trail and transactional outbox writers, plus a helper for SQL assembled from optional filters."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from healthcare.application.core.caller import Caller
from healthcare.application.core.jsonref import reference_json
from healthcare.application.core.models import to_data
from healthcare.application.core.ports import Tx, new_id
from healthcare.domain.enums import AuditOutcome


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One security-relevant action; metadata holds identifiers and field names only, never patient data."""

    action: str
    resource_type: str
    resource_id: UUID | None = None
    outcome: AuditOutcome = AuditOutcome.SUCCESS
    metadata: Any = None
    actor_user_id: UUID | None = None


def _truncate(value: str | None, limit: int) -> str | None:
    """Shorten an optional value to a column limit."""
    if value is None or len(value) <= limit:
        return value
    return value[:limit]


def truncate(value: str | None, limit: int) -> str | None:
    """Shorten an optional value to a column limit (shared with session records)."""
    return _truncate(value, limit)


async def audit(tx: Tx, caller: Caller, now: datetime, entry: AuditEntry) -> None:
    """Append an audit event in the current transaction, under the caller's selected organization."""
    metadata = None if entry.metadata is None else reference_json(to_data(entry.metadata))
    await tx.execute(
        """
        INSERT INTO platform.audit_events
            (id, occurred_at, organization_id, actor_user_id, action, resource_type, resource_id, outcome,
             ip_address, user_agent, request_id, metadata)
        VALUES (:id, :now, :organization, :actor, :action, :resource_type, :resource_id, :outcome,
                :ip, :agent, :request_id, CAST(:metadata AS jsonb))
        """,
        {
            "id": new_id(),
            "now": now,
            "organization": caller.organization_id,
            "actor": entry.actor_user_id or caller.user_id,
            "action": entry.action,
            "resource_type": entry.resource_type,
            "resource_id": entry.resource_id,
            "outcome": entry.outcome.value,
            "ip": caller.ip_address,
            "agent": _truncate(caller.user_agent, 300),
            "request_id": caller.request_id,
            "metadata": metadata,
        },
    )


async def outbox(
    tx: Tx,
    organization_id: UUID | None,
    now: datetime,
    event_type: str,
    aggregate_type: str,
    aggregate_id: UUID,
    payload: Any,
) -> None:
    """Add an integration event to the transactional outbox; payloads carry identifiers, times and statuses."""
    await tx.execute(
        """
        INSERT INTO platform.outbox_events
            (id, organization_id, event_type, aggregate_type, aggregate_id, payload, occurred_at, available_at, attempts)
        VALUES (:id, :organization, :event_type, :aggregate_type, :aggregate_id, CAST(:payload AS jsonb), :now, :now, 0)
        """,
        {
            "id": new_id(),
            "organization": organization_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "payload": reference_json(to_data(payload)),
            "now": now,
        },
    )


class Args:
    """Collects named parameters for SQL assembled from optional filters (values are never inlined)."""

    __slots__ = ("values",)

    def __init__(self) -> None:
        """Create an empty parameter set."""
        self.values: dict[str, Any] = {}

    def add(self, value: Any) -> str:
        """Add a parameter and return its placeholder (``:p1``, ``:p2``, ...)."""
        name = f"p{len(self.values) + 1}"
        self.values[name] = value
        return ":" + name


def where(conditions: list[str]) -> str:
    """Join conditions with AND into a WHERE clause (empty when there are none)."""
    return " WHERE " + " AND ".join(conditions) if conditions else ""
