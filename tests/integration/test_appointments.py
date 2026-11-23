"""Appointment scenarios: booking effects, idempotency, slot validation, contention, lifecycle and portal rules."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import asyncpg
import pytest

from tests.integration.support import (
    Practice,
    add_member,
    book,
    book_raw,
    create_patient,
    create_portal_patient,
    create_practice,
    env,
    expect,
    idempotency_key,
    local_date,
    parallel,
    parse_instant,
    problem,
    read,
    slots,
)


def format_instant(instant: datetime) -> str:
    """Format an instant as an RFC 3339 UTC timestamp with a ``Z`` suffix."""
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def with_start(slot: dict[str, Any], starts_at: datetime) -> dict[str, Any]:
    """Return a copy of the slot that starts at another instant."""
    return {**slot, "startsAt": format_instant(starts_at)}


async def count_active_appointments(provider_id: str, starts_at: str) -> int:
    """Count appointments that occupy the provider's time at the given start."""
    return int(
        await env().scalar(
            "SELECT count(*) FROM scheduling.appointments WHERE provider_id = $1 AND starts_at = $2 "
            "AND status NOT IN ('cancelled', 'no_show')",
            uuid.UUID(provider_id),
            parse_instant(starts_at),
        )
    )


def has_slot_at(open_slots: list[dict[str, Any]], starts_at: str) -> bool:
    """Report whether a slot starts at the instant."""
    instant = parse_instant(starts_at)
    return any(parse_instant(slot["startsAt"]) == instant for slot in open_slots)


async def test_staff_bookings_are_confirmed_and_record_history_reminders_and_an_event() -> None:
    """A staff booking is confirmed, recorded in history, schedules reminders and emits a PHI-free event."""
    practice = await create_practice("Rivera")
    slot = (await slots(practice.owner, practice, 7))[4]
    appointment = await book(practice.owner, practice, slot)
    assert appointment["status"] == "confirmed", appointment
    assert appointment["bookingChannel"] == "staff", appointment
    assert parse_instant(appointment["startsAt"]) == parse_instant(slot["startsAt"]), appointment
    assert appointment["patient"]["id"] == practice.patient_id, appointment
    assert appointment["location"]["timeZone"] == "America/Chicago", appointment
    history = read(await practice.owner.get(f"/api/v1/appointments/{appointment['id']}/history"), 200)
    assert len(history) == 1 and history[0]["changeType"] == "booked", history
    reminders = await env().scalar(
        "SELECT count(*) FROM scheduling.appointment_reminders WHERE appointment_id = $1", uuid.UUID(appointment["id"])
    )
    assert reminders == 2, f"expected 2 reminders, got {reminders}"
    payload = str(
        await env().scalar(
            "SELECT payload::text FROM platform.outbox_events "
            "WHERE aggregate_id = $1 AND event_type = 'appointment.booked'",
            uuid.UUID(appointment["id"]),
        )
    )
    lower = payload.lower()
    assert appointment["id"] in payload and "check-up" not in lower and "jordan" not in lower, payload
    remaining = await slots(practice.owner, practice, 7)
    assert not has_slot_at(remaining, slot["startsAt"]), "the booked slot is still offered"


async def test_retrying_with_the_same_idempotency_key_returns_the_original_appointment() -> None:
    """A retry with the same key replays the stored response."""
    practice = await create_practice("Rivera")
    slot = (await slots(practice.owner, practice, 5))[0]
    key = idempotency_key()
    first = await book_raw(practice.owner, practice, slot, key=key)
    retry = await book_raw(practice.owner, practice, slot, key=key)
    original = read(first, 201)
    replayed = read(retry, 201)
    assert original["id"] == replayed["id"], "the replay returns the original appointment"
    assert first.headers.get("Idempotent-Replayed") == "false", first.headers
    assert retry.headers.get("Idempotent-Replayed") == "true", retry.headers
    assert first.body == retry.body, f"the replayed body differs:\n{first.body!r}\n{retry.body!r}"
    count = await count_active_appointments(practice.provider_id, slot["startsAt"])
    assert count == 1, f"expected one appointment, got {count}"


