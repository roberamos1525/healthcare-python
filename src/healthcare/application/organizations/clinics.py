"""Clinic, location and holiday use cases."""

from datetime import date
from typing import Final
from uuid import UUID

from healthcare.application.core.audit import Args, AuditEntry, audit, where
from healthcare.application.core.caller import Caller
from healthcare.application.core.errors import concurrency_conflict, ensure_version, not_found, require_version
from healthcare.application.core.paging import Paged, PageRequest, paged
from healthcare.application.core.ports import Clock, Database, Row, Tx, new_id
from healthcare.application.core.validation import trimmed
from healthcare.application.organizations.contracts import (
    Clinic,
    ClinicRequest,
    Holiday,
    HolidayRequest,
    Location,
    LocationRequest,
)
from healthcare.domain.authorization import Permission
from healthcare.domain.enums import ActiveStatus

_CLINIC_COLUMNS: Final = "id, name, description, phone, email, status, version, created_at, updated_at"
_LOCATION_COLUMNS: Final = (
    "id, clinic_id, name, address_line1, address_line2, city, state, postal_code, country, time_zone, phone, status, "
    "version, created_at, updated_at"
)


def _clinic_view(row: Row) -> Clinic:
    """Build the API representation of a clinic row."""
    return Clinic.model_construct(
        id=row.id,
        name=row.name,
        description=row.description,
        phone=row.phone,
        email=row.email,
        status=ActiveStatus(row.status),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _location_view(row: Row) -> Location:
    """Build the API representation of a location row."""
    return Location.model_construct(
        id=row.id,
        clinic_id=row.clinic_id,
        name=row.name,
        address_line1=row.address_line1,
        address_line2=row.address_line2,
        city=row.city,
        state=row.state,
        postal_code=row.postal_code,
        country=row.country,
        time_zone=row.time_zone,
        phone=row.phone,
        status=ActiveStatus(row.status),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def find_location(tx: Tx, organization_id: UUID | None, location_id: UUID) -> Row:
    """Load a location of the organization or raise not found."""
    row = await tx.fetchrow(
        f"SELECT {_LOCATION_COLUMNS} FROM organization.clinic_locations WHERE id = :id AND organization_id = :organization",
        {"id": location_id, "organization": organization_id},
    )
    if row is None:
        raise not_found("location")
    return row


async def _find_clinic(tx: Tx, organization_id: UUID | None, clinic_id: UUID) -> Row:
    """Load a clinic of the organization or raise not found."""
    row = await tx.fetchrow(
        f"SELECT {_CLINIC_COLUMNS} FROM organization.clinics WHERE id = :id AND organization_id = :organization",
        {"id": clinic_id, "organization": organization_id},
    )
    if row is None:
        raise not_found("clinic")
    return row


class ClinicService:
    """Implements the clinic, location and holiday use cases."""

    def __init__(self, db: Database, clock: Clock) -> None:
        """Wire the service to its ports."""
        self._db = db
        self._clock = clock

    async def list_clinics(self, caller: Caller, status: ActiveStatus | None, page: PageRequest) -> Paged[Clinic]:
        """List the clinics of the selected organization."""
        caller.require(Permission.CLINICS_READ)
        args = Args()
        conditions = [f"organization_id = {args.add(caller.organization_id)}"]
        if status is not None:
            conditions.append(f"status = {args.add(status.value)}")
        clause = where(conditions)
        async with self._db.transaction(caller.scope()) as tx:
            total = await tx.fetchval(f"SELECT count(*) FROM organization.clinics{clause}", args.values)
            limit, offset = args.add(page.safe_page_size), args.add(page.offset)
            rows = await tx.fetch(
                f"SELECT {_CLINIC_COLUMNS} FROM organization.clinics{clause} ORDER BY name, id "
                f"LIMIT {limit} OFFSET {offset}",
                args.values,
            )
        return paged([_clinic_view(row) for row in rows], page, total)

    async def create_clinic(self, caller: Caller, request: ClinicRequest) -> Clinic:
        """Create a clinic."""
        caller.require(Permission.CLINICS_MANAGE)
        organization_id = caller.require_organization_id()
        now = self._clock()
        clinic = Clinic.model_construct(
            id=new_id(),
            name=request.name.strip(),
            description=trimmed(request.description),
            phone=trimmed(request.phone),
            email=trimmed(request.email),
            status=request.status,
            version=1,
            created_at=now,
            updated_at=now,
        )
        async with self._db.transaction(caller.scope()) as tx:
            await tx.execute(
                """
                INSERT INTO organization.clinics (id, organization_id, name, description, phone, email, status,
                    created_at, updated_at, version)
                VALUES (:id, :organization, :name, :description, :phone, :email, :status, :now, :now, 1)
                """,
                {
                    "id": clinic.id,
                    "organization": organization_id,
                    "name": clinic.name,
                    "description": clinic.description,
                    "phone": clinic.phone,
                    "email": clinic.email,
                    "status": request.status.value,
                    "now": now,
                },
            )
            await audit(tx, caller, now, AuditEntry("clinic.created", "clinic", clinic.id))
        return clinic

    async def get_clinic(self, caller: Caller, clinic_id: UUID) -> Clinic:
        """Return one clinic."""
        caller.require(Permission.CLINICS_READ)
        async with self._db.transaction(caller.scope()) as tx:
            return _clinic_view(await _find_clinic(tx, caller.organization_id, clinic_id))

    async def update_clinic(self, caller: Caller, clinic_id: UUID, request: ClinicRequest) -> Clinic:
        """Update a clinic; the request carries the version the client last read."""
        caller.require(Permission.CLINICS_MANAGE)
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            current = await _find_clinic(tx, caller.organization_id, clinic_id)
            ensure_version(current.version, require_version(request.version))
            values = (
                request.name.strip(),
                trimmed(request.description),
                trimmed(request.phone),
                trimmed(request.email),
                request.status.value,
            )
            version, updated_at = current.version, current.updated_at
            if values != (current.name, current.description, current.phone, current.email, current.status):
                version, updated_at = current.version + 1, now
                affected = await tx.execute(
                    """
                    UPDATE organization.clinics SET name = :name, description = :description, phone = :phone,
                        email = :email, status = :status, version = :version, updated_at = :now
                    WHERE id = :id AND version = :expected
                    """,
                    {
                        "id": clinic_id,
                        "expected": current.version,
                        "name": values[0],
                        "description": values[1],
                        "phone": values[2],
                        "email": values[3],
                        "status": values[4],
                        "version": version,
                        "now": now,
                    },
                )
                if affected == 0:
                    raise concurrency_conflict()
            await audit(tx, caller, now, AuditEntry("clinic.updated", "clinic", clinic_id))
        return Clinic.model_construct(
            id=current.id,
            name=values[0],
            description=values[1],
            phone=values[2],
            email=values[3],
            status=request.status,
            version=version,
            created_at=current.created_at,
            updated_at=updated_at,
        )

    async def list_locations(self, caller: Caller, clinic_id: UUID | None, page: PageRequest) -> Paged[Location]:
        """List the locations of a clinic, or of the whole organization when no clinic is given."""
        caller.require(Permission.CLINICS_READ)
        args = Args()
        conditions = [f"organization_id = {args.add(caller.organization_id)}"]
        if clinic_id is not None:
            conditions.append(f"clinic_id = {args.add(clinic_id)}")
        clause = where(conditions)
        async with self._db.transaction(caller.scope()) as tx:
            total = await tx.fetchval(f"SELECT count(*) FROM organization.clinic_locations{clause}", args.values)
            limit, offset = args.add(page.safe_page_size), args.add(page.offset)
            rows = await tx.fetch(
                f"SELECT {_LOCATION_COLUMNS} FROM organization.clinic_locations{clause} ORDER BY name, id "
                f"LIMIT {limit} OFFSET {offset}",
                args.values,
            )
        return paged([_location_view(row) for row in rows], page, total)

    async def create_location(self, caller: Caller, clinic_id: UUID, request: LocationRequest) -> Location:
        """Add a location to a clinic."""
        caller.require(Permission.CLINICS_MANAGE)
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            await _find_clinic(tx, caller.organization_id, clinic_id)
            location = Location.model_construct(
                id=new_id(),
                clinic_id=clinic_id,
                name=request.name.strip(),
                address_line1=request.address_line1.strip(),
                address_line2=trimmed(request.address_line2),
                city=request.city.strip(),
                state=request.state,
                postal_code=request.postal_code,
                country="US",
                time_zone=request.time_zone,
                phone=trimmed(request.phone),
                status=request.status,
                version=1,
                created_at=now,
                updated_at=now,
            )
            await tx.execute(
                """
                INSERT INTO organization.clinic_locations (id, organization_id, clinic_id, name, address_line1,
                    address_line2, city, state, postal_code, country, time_zone, phone, status, created_at, updated_at,
                    version)
                VALUES (:id, :organization, :clinic, :name, :line1, :line2, :city, :state, :postal, 'US', :zone, :phone,
                    :status, :now, :now, 1)
                """,
                {
                    "id": location.id,
                    "organization": caller.organization_id,
                    "clinic": clinic_id,
                    "name": location.name,
                    "line1": location.address_line1,
                    "line2": location.address_line2,
                    "city": location.city,
                    "state": location.state,
                    "postal": location.postal_code,
                    "zone": location.time_zone,
                    "phone": location.phone,
                    "status": request.status.value,
                    "now": now,
                },
            )
            await audit(tx, caller, now, AuditEntry("location.created", "clinic_location", location.id))
        return location

    async def get_location(self, caller: Caller, location_id: UUID) -> Location:
        """Return one location."""
        caller.require(Permission.CLINICS_READ)
        async with self._db.transaction(caller.scope()) as tx:
            return _location_view(await find_location(tx, caller.organization_id, location_id))

    async def update_location(self, caller: Caller, location_id: UUID, request: LocationRequest) -> Location:
        """Update a location; the request carries the version the client last read."""
        caller.require(Permission.CLINICS_MANAGE)
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            current = await find_location(tx, caller.organization_id, location_id)
            ensure_version(current.version, require_version(request.version))
            values = (
                request.name.strip(),
                request.address_line1.strip(),
                trimmed(request.address_line2),
                request.city.strip(),
                request.state,
                request.postal_code,
                request.time_zone,
                trimmed(request.phone),
                request.status.value,
            )
            stored = (
                current.name,
                current.address_line1,
                current.address_line2,
                current.city,
                current.state,
                current.postal_code,
                current.time_zone,
                current.phone,
                current.status,
            )
            version, updated_at = current.version, current.updated_at
            if values != stored:
                version, updated_at = current.version + 1, now
                affected = await tx.execute(
                    """
                    UPDATE organization.clinic_locations SET name = :name, address_line1 = :line1,
                        address_line2 = :line2, city = :city, state = :state, postal_code = :postal, time_zone = :zone,
                        phone = :phone, status = :status, version = :version, updated_at = :now
                    WHERE id = :id AND version = :expected
                    """,
                    {
                        "id": location_id,
                        "expected": current.version,
                        "name": values[0],
                        "line1": values[1],
                        "line2": values[2],
                        "city": values[3],
                        "state": values[4],
                        "postal": values[5],
                        "zone": values[6],
                        "phone": values[7],
                        "status": values[8],
                        "version": version,
                        "now": now,
                    },
                )
                if affected == 0:
                    raise concurrency_conflict()
            await audit(tx, caller, now, AuditEntry("location.updated", "clinic_location", location_id))
        return Location.model_construct(
            id=current.id,
            clinic_id=current.clinic_id,
            name=values[0],
            address_line1=values[1],
            address_line2=values[2],
            city=values[3],
            state=values[4],
            postal_code=values[5],
            country=current.country,
            time_zone=values[6],
            phone=values[7],
            status=request.status,
            version=version,
            created_at=current.created_at,
            updated_at=updated_at,
        )

    async def list_holidays(self, caller: Caller, date_from: date | None, date_to: date | None) -> list[Holiday]:
        """List closure days in a date range (at most 500)."""
        caller.require(Permission.CLINICS_READ)
        args = Args()
        conditions = [f"organization_id = {args.add(caller.organization_id)}"]
        if date_from is not None:
            conditions.append(f"date >= {args.add(date_from)}")
        if date_to is not None:
            conditions.append(f"date <= {args.add(date_to)}")
        async with self._db.transaction(caller.scope()) as tx:
            rows = await tx.fetch(
                f"SELECT id, date, name, clinic_location_id FROM organization.clinic_holidays{where(conditions)} "
                "ORDER BY date, id LIMIT 500",
                args.values,
            )
        return [
            Holiday.model_construct(id=row.id, date=row.date, name=row.name, location_id=row.clinic_location_id)
            for row in rows
        ]

    async def create_holiday(self, caller: Caller, request: HolidayRequest) -> Holiday:
        """Add a closure day for one location or for the whole organization."""
        caller.require(Permission.CLINICS_MANAGE)
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            if request.location_id is not None:
                await find_location(tx, caller.organization_id, request.location_id)
            organization_id = caller.require_organization_id()
            holiday = Holiday.model_construct(
                id=new_id(), date=request.date, name=request.name.strip(), location_id=request.location_id
            )
            await tx.execute(
                """
                INSERT INTO organization.clinic_holidays (id, organization_id, clinic_location_id, date, name, created_at)
                VALUES (:id, :organization, :location, :date, :name, :now)
                """,
                {
                    "id": holiday.id,
                    "organization": organization_id,
                    "location": holiday.location_id,
                    "date": request.date,
                    "name": holiday.name,
                    "now": now,
                },
            )
            await audit(tx, caller, now, AuditEntry("holiday.created", "clinic_holiday", holiday.id))
        return holiday

    async def delete_holiday(self, caller: Caller, holiday_id: UUID) -> None:
        """Remove a closure day."""
        caller.require(Permission.CLINICS_MANAGE)
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            deleted = await tx.execute(
                "DELETE FROM organization.clinic_holidays WHERE id = :id AND organization_id = :organization",
                {"id": holiday_id, "organization": caller.organization_id},
            )
            if deleted == 0:
                raise not_found("holiday")
            await audit(tx, caller, now, AuditEntry("holiday.deleted", "clinic_holiday", holiday_id))
