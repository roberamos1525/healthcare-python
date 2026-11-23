"""Read access to an organization's audit trail."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from healthcare.application.core.audit import Args, where
from healthcare.application.core.caller import Caller
from healthcare.application.core.models import ApiModel, UtcDateTime
from healthcare.application.core.paging import Paged, PageRequest, paged
from healthcare.application.core.ports import Database
from healthcare.application.core.validation import is_blank
from healthcare.domain.authorization import Permission
from healthcare.domain.enums import AuditOutcome


class AuditEvent(ApiModel):
    """API representation of an audit event."""

    id: UUID
    occurred_at: UtcDateTime
    actor_user_id: UUID | None = None
    action: str
    resource_type: str
    resource_id: UUID | None = None
    outcome: AuditOutcome
    ip_address: str | None = None
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class AuditFilter:
    """The audit trail filters."""

    action: str | None = None
    resource_type: str | None = None
    resource_id: UUID | None = None
    actor_user_id: UUID | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None


class AuditQueryService:
    """Implements the audit trail query."""

    def __init__(self, db: Database) -> None:
        """Wire the service to the database."""
        self._db = db

    async def list_events(self, caller: Caller, filters: AuditFilter, page: PageRequest) -> Paged[AuditEvent]:
        """List audit events of the selected organization, most recent first."""
        caller.require(Permission.AUDIT_READ)
        organization_id = caller.require_organization_id()
        args = Args()
        conditions = [f"organization_id = {args.add(organization_id)}"]
        if not is_blank(filters.action):
            conditions.append(f"action = {args.add(filters.action)}")
        if not is_blank(filters.resource_type):
            conditions.append(f"resource_type = {args.add(filters.resource_type)}")
        if filters.resource_id is not None:
            conditions.append(f"resource_id = {args.add(filters.resource_id)}")
        if filters.actor_user_id is not None:
            conditions.append(f"actor_user_id = {args.add(filters.actor_user_id)}")
        if filters.occurred_from is not None:
            conditions.append(f"occurred_at >= {args.add(filters.occurred_from)}")
        if filters.occurred_to is not None:
            conditions.append(f"occurred_at < {args.add(filters.occurred_to)}")
        clause = where(conditions)
        async with self._db.transaction(caller.scope()) as tx:
            total = await tx.fetchval(f"SELECT count(*) FROM platform.audit_events{clause}", args.values)
            limit, offset = args.add(page.safe_page_size), args.add(page.offset)
            rows = await tx.fetch(
                "SELECT id, occurred_at, actor_user_id, action, resource_type, resource_id, outcome, ip_address, "
                f"request_id FROM platform.audit_events{clause} ORDER BY occurred_at DESC, id "
                f"LIMIT {limit} OFFSET {offset}",
                args.values,
            )
        items = [
            AuditEvent.model_construct(
                id=row.id,
                occurred_at=row.occurred_at,
                actor_user_id=row.actor_user_id,
                action=row.action,
                resource_type=row.resource_type,
                resource_id=row.resource_id,
                outcome=AuditOutcome(row.outcome),
                ip_address=row.ip_address,
                request_id=row.request_id,
            )
            for row in rows
        ]
        return paged(items, page, total)