async def test_reusing_an_idempotency_key_for_a_different_request_is_rejected() -> None:
    """The request hash of a stored key must match."""
    practice = await create_practice("Rivera")
    open_slots = await slots(practice.owner, practice, 5)
    key = idempotency_key()
    expect(await book_raw(practice.owner, practice, open_slots[0], key=key), 201)
    problem(await book_raw(practice.owner, practice, open_slots[1], key=key), 422, "idempotency_key_reused")


async def test_booking_requires_a_well_formed_idempotency_key() -> None:
    """Missing or malformed keys are refused."""
    practice = await create_practice("Rivera")
    slot = (await slots(practice.owner, practice, 5))[0]
    for key in ("", "short", "contains spaces and !"):
        response = await practice.owner.send(
            "POST",
            "/api/v1/appointments",
            {
                "patientId": practice.patient_id,
                "providerId": practice.provider_id,
                "locationId": slot["locationId"],
                "startsAt": slot["startsAt"],
                "visitType": "in_person",
            },
            {"Idempotency-Key": key or None},
        )
        problem(response, 400, "idempotency_key_required")


async def test_a_taken_slot_or_a_time_outside_working_hours_returns_a_conflict() -> None:
    """Taken, off-grid and out-of-hours times are unavailable."""
    practice = await create_practice("Rivera")
    slot = (await slots(practice.owner, practice, 6))[2]
    await book(practice.owner, practice, slot)
    second = (await create_patient(practice.owner, "Second"))["id"]
    starts_at = parse_instant(slot["startsAt"]).astimezone(UTC)
    off_grid = with_start(slot, starts_at + timedelta(minutes=10))
    night = with_start(slot, starts_at.replace(hour=5, minute=0, second=0, microsecond=0))
    for candidate in (slot, off_grid, night):
        problem(await book_raw(practice.owner, practice, candidate, second), 409, "slot_unavailable")


async def test_concurrent_requests_for_the_same_slot_produce_exactly_one_appointment() -> None:
    """The booking lock lets exactly one of many concurrent requests win."""
    practice = await create_practice("Rivera")
    slot = (await slots(practice.owner, practice, 8))[3]
    patients = [(await create_patient(practice.owner, "Concurrent"))["id"] for _ in range(5)]
    responses = await parallel(20, lambda i: book_raw(practice.owner, practice, slot, patients[i % len(patients)]))
    created = conflicts = 0
    for response in responses:
        if response.status == 201:
            created += 1
        elif response.status == 409:
            conflicts += 1
        else:
            raise AssertionError(f"unexpected status {response.status}: {response.body!r}")
    assert created == 1 and conflicts == 19, f"expected 1 created and 19 conflicts, got {created} and {conflicts}"
    count = await count_active_appointments(practice.provider_id, slot["startsAt"])
    assert count == 1, f"expected one appointment, got {count}"


async def test_concurrent_retries_with_one_idempotency_key_create_one_appointment() -> None:
    """Concurrent retries with one key create one appointment and one idempotency record."""
    practice = await create_practice("Rivera")
    slot = (await slots(practice.owner, practice, 8))[5]
    key = idempotency_key()
    responses = await parallel(10, lambda _: book_raw(practice.owner, practice, slot, key=key))
    ids = {read(response, 201)["id"] for response in responses}
    assert len(ids) == 1, f"expected one appointment id, got {ids}"
    count = await count_active_appointments(practice.provider_id, slot["startsAt"])
    assert count == 1, f"expected one appointment, got {count}"
    records = await env().scalar("SELECT count(*) FROM platform.idempotency_records WHERE key = $1", key)
    assert records == 1, f"expected one idempotency record, got {records}"


async def test_the_database_rejects_overlapping_appointments_even_when_application_checks_are_bypassed() -> None:
    """The exclusion constraint refuses an overlapping row inserted directly."""
    practice = await create_practice("Rivera")
    slot = (await slots(practice.owner, practice, 9))[0]
    booked = await book(practice.owner, practice, slot)
    connection = await env().system()
    try:
        with pytest.raises(asyncpg.PostgresError) as raised:
            await connection.execute(
                """
                INSERT INTO scheduling.appointments
                    (id, organization_id, patient_id, provider_id, clinic_location_id, starts_at, ends_at, status,
                     visit_type, booking_channel, created_at, updated_at, version)
                VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, 'confirmed', 'in_person', 'staff', now(), now(), 1)
                """,
                uuid.UUID(practice.organization_id),
                uuid.UUID(practice.patient_id),
                uuid.UUID(practice.provider_id),
                uuid.UUID(practice.location_id),
                parse_instant(slot["startsAt"]) + timedelta(minutes=15),
                parse_instant(slot["endsAt"]) + timedelta(minutes=15),
            )
    finally:
        await connection.close()
    error: Any = raised.value
    assert error.sqlstate == "23P01", f"expected an exclusion violation, got {error!r}"
    assert error.constraint_name == "ex_appointments_provider_no_overlap", f"unexpected constraint {error!r}"
    current = read(await practice.owner.get(f"/api/v1/appointments/{booked['id']}"), 200)
    assert current["id"] == booked["id"], "the original appointment is intact"


