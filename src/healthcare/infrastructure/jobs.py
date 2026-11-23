"""Background jobs inside the API process: outbox dispatch, reminder dispatch and maintenance.

Rows are claimed with ``FOR UPDATE SKIP LOCKED``, so any number of worker processes and instances can run the
jobs at the same time without processing a row twice. Each job runs in a loop that logs failures and
retries, so a worker recovers by itself.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import Protocol

from sqlalchemy import Text, cast, select

from healthcare.application.core.audit import outbox
from healthcare.application.core.caller import SYSTEM_SCOPE
from healthcare.application.core.ports import Clock
from healthcare.infrastructure.config import WorkerSettings
from healthcare.infrastructure.messaging import Event, Publisher
from healthcare.infrastructure.persistence.database import SqlDatabase
from healthcare.infrastructure.persistence.tables import appointment_reminders, appointments, outbox_events

_LOGGER = logging.getLogger("healthcare.jobs")


class Job(Protocol):
    """One unit of recurring work."""

    name: str

    @property
    def interval(self) -> float:
        """Return the delay in seconds between runs when nothing more is waiting."""
        ...

    async def run_once(self) -> bool:
        """Run once and return whether more work is waiting."""
        ...


async def run_job(job: Job) -> None:
    """Host a job in a loop until cancelled; failures are logged and retried after five seconds."""
    while True:
        try:
            more = await job.run_once()
            delay = 0.0 if more else job.interval
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.error(
                "Background job failed; it will be retried",
                extra={
                    "job": job.name,
                    "errorType": type(error).__name__,
                },
                exc_info=error,
            )
            delay = 5.0
        await asyncio.sleep(delay)


@dataclass(slots=True)
class OutboxDispatchJob:
    """Publishes due outbox events and records the outcome, retrying failures with exponential backoff."""

    db: SqlDatabase
    publisher: Publisher
    clock: Clock
    settings: WorkerSettings
    name: str = "OutboxDispatchJob"

    @property
    def interval(self) -> float:
        """Return the outbox poll interval."""
        return float(self.settings.outbox_poll_seconds)

    async def run_once(self) -> bool:
        """Claim a batch of due events, publish them and mark them processed or delay them."""
        now = self.clock()
        table = outbox_events
        claim = (
            select(
                table.c.id,
                table.c.event_type,
                cast(table.c.payload, Text).label("payload"),
                table.c.occurred_at,
                table.c.attempts,
            )
            .where(table.c.processed_at.is_(None), table.c.available_at <= now)
            .order_by(table.c.occurred_at)
            .limit(self.settings.batch_size)
            .with_for_update(skip_locked=True)
        )
        async with self.db.transaction(SYSTEM_SCOPE) as tx:
            events = (await tx.connection.execute(claim)).fetchall()
            for event in events:
                attempts = event.attempts + 1
                try:
                    payload = str(event.payload).encode("utf-8")
                    await self.publisher.publish(Event(event.id, event.event_type, payload, event.occurred_at))
                except Exception as error:
                    # Only the error type is stored, never payload data.
                    delay = min(3600.0, 5.0 * 2 ** min(attempts, 10))
                    _LOGGER.warning("Publishing an event failed", extra={"eventId": str(event.id), "attempt": attempts})
                    await tx.execute(
                        """
                        UPDATE platform.outbox_events SET attempts = :attempts, available_at = :available,
                            last_error = :error
                        WHERE id = :id
                        """,
                        {
                            "id": event.id,
                            "attempts": attempts,
                            "available": now + timedelta(seconds=delay),
                            "error": type(error).__name__,
                        },
                    )
                    continue
                await tx.execute(
                    """
                    UPDATE platform.outbox_events SET attempts = :attempts, processed_at = :now, last_error = NULL
                    WHERE id = :id
                    """,
                    {"id": event.id, "attempts": attempts, "now": now},
                )
        return len(events) == self.settings.batch_size


@dataclass(slots=True)
class ReminderDispatchJob:
    """Turns due reminders into outbox events and cancels reminders of visits that no longer take place."""

    db: SqlDatabase
    clock: Clock
    settings: WorkerSettings
    name: str = "ReminderDispatchJob"

    @property
    def interval(self) -> float:
        """Return the reminder poll interval."""
        return float(self.settings.reminder_poll_seconds)

    async def run_once(self) -> bool:
        """Claim due reminders and queue one PHI-free event per reminder that is still upcoming."""
        now = self.clock()
        r, a = appointment_reminders, appointments
        claim = (
            select(r.c.id, r.c.organization_id, r.c.appointment_id, r.c.channel, a.c.status, a.c.starts_at)
            .join(a, a.c.id == r.c.appointment_id)
            .where(r.c.status == "scheduled", r.c.scheduled_for <= now)
            .order_by(r.c.scheduled_for)
            .limit(self.settings.batch_size)
            .with_for_update(of=r, skip_locked=True)
        )
        async with self.db.transaction(SYSTEM_SCOPE) as tx:
            reminders = (await tx.connection.execute(claim)).fetchall()
            for reminder in reminders:
                upcoming = reminder.starts_at > now and reminder.status in ("pending", "confirmed")
                if not upcoming:
                    await tx.execute(
                        "UPDATE scheduling.appointment_reminders SET status = 'cancelled' WHERE id = :id",
                        {"id": reminder.id},
                    )
                    continue
                await tx.execute(
                    "UPDATE scheduling.appointment_reminders SET status = 'queued', queued_at = :now WHERE id = :id",
                    {"id": reminder.id, "now": now},
                )
                await outbox(
                    tx,
                    reminder.organization_id,
                    now,
                    "appointment.reminder_due",
                    "appointment",
                    reminder.appointment_id,
                    {
                        "reminderId": str(reminder.id),
                        "appointmentId": str(reminder.appointment_id),
                        "channel": reminder.channel,
                    },
                )
        return len(reminders) == self.settings.batch_size


@dataclass(slots=True)
class MaintenanceJob:
    """Expires stale records and deletes data that has passed its retention period."""

    db: SqlDatabase
    clock: Clock
    settings: WorkerSettings
    name: str = "MaintenanceJob"

    @property
    def interval(self) -> float:
        """Return the maintenance interval."""
        return float(self.settings.maintenance_interval_minutes * 60)

    async def run_once(self) -> bool:
        """Run every cleanup step and log how many rows each affected."""
        now = self.clock()
        today = now.astimezone(UTC).date()
        credential_cutoff = now - timedelta(days=self.settings.expired_credential_retention_days)
        outbox_cutoff = now - timedelta(days=self.settings.outbox_retention_days)
        statements: list[tuple[str, str, dict[str, object]]] = [
            (
                "idempotencyRecords",
                "DELETE FROM platform.idempotency_records WHERE expires_at <= :cutoff",
                {"cutoff": now},
            ),
            ("tokens", "DELETE FROM identity.user_tokens WHERE expires_at <= :cutoff", {"cutoff": credential_cutoff}),
            ("sessions", "DELETE FROM identity.sessions WHERE expires_at <= :cutoff", {"cutoff": credential_cutoff}),
            (
                "outboxEvents",
                "DELETE FROM platform.outbox_events WHERE processed_at IS NOT NULL AND processed_at <= :cutoff",
                {"cutoff": outbox_cutoff},
            ),
            (
                "expiredInvitations",
                "UPDATE organization.staff_invitations SET status = 'expired' WHERE status = 'pending' AND expires_at <= :cutoff",
                {"cutoff": now},
            ),
            (
                "expiredWaitingListEntries",
                "UPDATE scheduling.waiting_list_entries SET status = 'expired' WHERE status = 'active' AND latest_date < :today",
                {"today": today},
            ),
        ]
        counts: dict[str, int] = {}
        async with self.db.transaction(SYSTEM_SCOPE) as tx:
            for name, sql, params in statements:
                counts[name] = await tx.execute(sql, params)
        _LOGGER.info("Maintenance completed", extra=counts)
        return False


def build_jobs(db: SqlDatabase, publisher: Publisher, clock: Clock, settings: WorkerSettings) -> list[Job]:
    """Create the three background jobs."""
    return [
        OutboxDispatchJob(db, publisher, clock, settings),
        ReminderDispatchJob(db, clock, settings),
        MaintenanceJob(db, clock, settings),
    ]
