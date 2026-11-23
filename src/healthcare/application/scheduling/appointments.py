"""Booking and the appointment life cycle.

A booking runs in one transaction: the idempotency claim, the provider's advisory lock, slot validation with
the same computation the availability endpoint uses, the inserts and the stored response. The exclusion
constraint on the appointments table is the last line of defence against double booking.
"""

import json
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Final
from uuid import UUID

from healthcare.application.core.audit import Args, AuditEntry, audit, outbox, where
from healthcare.application.core.caller import Caller
from healthcare.application.core.errors import (
    conflict,
    ensure_version,
    field_error,
    from_domain,
    not_found,
    permission_denied,
    rule,
)
from healthcare.application.core.jsonref import format_offset, parse_instant
from healthcare.application.core.paging import Paged, PageRequest, paged, parse_sort
from healthcare.application.core.ports import Clock, Database, IdempotencyStore, ProviderLock, Row, Tx, new_id
from healthcare.application.core.validation import trimmed
from healthcare.application.organizations.service import load_settings
from healthcare.application.scheduling.availability import open_slots
from healthcare.application.scheduling.contracts import (
    Appointment,
    AppointmentFilter,
    BookRequest,
    BookResult,
    CancelRequest,
    History,
    LocationRef,
    PatientRef,
    ProviderRef,
    RescheduleRequest,
    Slot,
    StoredAppointment,
    UpdateAppointmentRequest,
    request_hash,
)
from healthcare.domain.authorization import Permission
from healthcare.domain.calendar import date_in_zone, load_zone
from healthcare.domain.enums import ActiveStatus, AppointmentStatus, BookingChannel, VisitType
from healthcare.domain.rules import DomainError, ensure_reschedulable, ensure_transition, is_closed, occupies_slot

BOOK_OPERATION: Final = "appointments.create"

_PROJECTION: Final = """
    SELECT a.id, a.status, a.starts_at, a.ends_at, a.visit_type, a.reason, a.booking_channel,
        pa.id AS patient_id, pa.first_name AS patient_first_name, pa.last_name AS patient_last_name,
        pr.id AS provider_id, pr.first_name AS provider_first_name, pr.last_name AS provider_last_name,
        pr.professional_title, l.id AS location_id, l.name AS location_name, l.time_zone,
        a.checked_in_at, a.started_at, a.completed_at, a.cancelled_at, a.cancellation_reason, a.version,
        a.created_at, a.updated_at
    FROM scheduling.appointments a
    JOIN patient.patients pa ON pa.id = a.patient_id
    JOIN provider.providers pr ON pr.id = a.provider_id
    JOIN organization.clinic_locations l ON l.id = a.clinic_location_id
"""

_STATUS_COLUMNS: Final = {
    AppointmentStatus.CHECKED_IN: "checked_in_at",
    AppointmentStatus.IN_PROGRESS: "started_at",
    AppointmentStatus.COMPLETED: "completed_at",
    AppointmentStatus.CANCELLED: "cancelled_at",
}


class Actor(Enum):
    """Whether the caller acts as staff or as the patient, which decides the rules that apply."""

    STAFF = "staff"
    PATIENT = "patient"