async def test_an_appointment_moves_through_check_in_to_completion() -> None:
    """The visit lifecycle: check-in, start, completion, then no more changes."""
    practice = await create_practice("Rivera")
    open_slots = await slots(practice.owner, practice, 0)
    if not open_slots:
        open_slots = await slots(practice.owner, practice, 1)
    appointment = await book(practice.owner, practice, open_slots[0])
    path = f"/api/v1/appointments/{appointment['id']}"
    problem(await practice.owner.post(path + "/complete"), 409, "invalid_status_transition")
    assert read(await practice.owner.post(path + "/check-in"), 200)["status"] == "checked_in", "checked in"
    assert read(await practice.owner.post(path + "/start"), 200)["status"] == "in_progress", "started"
    completed = read(await practice.owner.post(path + "/complete"), 200)
    assert completed["status"] == "completed", completed
    assert completed.get("checkedInAt") is not None and completed.get("completedAt") is not None, completed
    problem(await practice.owner.post(path + "/cancel", {"reason": "Too late"}), 409, "invalid_status_transition")
    history = read(await practice.owner.get(path + "/history"), 200)
    statuses = [entry["toStatus"] for entry in history]
    assert statuses == ["confirmed", "checked_in", "in_progress", "completed"], f"unexpected history {statuses}"


async def test_a_future_appointment_cannot_be_marked_as_a_no_show() -> None:
    """Only started appointments can be no-shows."""
    practice = await create_practice("Rivera")
    appointment = await book(practice.owner, practice, (await slots(practice.owner, practice, 4))[0])
    problem(
        await practice.owner.post(f"/api/v1/appointments/{appointment['id']}/no-show"), 422, "appointment_not_started"
    )


async def test_receptionists_cannot_conduct_visits() -> None:
    """Receptionists can check in but not start a visit."""
    practice = await create_practice("Rivera")
    receptionist = await add_member(practice.owner, practice.organization_id, "receptionist")
    appointment = await book(receptionist, practice, (await slots(receptionist, practice, 4))[1])
    expect(await receptionist.post(f"/api/v1/appointments/{appointment['id']}/check-in"), 200)
    problem(await receptionist.post(f"/api/v1/appointments/{appointment['id']}/start"), 403, "forbidden")


async def test_cancelling_frees_the_slot_for_another_booking() -> None:
    """Cancellation releases the slot and drops the scheduled reminders."""
    practice = await create_practice("Rivera")
    slot = (await slots(practice.owner, practice, 10))[6]
    appointment = await book(practice.owner, practice, slot)
    cancelled = read(
        await practice.owner.post(f"/api/v1/appointments/{appointment['id']}/cancel", {"reason": "Patient request"}),
        200,
    )
    assert cancelled["status"] == "cancelled" and cancelled["cancellationReason"] == "Patient request", cancelled
    scheduled = await env().scalar(
        "SELECT count(*) FROM scheduling.appointment_reminders WHERE appointment_id = $1 AND status = 'scheduled'",
        uuid.UUID(appointment["id"]),
    )
    assert scheduled == 0, f"expected no scheduled reminders, got {scheduled}"
    other = (await create_patient(practice.owner, "Next"))["id"]
    rebooked = await book(practice.owner, practice, slot, other)
    assert parse_instant(rebooked["startsAt"]) == parse_instant(slot["startsAt"]), "the slot can be booked again"


