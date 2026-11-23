"""Background job scenarios: outbox dispatch, reminder dispatch and maintenance."""

import uuid

from healthcare.infrastructure.jobs import Job, OutboxDispatchJob
from healthcare.infrastructure.messaging import Event
from tests.integration.support import book, create_practice, env, expect, slots, unique_email


class CapturingPublisher:
    """Records published events instead of sending them to a broker."""

    def __init__(self) -> None:
        """Start with no events."""
        self.events: list[Event] = []

    async def publish(self, event: Event) -> None:
        """Store the event."""
        self.events.append(event)

    async def close(self) -> None:
        """Release nothing."""


def app_job(name: str) -> Job:
    """Return the application's background job with the name."""
    container = env().container
    assert container is not None
    for job in container.jobs:
        if job.name == name:
            return job
    raise AssertionError(f"no job named {name}")


async def run_job(job: Job) -> None:
    """Run a background job until it reports that no more work is waiting."""
    for _ in range(100):
        if not await job.run_once():
            return


async def test_the_outbox_dispatcher_publishes_pending_events_once() -> None:
    """Pending events are published once and marked processed."""
    practice = await create_practice("Rivera")
    appointment = await book(practice.owner, practice, (await slots(practice.owner, practice, 6))[0])
    aggregate = uuid.UUID(appointment["id"])
    pending_sql = (
        "SELECT count(*) FROM platform.outbox_events WHERE aggregate_id = $1 "
        "AND event_type = 'appointment.booked' AND processed_at IS NULL"
    )
    pending = await env().scalar(pending_sql, aggregate)
    assert pending == 1, f"expected one pending event, got {pending}"
    event_id = await env().scalar(
        "SELECT id FROM platform.outbox_events WHERE aggregate_id = $1 AND event_type = 'appointment.booked'", aggregate
    )
    container = env().container
    assert container is not None
    template = app_job("OutboxDispatchJob")
    assert isinstance(template, OutboxDispatchJob)
    publisher = CapturingPublisher()
    await run_job(OutboxDispatchJob(container.database, publisher, template.clock, template.settings))
    pending = await env().scalar(pending_sql, aggregate)
    attempts = await env().scalar(
        "SELECT attempts FROM platform.outbox_events WHERE aggregate_id = $1 AND event_type = 'appointment.booked'",
        aggregate,
    )
    assert pending == 0 and attempts == 1, (
        f"expected the event to be published once, pending {pending} attempts {attempts}"
    )
    published = [event for event in publisher.events if event.id == event_id]
    assert len(published) == 1 and published[0].event_type == "appointment.booked", published


async def test_due_reminders_are_queued_and_reminders_for_cancelled_visits_are_dropped() -> None:
    """Due reminders become events; reminders of cancelled visits are cancelled without events."""
    practice = await create_practice("Rivera")
    open_slots = await slots(practice.owner, practice, 6)
    kept = uuid.UUID((await book(practice.owner, practice, open_slots[0]))["id"])
    cancelled = uuid.UUID((await book(practice.owner, practice, open_slots[2]))["id"])
    await env().execute(
        "UPDATE scheduling.appointment_reminders SET scheduled_for = now() - interval '1 minute' "
        "WHERE appointment_id = ANY($1)",
        [kept, cancelled],
    )
    await env().execute("UPDATE scheduling.appointments SET status = 'cancelled' WHERE id = $1", cancelled)
    await run_job(app_job("ReminderDispatchJob"))
    queued = await env().scalar(
        "SELECT count(*) FROM scheduling.appointment_reminders WHERE appointment_id = $1 AND status = 'queued'", kept
    )
    dropped = await env().scalar(
        "SELECT count(*) FROM scheduling.appointment_reminders WHERE appointment_id = $1 AND status = 'cancelled'",
        cancelled,
    )
    assert queued == 2 and dropped == 2, f"expected 2 queued and 2 cancelled reminders, got {queued} and {dropped}"
    payload = str(
        await env().scalar(
            "SELECT coalesce(string_agg(payload::text, ','), '') FROM platform.outbox_events "
            "WHERE aggregate_id = $1 AND event_type = 'appointment.reminder_due'",
            kept,
        )
    )
    assert "reminderId" in payload and "jordan" not in payload.lower(), f"unexpected reminder payload {payload}"
    dropped_events = await env().scalar(
        "SELECT count(*) FROM platform.outbox_events "
        "WHERE aggregate_id = $1 AND event_type = 'appointment.reminder_due'",
        cancelled,
    )
    assert dropped_events == 0, "no reminder events for a cancelled visit"


async def test_maintenance_removes_expired_idempotency_records_and_expires_stale_invitations() -> None:
    """Maintenance deletes expired idempotency records and expires stale invitations."""
    practice = await create_practice("Rivera")
    appointment = uuid.UUID((await book(practice.owner, practice, (await slots(practice.owner, practice, 6))[0]))["id"])
    expect(
        await practice.owner.post(
            f"/api/v1/organizations/{practice.organization_id}/invitations",
            {"email": unique_email("late"), "roles": ["staff"]},
        ),
        201,
    )
    organization = uuid.UUID(practice.organization_id)
    await env().execute(
        "UPDATE platform.idempotency_records SET expires_at = now() - interval '1 minute' WHERE resource_id = $1",
        appointment,
    )
    await env().execute(
        "UPDATE organization.staff_invitations SET expires_at = now() - interval '1 minute' WHERE organization_id = $1",
        organization,
    )
    await run_job(app_job("MaintenanceJob"))
    records = await env().scalar(
        "SELECT count(*) FROM platform.idempotency_records WHERE resource_id = $1", appointment
    )
    status = await env().scalar(
        "SELECT status FROM organization.staff_invitations WHERE organization_id = $1", organization
    )
    assert records == 0 and status == "expired", (
        f"expected the record removed and the invitation expired, got {records} {status}"
    )