def _appointment_view(row: Row) -> Appointment:
    """Build the API representation of a projected appointment row."""
    return Appointment.model_construct(
        id=row.id,
        status=AppointmentStatus(row.status),
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        visit_type=VisitType(row.visit_type),
        reason=row.reason,
        booking_channel=BookingChannel(row.booking_channel),
        patient=PatientRef.model_construct(
            id=row.patient_id, first_name=row.patient_first_name, last_name=row.patient_last_name
        ),
        provider=ProviderRef.model_construct(
            id=row.provider_id,
            first_name=row.provider_first_name,
            last_name=row.provider_last_name,
            professional_title=row.professional_title,
        ),
        location=LocationRef.model_construct(id=row.location_id, name=row.location_name, time_zone=row.time_zone),
        checked_in_at=row.checked_in_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        cancelled_at=row.cancelled_at,
        cancellation_reason=row.cancellation_reason,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _stored_form(appointment: Appointment) -> str:
    """Serialize an appointment in the stored form shared with the other backends."""
    stored = StoredAppointment.model_construct(
        **{name: getattr(appointment, name) for name in Appointment.model_fields}
    )
    return stored.to_json()


def _required_instant(value: Any) -> datetime:
    """Parse a stored timestamp that must be present."""
    instant = _instant(value)
    if instant is None:
        raise ValueError("The stored idempotent response has an invalid timestamp.")
    return instant


def _instant(value: Any) -> datetime | None:
    """Parse an optional stored timestamp."""
    return parse_instant(value) if isinstance(value, str) else None


def _from_stored(text: str) -> Appointment:
    """Read a stored response written by any backend back into an appointment."""
    data = json.loads(text)
    patient, provider, location = data["patient"], data["provider"], data["location"]
    return Appointment.model_construct(
        id=UUID(data["id"]),
        status=AppointmentStatus(data["status"]),
        starts_at=_required_instant(data["startsAt"]),
        ends_at=_required_instant(data["endsAt"]),
        visit_type=VisitType(data["visitType"]),
        reason=data.get("reason"),
        booking_channel=BookingChannel(data["bookingChannel"]),
        patient=PatientRef.model_construct(
            id=UUID(patient["id"]), first_name=patient["firstName"], last_name=patient["lastName"]
        ),
        provider=ProviderRef.model_construct(
            id=UUID(provider["id"]),
            first_name=provider["firstName"],
            last_name=provider["lastName"],
            professional_title=provider.get("professionalTitle"),
        ),
        location=LocationRef.model_construct(
            id=UUID(location["id"]), name=location["name"], time_zone=location["timeZone"]
        ),
        checked_in_at=_instant(data.get("checkedInAt")),
        started_at=_instant(data.get("startedAt")),
        completed_at=_instant(data.get("completedAt")),
        cancelled_at=_instant(data.get("cancelledAt")),
        cancellation_reason=data.get("cancellationReason"),
        version=int(data["version"]),
        created_at=_required_instant(data["createdAt"]),
        updated_at=_required_instant(data["updatedAt"]),
    )


def visible_condition(caller: Caller, args: Args) -> str:
    """Limit appointments (alias ``a``) to those the caller may read: all, own calendar, or own appointments."""
    organization = f"a.organization_id = {args.add(caller.organization_id)}"
    if caller.has(Permission.APPOINTMENTS_READ):
        return organization
    provider_id = caller.provider_id if caller.has(Permission.APPOINTMENTS_READ_OWN) else None
    own = caller.patient_ids if caller.has(Permission.APPOINTMENTS_SELF) else []
    if provider_id is None and not own:
        caller.require(Permission.APPOINTMENTS_READ)
    parts: list[str] = []
    if provider_id is not None:
        parts.append(f"a.provider_id = {args.add(provider_id)}")
    if own:
        parts.append(f"a.patient_id = ANY({args.add(list(own))})")
    return f"{organization} AND ({' OR '.join(parts)})"


def _resolve_actor(caller: Caller, patient_id: UUID, provider_id: UUID) -> Actor:
    """Decide whether a change is made by staff or by the patient for themselves."""
    if caller.has(Permission.APPOINTMENTS_MANAGE):
        return Actor.STAFF
    if caller.has(Permission.APPOINTMENTS_MANAGE_OWN) and caller.is_own_provider(provider_id):
        return Actor.STAFF
    if caller.has(Permission.APPOINTMENTS_SELF) and caller.is_linked_patient(patient_id):
        return Actor.PATIENT
    raise permission_denied()


def _payload(
    organization_id: UUID,
    appointment_id: UUID,
    provider_id: UUID,
    patient_id: UUID,
    location_id: UUID,
    starts_at: datetime,
    ends_at: datetime,
    status: str,
) -> dict[str, Any]:
    """Build the PHI-free event payload of an appointment (identifiers, times and status only)."""
    return {
        "appointmentId": str(appointment_id),
        "organizationId": str(organization_id),
        "providerId": str(provider_id),
        "patientId": str(patient_id),
        "locationId": str(location_id),
        "startsAt": format_offset(starts_at),
        "endsAt": format_offset(ends_at),
        "status": status,
    }


def _ensure_outside_cancellation_window(starts_at: datetime, actor: Actor, window_hours: int, now: datetime) -> None:
    """Stop patients from changing an appointment online once the cancellation window has started."""
    if actor == Actor.PATIENT and now > starts_at - timedelta(hours=window_hours):
        raise rule(
            "cancellation_window_passed",
            f"Online changes are not available within {window_hours} hours of the appointment. Contact the clinic.",
        )


class AppointmentService:
    """Implements booking, listing and the appointment life cycle."""

    def __init__(
        self,
        db: Database,
        clock: Clock,
        provider_lock: ProviderLock,
        idempotency: IdempotencyStore,
        idempotency_retention: timedelta,
    ) -> None:
        """Wire the service to its ports."""
        self._db = db
        self._clock = clock
        self._lock = provider_lock
        self._idempotency = idempotency
        self._retention = idempotency_retention

    async def list_appointments(
        self, caller: Caller, filters: AppointmentFilter, page: PageRequest
    ) -> Paged[Appointment]:
        """List the appointments visible to the caller, filtered and sorted as requested."""
        args = Args()
        conditions = [visible_condition(caller, args)]
        if filters.provider_id is not None:
            conditions.append(f"a.provider_id = {args.add(filters.provider_id)}")
        if filters.patient_id is not None:
            conditions.append(f"a.patient_id = {args.add(filters.patient_id)}")
        if filters.location_id is not None:
            conditions.append(f"a.clinic_location_id = {args.add(filters.location_id)}")
        if filters.status is not None:
            conditions.append(f"a.status = {args.add(filters.status.value)}")
        if filters.starts_from is not None:
            conditions.append(f"a.starts_at >= {args.add(filters.starts_from)}")
        if filters.starts_to is not None:
            conditions.append(f"a.starts_at < {args.add(filters.starts_to)}")
        order = parse_sort(filters.sort, ["startsAt", "createdAt"], "startsAt")[0]
        if order.name == "createdAt":
            order_by = "a.created_at DESC, a.id" if order.descending else "a.created_at, a.id"
        else:
            order_by = "a.starts_at DESC, a.id" if order.descending else "a.starts_at, a.id"
        clause = where(conditions)
        async with self._db.transaction(caller.scope()) as tx:
            total = await tx.fetchval(f"SELECT count(*) FROM scheduling.appointments a{clause}", args.values)
            limit, offset = args.add(page.safe_page_size), args.add(page.offset)
            rows = await tx.fetch(
                f"{_PROJECTION}{clause} ORDER BY {order_by} LIMIT {limit} OFFSET {offset}", args.values
            )
        return paged([_appointment_view(row) for row in rows], page, total)

    async def _get_visible(self, tx: Tx, caller: Caller, appointment_id: UUID) -> Appointment:
        """Return one appointment the caller may see or raise not found."""
        args = Args()
        condition = visible_condition(caller, args)
        row = await tx.fetchrow(f"{_PROJECTION} WHERE {condition} AND a.id = {args.add(appointment_id)}", args.values)
        if row is None:
            raise not_found("appointment")
        return _appointment_view(row)

    async def get(self, caller: Caller, appointment_id: UUID) -> Appointment:
        """Return one appointment the caller may see."""
        async with self._db.transaction(caller.scope()) as tx:
            return await self._get_visible(tx, caller, appointment_id)

    async def history(self, caller: Caller, appointment_id: UUID) -> list[History]:
        """Return the status and time changes of an appointment, oldest first."""
        async with self._db.transaction(caller.scope()) as tx:
            await self._get_visible(tx, caller, appointment_id)
            rows = await tx.fetch(
                """
                SELECT id, change_type, from_status, to_status, changed_by_user_id, changed_at, reason,
                    previous_starts_at, previous_ends_at
                FROM scheduling.appointment_status_history
                WHERE organization_id = :organization AND appointment_id = :id ORDER BY changed_at, id
                """,
                {"organization": caller.organization_id, "id": appointment_id},
            )
        return [
            History.model_construct(
                id=row.id,
                change_type=row.change_type,
                from_status=AppointmentStatus(row.from_status) if row.from_status is not None else None,
                to_status=AppointmentStatus(row.to_status),
                changed_by_user_id=row.changed_by_user_id,
                changed_at=row.changed_at,
                reason=row.reason,
                previous_starts_at=row.previous_starts_at,
                previous_ends_at=row.previous_ends_at,
            )
            for row in rows
        ]

    async def _find_open_slot(
        self,
        tx: Tx,
        caller: Caller,
        now: datetime,
        provider_id: UUID,
        location_id: UUID,
        starts_at: datetime,
        actor: Actor,
        ignore: UUID | None,
    ) -> Slot:
        """Find the open slot starting at the requested time or raise a booking conflict."""
        zone = await tx.fetchval(
            "SELECT time_zone FROM organization.clinic_locations WHERE id = :id AND organization_id = :organization",
            {"id": location_id, "organization": caller.organization_id},
        )
        if zone is None:
            raise field_error("locationId", "The location does not exist in this organization.")
        local_date = date_in_zone(starts_at, load_zone(zone))
        slots = await open_slots(
            tx, caller, now, provider_id, location_id, local_date, local_date, actor == Actor.PATIENT, ignore
        )
        for slot in slots:
            if slot.starts_at == starts_at:
                return slot
        raise conflict("slot_unavailable", "The selected time is no longer available. Choose another time.")

    @staticmethod
    async def _plan_reminders(
        tx: Tx, organization_id: UUID, appointment_id: UUID, starts_at: datetime, offsets: list[int], now: datetime
    ) -> None:
        """Create reminder records for each configured lead time that is still in the future."""
        unique_offsets: list[int] = list(dict.fromkeys(offsets))
        for offset in unique_offsets:
            scheduled_for = starts_at - timedelta(minutes=offset)
            if scheduled_for <= now:
                continue
            await tx.execute(
                """
                INSERT INTO scheduling.appointment_reminders (id, organization_id, appointment_id, channel,
                    scheduled_for, status, created_at)
                VALUES (:id, :organization, :appointment, 'email', :scheduled, 'scheduled', :now)
                """,
                {
                    "id": new_id(),
                    "organization": organization_id,
                    "appointment": appointment_id,
                    "scheduled": scheduled_for,
                    "now": now,
                },
            )

    @staticmethod
    async def _cancel_pending_reminders(tx: Tx, organization_id: UUID, appointment_id: UUID) -> None:
        """Cancel reminders that have not been sent yet."""
        await tx.execute(
            """
            UPDATE scheduling.appointment_reminders SET status = 'cancelled'
            WHERE organization_id = :organization AND appointment_id = :appointment AND status = 'scheduled'
            """,
            {"organization": organization_id, "appointment": appointment_id},
        )

    async def book(self, caller: Caller, request: BookRequest, idempotency_key: str) -> BookResult:
        """Book an appointment in an open slot; a retry with the same key returns the original appointment."""
        organization_id = caller.require_organization_id()
        user_id = caller.require_user_id()
        actor = _resolve_actor(caller, request.patient_id, request.provider_id)
        now = self._clock()
        starts_at = request.starts_at.astimezone(UTC)
        hashed = request_hash(request)
        replay: Appointment | None = None
        try:
            async with self._db.transaction(caller.scope()) as tx:
                claim = await self._idempotency.claim(
                    tx,
                    organization_id,
                    user_id,
                    BOOK_OPERATION,
                    idempotency_key,
                    hashed,
                    now,
                    self._retention.total_seconds(),
                )
                if not claim.is_new:
                    if claim.stored_request_hash != hashed:
                        raise rule(
                            "idempotency_key_reused", "This idempotency key was already used with a different request."
                        )
                    if claim.stored_response is None:
                        raise conflict(
                            "request_in_progress", "The original request is still being processed. Try again shortly."
                        )
                    replay = _from_stored(claim.stored_response)
                    raise _ReplayError
                view = await self._book_new(
                    tx, caller, request, idempotency_key, organization_id, user_id, actor, now, starts_at
                )
        except _ReplayError:
            assert replay is not None
            return BookResult(replay, True)
        return BookResult(view, False)

    async def _book_new(
        self,
        tx: Tx,
        caller: Caller,
        request: BookRequest,
        key: str,
        organization_id: UUID,
        user_id: UUID,
        actor: Actor,
        now: datetime,
        starts_at: datetime,
    ) -> Appointment:
        """Validate the slot and write the appointment, its history, reminders, audit, event and stored response."""
        await self._lock.acquire(tx, request.provider_id)
        settings = await load_settings(tx, organization_id)
        if settings is None:
            raise not_found("organization")
        slot = await self._find_open_slot(
            tx, caller, now, request.provider_id, request.location_id, starts_at, actor, None
        )
        patient_status = await tx.fetchval(
            "SELECT status FROM patient.patients WHERE id = :id AND organization_id = :organization",
            {"id": request.patient_id, "organization": organization_id},
        )
        if patient_status is None:
            raise field_error("patientId", "The patient does not exist in this organization.")
        if patient_status != ActiveStatus.ACTIVE:
            raise rule("patient_inactive", "Appointments cannot be booked for an inactive patient.")
        staff = actor == Actor.STAFF
        status = (
            AppointmentStatus.CONFIRMED
            if staff or settings.auto_confirm_patient_bookings
            else AppointmentStatus.PENDING
        )
        channel = BookingChannel.STAFF if staff else BookingChannel.PATIENT_PORTAL
        appointment_id = new_id()
        await tx.execute(
            """
            INSERT INTO scheduling.appointments (id, organization_id, patient_id, provider_id, clinic_location_id,
                starts_at, ends_at, status, visit_type, reason, booking_channel, booked_by_user_id, created_at,
                updated_at, version)
            VALUES (:id, :organization, :patient, :provider, :location, :starts, :ends, :status, :visit, :reason,
                :channel, :user, :now, :now, 1)
            """,
            {
                "id": appointment_id,
                "organization": organization_id,
                "patient": request.patient_id,
                "provider": request.provider_id,
                "location": slot.location_id,
                "starts": slot.starts_at,
                "ends": slot.ends_at,
                "status": status.value,
                "visit": request.visit_type.value,
                "reason": trimmed(request.reason),
                "channel": channel.value,
                "user": user_id,
                "now": now,
            },
        )
        await tx.execute(
            """
            INSERT INTO scheduling.appointment_status_history (id, organization_id, appointment_id, to_status,
                change_type, changed_by_user_id, changed_at)
            VALUES (:id, :organization, :appointment, :status, 'booked', :user, :now)
            """,
            {
                "id": new_id(),
                "organization": organization_id,
                "appointment": appointment_id,
                "status": status.value,
                "user": user_id,
                "now": now,
            },
        )
        await self._plan_reminders(
            tx, organization_id, appointment_id, slot.starts_at, list(settings.reminder_offsets_minutes), now
        )
        if request.waiting_list_entry_id is not None:
            fulfilled = await tx.execute(
                """
                UPDATE scheduling.waiting_list_entries SET status = 'fulfilled', fulfilled_appointment_id = :appointment,
                    updated_at = :now
                WHERE id = :id AND organization_id = :organization AND patient_id = :patient AND status = 'active'
                """,
                {
                    "id": request.waiting_list_entry_id,
                    "organization": organization_id,
                    "patient": request.patient_id,
                    "appointment": appointment_id,
                    "now": now,
                },
            )
            if fulfilled == 0:
                raise field_error("waitingListEntryId", "The waiting list entry is not active for this patient.")
        await audit(
            tx,
            caller,
            now,
            AuditEntry(
                "appointment.booked",
                "appointment",
                appointment_id,
                metadata={"providerId": str(request.provider_id), "patientId": str(request.patient_id)},
            ),
        )
        await outbox(
            tx,
            caller.organization_id,
            now,
            "appointment.booked",
            "appointment",
            appointment_id,
            _payload(
                organization_id,
                appointment_id,
                request.provider_id,
                request.patient_id,
                slot.location_id,
                slot.starts_at,
                slot.ends_at,
                status.value,
            ),
        )
        row = await tx.fetchrow(f"{_PROJECTION} WHERE a.id = :id", {"id": appointment_id})
        if row is None:
            raise not_found("appointment")
        view = _appointment_view(row)
        await self._idempotency.complete(
            tx, organization_id, user_id, BOOK_OPERATION, key, _stored_form(view), appointment_id
        )
        return view

    @staticmethod
    async def _load_for_change(tx: Tx, caller: Caller, appointment_id: UUID) -> Row:
        """Load and lock a visible appointment for modification or raise not found."""
        args = Args()
        condition = visible_condition(caller, args)
        row = await tx.fetchrow(
            "SELECT a.id, a.patient_id, a.provider_id, a.clinic_location_id AS location_id, a.starts_at, a.ends_at, "
            f"a.status, a.visit_type, a.reason, a.version FROM scheduling.appointments a WHERE {condition} "
            f"AND a.id = {args.add(appointment_id)} FOR UPDATE",
            args.values,
        )
        if row is None:
            raise not_found("appointment")
        return row

    async def update(self, caller: Caller, appointment_id: UUID, request: UpdateAppointmentRequest) -> Appointment:
        """Update the visit type and reason of an appointment that has not finished."""
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            current = await self._load_for_change(tx, caller, appointment_id)
            _resolve_actor(caller, current.patient_id, current.provider_id)
            ensure_version(current.version, request.version)
            if is_closed(AppointmentStatus(current.status)):
                raise conflict("appointment_closed", "This appointment can no longer be changed.")
            reason = trimmed(request.reason)
            if request.visit_type != current.visit_type or reason != current.reason:
                await tx.execute(
                    """
                    UPDATE scheduling.appointments SET visit_type = :visit, reason = :reason, version = version + 1,
                        updated_at = :now
                    WHERE id = :id
                    """,
                    {"id": current.id, "visit": request.visit_type.value, "reason": reason, "now": now},
                )
            await audit(tx, caller, now, AuditEntry("appointment.updated", "appointment", current.id))
            return await self._get_visible(tx, caller, current.id)

    async def confirm(self, caller: Caller, appointment_id: UUID) -> Appointment:
        """Confirm a pending appointment (staff only)."""
        return await self._change_status(caller, appointment_id, AppointmentStatus.CONFIRMED, None, True)

    async def check_in(self, caller: Caller, appointment_id: UUID) -> Appointment:
        """Record that the patient has arrived (staff only)."""
        return await self._change_status(caller, appointment_id, AppointmentStatus.CHECKED_IN, None, True)

    async def start(self, caller: Caller, appointment_id: UUID) -> Appointment:
        """Mark the visit as started; requires permission to conduct appointments."""
        caller.require(Permission.APPOINTMENTS_CONDUCT)
        return await self._change_status(caller, appointment_id, AppointmentStatus.IN_PROGRESS, None, True)

    async def complete(self, caller: Caller, appointment_id: UUID) -> Appointment:
        """Mark the visit as completed; requires permission to conduct appointments."""
        caller.require(Permission.APPOINTMENTS_CONDUCT)
        return await self._change_status(caller, appointment_id, AppointmentStatus.COMPLETED, None, True)

    async def mark_no_show(self, caller: Caller, appointment_id: UUID) -> Appointment:
        """Mark a confirmed appointment as a no-show once its start has passed (staff only)."""
        return await self._change_status(caller, appointment_id, AppointmentStatus.NO_SHOW, None, True)

    async def cancel(self, caller: Caller, appointment_id: UUID, request: CancelRequest) -> Appointment:
        """Cancel an appointment and free the slot; patients must cancel before the cancellation window."""
        return await self._change_status(
            caller, appointment_id, AppointmentStatus.CANCELLED, trimmed(request.reason), False
        )

    async def _change_status(
        self, caller: Caller, appointment_id: UUID, target: AppointmentStatus, reason: str | None, staff_only: bool
    ) -> Appointment:
        """Apply a status change with the authorization and timing rules of the target status."""
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            current = await self._load_for_change(tx, caller, appointment_id)
            actor = _resolve_actor(caller, current.patient_id, current.provider_id)
            if staff_only and actor != Actor.STAFF:
                raise permission_denied()
            if target == AppointmentStatus.NO_SHOW and now < current.starts_at:
                raise rule(
                    "appointment_not_started", "An appointment cannot be marked as a no-show before its start time."
                )
            organization_id = caller.require_organization_id()
            if target == AppointmentStatus.CANCELLED:
                settings = await load_settings(tx, organization_id)
                window = settings.patient_cancellation_window_hours if settings is not None else 0
                _ensure_outside_cancellation_window(current.starts_at, actor, window, now)
            previous = AppointmentStatus(current.status)
            try:
                ensure_transition(previous, target)
            except DomainError as error:
                raise from_domain(error) from error
            assignments = "status = :status, version = version + 1, updated_at = :now"
            params: dict[str, Any] = {"id": current.id, "status": target.value, "now": now}
            column = _STATUS_COLUMNS.get(target)
            if column is not None:
                assignments += f", {column} = :now"
            if target == AppointmentStatus.CANCELLED:
                assignments += ", cancellation_reason = :reason, cancelled_by_user_id = :user"
                params |= {"reason": reason, "user": caller.user_id}
            await tx.execute(f"UPDATE scheduling.appointments SET {assignments} WHERE id = :id", params)
            await tx.execute(
                """
                INSERT INTO scheduling.appointment_status_history (id, organization_id, appointment_id, from_status,
                    to_status, change_type, changed_by_user_id, changed_at, reason)
                VALUES (:id, :organization, :appointment, :from, :to, 'status_changed', :user, :now, :reason)
                """,
                {
                    "id": new_id(),
                    "organization": organization_id,
                    "appointment": current.id,
                    "from": previous.value,
                    "to": target.value,
                    "user": caller.user_id,
                    "now": now,
                    "reason": reason,
                },
            )
            if not occupies_slot(target):
                await self._cancel_pending_reminders(tx, organization_id, current.id)
            event = f"appointment.{target.value}"
            await audit(tx, caller, now, AuditEntry(event, "appointment", current.id))
            await outbox(
                tx,
                caller.organization_id,
                now,
                event,
                "appointment",
                current.id,
                _payload(
                    organization_id,
                    current.id,
                    current.provider_id,
                    current.patient_id,
                    current.location_id,
                    current.starts_at,
                    current.ends_at,
                    target.value,
                ),
            )
            return await self._get_visible(tx, caller, current.id)

    async def reschedule(self, caller: Caller, appointment_id: UUID, request: RescheduleRequest) -> Appointment:
        """Move an appointment to another open slot of the same provider."""
        now = self._clock()
        starts_at = request.starts_at.astimezone(UTC)
        async with self._db.transaction(caller.scope()) as tx:
            current = await self._load_for_change(tx, caller, appointment_id)
            actor = _resolve_actor(caller, current.patient_id, current.provider_id)
            location_id = request.location_id if request.location_id is not None else current.location_id
            if current.starts_at == starts_at and current.location_id == location_id:
                return await self._get_visible(tx, caller, current.id)
            organization_id = caller.require_organization_id()
            settings = await load_settings(tx, organization_id)
            if settings is None:
                raise not_found("organization")
            _ensure_outside_cancellation_window(
                current.starts_at, actor, settings.patient_cancellation_window_hours, now
            )
            await self._lock.acquire(tx, current.provider_id)
            slot = await self._find_open_slot(
                tx, caller, now, current.provider_id, location_id, starts_at, actor, current.id
            )
            try:
                ensure_reschedulable(AppointmentStatus(current.status), slot.starts_at, slot.ends_at)
            except DomainError as error:
                raise from_domain(error) from error
            await tx.execute(
                """
                UPDATE scheduling.appointments SET starts_at = :starts, ends_at = :ends, clinic_location_id = :location,
                    version = version + 1, updated_at = :now
                WHERE id = :id
                """,
                {
                    "id": current.id,
                    "starts": slot.starts_at,
                    "ends": slot.ends_at,
                    "location": slot.location_id,
                    "now": now,
                },
            )
            await tx.execute(
                """
                INSERT INTO scheduling.appointment_status_history (id, organization_id, appointment_id, from_status,
                    to_status, change_type, changed_by_user_id, changed_at, previous_starts_at, previous_ends_at)
                VALUES (:id, :organization, :appointment, :status, :status, 'rescheduled', :user, :now, :previous_start,
                    :previous_end)
                """,
                {
                    "id": new_id(),
                    "organization": organization_id,
                    "appointment": current.id,
                    "status": current.status,
                    "user": caller.user_id,
                    "now": now,
                    "previous_start": current.starts_at,
                    "previous_end": current.ends_at,
                },
            )
            await self._cancel_pending_reminders(tx, organization_id, current.id)
            await self._plan_reminders(
                tx, organization_id, current.id, slot.starts_at, list(settings.reminder_offsets_minutes), now
            )
            await audit(tx, caller, now, AuditEntry("appointment.rescheduled", "appointment", current.id))
            await outbox(
                tx,
                caller.organization_id,
                now,
                "appointment.rescheduled",
                "appointment",
                current.id,
                _payload(
                    organization_id,
                    current.id,
                    current.provider_id,
                    current.patient_id,
                    slot.location_id,
                    slot.starts_at,
                    slot.ends_at,
                    current.status,
                ),
            )
            return await self._get_visible(tx, caller, current.id)


class _ReplayError(Exception):
    """Ends a booking transaction without committing when a stored response is replayed."""