async def test_rescheduling_moves_the_appointment_and_releases_the_old_time() -> None:
    """Rescheduling refuses a taken time, moves the appointment and records the previous start."""
    practice = await create_practice("Rivera")
    open_slots = await slots(practice.owner, practice, 11)
    appointment = await book(practice.owner, practice, open_slots[0])
    blocker_patient = await create_patient(practice.owner, "Blocker")
    blocker = await book(practice.owner, practice, open_slots[2], blocker_patient["id"])
    path = f"/api/v1/appointments/{appointment['id']}"
    problem(await practice.owner.post(path + "/reschedule", {"startsAt": blocker["startsAt"]}), 409, "slot_unavailable")
    moved = read(await practice.owner.post(path + "/reschedule", {"startsAt": open_slots[5]["startsAt"]}), 200)
    assert parse_instant(moved["startsAt"]) == parse_instant(open_slots[5]["startsAt"]), moved
    assert moved["version"] == appointment["version"] + 1, moved
    after = await slots(practice.owner, practice, 11)
    assert has_slot_at(after, open_slots[0]["startsAt"]), "the old time is released"
    assert not has_slot_at(after, open_slots[5]["startsAt"]), "the new time is taken"
    history = read(await practice.owner.get(path + "/history"), 200)
    changes = [entry for entry in history if entry["changeType"] == "rescheduled"]
    assert len(changes) == 1 and changes[0].get("previousStartsAt") is not None, f"unexpected history {changes}"
    assert parse_instant(changes[0]["previousStartsAt"]) == parse_instant(open_slots[0]["startsAt"]), changes


async def test_appointment_details_use_optimistic_concurrency() -> None:
    """Updates carry the version and stale versions are refused."""
    practice = await create_practice("Rivera")
    appointment = await book(practice.owner, practice, (await slots(practice.owner, practice, 12))[0])
    path = f"/api/v1/appointments/{appointment['id']}"
    updated = read(
        await practice.owner.put(
            path,
            {
                "visitType": "telehealth",
                "reason": "Follow-up",
                "version": appointment["version"],
            },
        ),
        200,
    )
    assert updated["visitType"] == "telehealth", updated
    problem(
        await practice.owner.put(
            path,
            {
                "visitType": "in_person",
                "reason": "Stale",
                "version": appointment["version"],
            },
        ),
        409,
        "concurrency_conflict",
    )


async def test_patients_book_for_themselves_and_see_only_their_own_appointments() -> None:
    """Portal bookings are pending, limited to the patient's own record and confirmed by staff."""
    practice = await create_practice("Rivera")
    portal, patient = await create_portal_patient(practice)
    await book(practice.owner, practice, (await slots(practice.owner, practice, 6))[0])
    own = await book(portal, practice, (await slots(portal, practice, 6))[3], patient["id"])
    problem(
        await book_raw(portal, practice, (await slots(portal, practice, 6))[5], practice.patient_id), 403, "forbidden"
    )
    assert own["status"] == "pending" and own["bookingChannel"] == "patient_portal", own
    visible = read(await portal.get("/api/v1/appointments"), 200)
    assert len(visible["items"]) == 1 and visible["items"][0]["id"] == own["id"], visible["items"]
    problem(await portal.post(f"/api/v1/appointments/{own['id']}/confirm"), 403, "forbidden")
    confirmed = read(await practice.owner.post(f"/api/v1/appointments/{own['id']}/confirm"), 200)
    assert confirmed["status"] == "confirmed", "staff confirm the booking"


async def update_settings(practice: Practice, notice: int, cancellation_hours: int, auto_confirm: bool) -> None:
    """Replace the booking policy of the practice."""
    expect(
        await practice.owner.put(
            f"/api/v1/organizations/{practice.organization_id}/settings",
            {
                "defaultAppointmentDurationMinutes": 30,
                "minimumBookingNoticeMinutes": notice,
                "bookingHorizonDays": 90,
                "patientCancellationWindowHours": cancellation_hours,
                "reminderOffsetsMinutes": [1440],
                "allowPatientSelfScheduling": True,
                "autoConfirmPatientBookings": auto_confirm,
            },
        ),
        200,
    )


async def test_patients_cannot_cancel_online_inside_the_cancellation_window() -> None:
    """The cancellation window applies to patients only."""
    practice = await create_practice("Rivera")
    await update_settings(practice, 60, 168, True)
    portal, patient = await create_portal_patient(practice)
    appointment = await book(portal, practice, (await slots(portal, practice, 3))[0], patient["id"])
    assert appointment["status"] == "confirmed", f"auto-confirmed booking expected, got {appointment['status']}"
    path = f"/api/v1/appointments/{appointment['id']}"
    problem(await portal.post(path + "/cancel", {"reason": "Conflict"}), 422, "cancellation_window_passed")
    problem(
        await portal.post(path + "/reschedule", {"startsAt": (await slots(portal, practice, 3))[2]["startsAt"]}),
        422,
        "cancellation_window_passed",
    )
    cancelled = read(await practice.owner.post(path + "/cancel", {"reason": "Staff override"}), 200)
    assert cancelled["status"] == "cancelled", "staff can cancel"


async def test_patients_must_respect_the_minimum_booking_notice() -> None:
    """The minimum booking notice applies to patients only."""
    practice = await create_practice("Rivera")
    await update_settings(practice, 10_080, 24, False)
    portal, patient = await create_portal_patient(practice)
    staff_slot = (await slots(practice.owner, practice, 2))[0]
    assert len(await slots(portal, practice, 2)) == 0, "no slots inside the notice period for patients"
    problem(await book_raw(portal, practice, staff_slot, patient["id"]), 409, "slot_unavailable")
    await book(practice.owner, practice, staff_slot, patient["id"])


async def test_no_slots_are_offered_for_an_inactive_provider() -> None:
    """Deactivating the provider removes availability."""
    practice = await create_practice("Rivera")
    slot = (await slots(practice.owner, practice, 5))[0]
    expect(await practice.owner.post(f"/api/v1/providers/{practice.provider_id}/deactivate"), 200)
    assert len(await slots(practice.owner, practice, 5)) == 0, "no slots for an inactive provider"
    problem(await book_raw(practice.owner, practice, slot), 409, "slot_unavailable")


async def test_waiting_list_entries_are_fulfilled_by_a_booking() -> None:
    """A booking that names a waiting list entry fulfils it."""
    practice = await create_practice("Rivera")
    entry = read(
        await practice.owner.post(
            "/api/v1/waiting-list",
            {
                "patientId": practice.patient_id,
                "providerId": practice.provider_id,
                "earliestDate": local_date(practice.time_zone, 1),
                "latestDate": local_date(practice.time_zone, 15),
                "notes": "Prefers mornings",
            },
        ),
        201,
    )
    slot = (await slots(practice.owner, practice, 2))[0]
    appointment = read(
        await practice.owner.send(
            "POST",
            "/api/v1/appointments",
            {
                "patientId": practice.patient_id,
                "providerId": practice.provider_id,
                "locationId": slot["locationId"],
                "startsAt": slot["startsAt"],
                "visitType": "in_person",
                "waitingListEntryId": entry["id"],
            },
            {"Idempotency-Key": idempotency_key()},
        ),
        201,
    )
    entries = read(await practice.owner.get("/api/v1/waiting-list?status=fulfilled"), 200)
    assert len(entries["items"]) == 1, entries["items"]
    assert entries["items"][0]["fulfilledAppointmentId"] == appointment["id"], entries["items"]


async def test_appointment_lists_support_filters_sorting_and_pagination() -> None:
    """List filters, sorting and paging."""
    practice = await create_practice("Rivera")
    open_slots = await slots(practice.owner, practice, 13)
    for index in range(3):
        await book(practice.owner, practice, open_slots[index * 2])
    start = quote(format_instant(parse_instant(open_slots[0]["startsAt"])), safe="")
    base = (
        f"/api/v1/appointments?providerId={practice.provider_id}&status=confirmed&from={start}"
        "&sort=-startsAt&pageSize=2&page="
    )
    page1 = read(await practice.owner.get(base + "1"), 200)
    page2 = read(await practice.owner.get(base + "2"), 200)
    assert page1["totalItems"] == 3 and page1["totalPages"] == 2 and len(page1["items"]) == 2, page1
    assert parse_instant(page1["items"][0]["startsAt"]) == parse_instant(open_slots[4]["startsAt"]), page1
    assert parse_instant(page1["items"][1]["startsAt"]) == parse_instant(open_slots[2]["startsAt"]), page1
    assert len(page2["items"]) == 1, page2
    assert parse_instant(page2["items"][0]["startsAt"]) == parse_instant(open_slots[0]["startsAt"]), page2
    problem(await practice.owner.get("/api/v1/appointments?status=unknown"), 400, "validation_failed")
