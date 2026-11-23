"""Patient use cases: registration, minimum-necessary reads, updates, contacts, consents and care relationships."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final
from uuid import UUID

from healthcare.application.access import AccessResolver
from healthcare.application.core.audit import Args, AuditEntry, audit, outbox, where
from healthcare.application.core.caller import Caller
from healthcare.application.core.errors import (
    concurrency_conflict,
    conflict,
    ensure_version,
    field_error,
    from_domain,
    not_found,
    require_version,
    rule,
)
from healthcare.application.core.paging import Paged, PageRequest, like_starts_with, paged, parse_sort
from healthcare.application.core.ports import Clock, Database, Row, Tx, new_id
from healthcare.application.core.validation import is_blank, trimmed
from healthcare.application.patients.contracts import (
    Address,
    AddressView,
    CareRelationship,
    CareRelationshipRequest,
    Consent,
    ConsentRequest,
    EmergencyContact,
    EmergencyContactRequest,
    Patient,
    PatientRequest,
    PatientSummary,
    SelfRegistrationRequest,
)
from healthcare.application.providers.profiles import culture_key
from healthcare.application.security import random_int
from healthcare.domain.authorization import Permission
from healthcare.domain.enums import (
    ActiveStatus,
    AdministrativeGender,
    CareRelationshipType,
    ConsentMethod,
    ConsentStatus,
    ConsentType,
)
from healthcare.domain.rules import DomainError, ensure_revocable

_PATIENT_COLUMNS: Final = (
    "p.id, p.organization_id, p.medical_record_number, p.first_name, p.middle_name, p.last_name, p.preferred_name, "
    "p.date_of_birth, p.administrative_gender, p.email, p.phone, p.address_line1, p.address_line2, p.city, p.state, "
    "p.postal_code, p.country, p.preferred_language, p.status, p.version, p.created_at, p.updated_at"
)
_CONSENT_COLUMNS: Final = "id, type, status, method, document_version, granted_at, revoked_at"


def visible_condition(caller: Caller, args: Args) -> str:
    """Return the SQL condition (alias ``p``) limiting patients to those the caller may read.

    Staff with patients.read see all patients, providers see patients in their care, and patients see their
    linked records (minimum-necessary access).
    """
    organization = f"p.organization_id = {args.add(caller.organization_id)}"
    if caller.has(Permission.PATIENTS_READ):
        return organization
    provider_id = caller.provider_id if caller.has(Permission.PATIENTS_READ_ASSIGNED) else None
    own = caller.patient_ids if caller.has(Permission.PATIENTS_SELF) else []
    if provider_id is None and not own:
        caller.require(Permission.PATIENTS_READ)
    parts: list[str] = []
    if own:
        parts.append(f"p.id = ANY({args.add(list(own))})")
    if provider_id is not None:
        provider = args.add(provider_id)
        parts.append(
            "EXISTS (SELECT 1 FROM patient.patient_provider_relationships r WHERE r.organization_id = p.organization_id "
            f"AND r.patient_id = p.id AND r.provider_id = {provider} AND r.ended_on IS NULL)"
        )
        parts.append(
            "EXISTS (SELECT 1 FROM scheduling.appointments a WHERE a.organization_id = p.organization_id "
            f"AND a.patient_id = p.id AND a.provider_id = {provider})"
        )
    return f"{organization} AND ({' OR '.join(parts)})"


@dataclass(slots=True)
class StoredPatient:
    """A patient row in editable form."""

    id: UUID
    organization_id: UUID
    medical_record_number: str
    first_name: str
    middle_name: str | None
    last_name: str
    preferred_name: str | None
    date_of_birth: date
    gender: AdministrativeGender
    email: str | None
    phone: str | None
    address_line1: str | None
    address_line2: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    country: str | None
    preferred_language: str | None
    status: ActiveStatus
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Row) -> StoredPatient:
        """Read a patient row."""
        return cls(
            row.id,
            row.organization_id,
            row.medical_record_number,
            row.first_name,
            row.middle_name,
            row.last_name,
            row.preferred_name,
            row.date_of_birth,
            AdministrativeGender(row.administrative_gender),
            row.email,
            row.phone,
            row.address_line1,
            row.address_line2,
            row.city,
            row.state,
            row.postal_code,
            row.country,
            row.preferred_language,
            ActiveStatus(row.status),
            row.version,
            row.created_at,
            row.updated_at,
        )

    def comparable(self) -> tuple[object, ...]:
        """Return every editable field, for change detection."""
        return (
            self.first_name,
            self.middle_name,
            self.last_name,
            self.preferred_name,
            self.date_of_birth,
            self.gender,
            self.email,
            self.phone,
            self.address_line1,
            self.address_line2,
            self.city,
            self.state,
            self.postal_code,
            self.country,
            self.preferred_language,
            self.status,
        )

    def view(self) -> Patient:
        """Build the API representation; the address is omitted without line 1, city, state or ZIP."""
        address = None
        if any(part is not None for part in (self.address_line1, self.city, self.state, self.postal_code)):
            address = AddressView.model_construct(
                line1=self.address_line1,
                line2=self.address_line2,
                city=self.city,
                state=self.state,
                postal_code=self.postal_code,
            )
        return Patient.model_construct(
            id=self.id,
            medical_record_number=self.medical_record_number,
            first_name=self.first_name,
            middle_name=self.middle_name,
            last_name=self.last_name,
            preferred_name=self.preferred_name,
            date_of_birth=self.date_of_birth,
            administrative_gender=self.gender,
            email=self.email,
            phone=self.phone,
            address=address,
            preferred_language=self.preferred_language,
            status=self.status,
            version=self.version,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    def apply_contact(
        self,
        email: str | None,
        phone: str | None,
        address: Address | None,
        preferred_name: str | None,
        preferred_language: str | None,
    ) -> None:
        """Copy contact details and preferences onto the patient."""
        self.email, self.phone = trimmed(email), trimmed(phone)
        self.preferred_name, self.preferred_language = trimmed(preferred_name), trimmed(preferred_language)
        if address is None:
            self.address_line1 = self.address_line2 = self.city = self.state = self.postal_code = self.country = None
            return
        self.address_line1, self.address_line2 = trimmed(address.line1), trimmed(address.line2)
        self.city, self.state, self.postal_code, self.country = (
            trimmed(address.city),
            address.state,
            address.postal_code,
            "US",
        )

    def apply_identity(self, request: PatientRequest) -> None:
        """Copy name, date of birth and gender from the request."""
        self.first_name, self.middle_name = request.first_name.strip(), trimmed(request.middle_name)
        self.last_name = request.last_name.strip()
        self.date_of_birth, self.gender = request.date_of_birth, request.administrative_gender


async def readable(tx: Tx, caller: Caller, patient_id: UUID, lock: bool = False) -> StoredPatient:
    """Load a patient the caller may read, or raise not found so existence is not revealed."""
    args = Args()
    condition = visible_condition(caller, args)
    suffix = " FOR UPDATE OF p" if lock else ""
    row = await tx.fetchrow(
        f"SELECT {_PATIENT_COLUMNS} FROM patient.patients p WHERE {condition} AND p.id = {args.add(patient_id)}{suffix}",
        args.values,
    )
    if row is None:
        raise not_found("patient")
    return StoredPatient.from_row(row)


def _is_self(caller: Caller, patient_id: UUID) -> bool:
    """Return whether the caller is the patient (or their representative) rather than staff."""
    return caller.has(Permission.PATIENTS_SELF) and caller.is_linked_patient(patient_id)


def _ensure_can_write(caller: Caller, patient_id: UUID) -> None:
    """Allow staff with write access, or the patient."""
    if not _is_self(caller, patient_id):
        caller.require(Permission.PATIENTS_WRITE)


async def _next_medical_record_number(tx: Tx, organization_id: UUID) -> str:
    """Generate a ten-digit medical record number unused in the organization."""
    for _ in range(5):
        candidate = f"{random_int(100_000_000, 1_000_000_000)}{random_int(0, 10)}"
        exists = await tx.fetchval(
            """
            SELECT EXISTS (SELECT 1 FROM patient.patients
                WHERE organization_id = :organization AND medical_record_number = :mrn)
            """,
            {"organization": organization_id, "mrn": candidate},
        )
        if not exists:
            return candidate
    raise RuntimeError("Could not allocate a medical record number.")


async def _insert_patient(tx: Tx, patient: StoredPatient, created_by: UUID | None) -> None:
    """Store a new patient row."""
    await tx.execute(
        """
        INSERT INTO patient.patients (id, organization_id, medical_record_number, first_name, middle_name, last_name,
            preferred_name, date_of_birth, administrative_gender, email, phone, address_line1, address_line2, city,
            state, postal_code, country, preferred_language, status, created_by_user_id, created_at, updated_at, version)
        VALUES (:id, :organization, :mrn, :first, :middle, :last, :preferred, :dob, :gender, :email, :phone, :line1,
            :line2, :city, :state, :postal, :country, :language, 'active', :created_by, :now, :now, 1)
        """,
        {
            "id": patient.id,
            "organization": patient.organization_id,
            "mrn": patient.medical_record_number,
            "first": patient.first_name,
            "middle": patient.middle_name,
            "last": patient.last_name,
            "preferred": patient.preferred_name,
            "dob": patient.date_of_birth,
            "gender": patient.gender.value,
            "email": patient.email,
            "phone": patient.phone,
            "line1": patient.address_line1,
            "line2": patient.address_line2,
            "city": patient.city,
            "state": patient.state,
            "postal": patient.postal_code,
            "country": patient.country,
            "language": patient.preferred_language,
            "created_by": created_by,
            "now": patient.created_at,
        },
    )


def _new_patient(
    organization_id: UUID, now: datetime, first_name: str, last_name: str, dob: date, gender: AdministrativeGender
) -> StoredPatient:
    """Create an empty active patient record."""
    return StoredPatient(
        new_id(),
        organization_id,
        "",
        first_name,
        None,
        last_name,
        None,
        dob,
        gender,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        ActiveStatus.ACTIVE,
        1,
        now,
        now,
    )


def _consent_view(row: Row) -> Consent:
    """Build the API representation of a consent row."""
    return Consent.model_construct(
        id=row.id,
        type=ConsentType(row.type),
        status=ConsentStatus(row.status),
        method=ConsentMethod(row.method),
        document_version=row.document_version,
        granted_at=row.granted_at,
        revoked_at=row.revoked_at,
    )


def _contact_view(row: Row) -> EmergencyContact:
    """Build the API representation of an emergency contact row."""
    return EmergencyContact.model_construct(
        id=row.id,
        name=row.name,
        relationship=row.relationship,
        phone=row.phone,
        email=row.email,
        is_primary=row.is_primary,
    )


def _contact_from(contact_id: UUID, request: EmergencyContactRequest) -> EmergencyContact:
    """Build a contact from the request."""
    return EmergencyContact.model_construct(
        id=contact_id,
        name=request.name.strip(),
        relationship=request.relationship.strip(),
        phone=request.phone.strip(),
        email=trimmed(request.email),
        is_primary=request.is_primary,
    )


class PatientService:
    """Implements the patient use cases."""

    def __init__(self, db: Database, access: AccessResolver, clock: Clock) -> None:
        """Wire the service to its ports."""
        self._db = db
        self._access = access
        self._clock = clock

    async def list_patients(
        self,
        caller: Caller,
        search: str | None,
        date_of_birth: date | None,
        status: ActiveStatus | None,
        sort: str | None,
        page: PageRequest,
    ) -> Paged[PatientSummary]:
        """Search visible patients by name prefix or MRN; the search is audited with the result count only."""
        args = Args()
        conditions = [visible_condition(caller, args)]
        if not is_blank(search):
            term = (search or "").strip()
            pattern = args.add(like_starts_with(term))
            conditions.append(
                f"(p.medical_record_number = {args.add(term)} OR lower(p.last_name) LIKE {pattern} ESCAPE '\\' "
                f"OR lower(p.first_name) LIKE {pattern} ESCAPE '\\')"
            )
        if date_of_birth is not None:
            conditions.append(f"p.date_of_birth = {args.add(date_of_birth)}")
        if status is not None:
            conditions.append(f"p.status = {args.add(status.value)}")
        order = parse_sort(sort, ["lastName", "createdAt"], "lastName")[0]
        if order.name == "createdAt":
            order_by = "p.created_at DESC, p.id" if order.descending else "p.created_at, p.id"
        else:
            order_by = (
                "p.last_name DESC, p.first_name DESC, p.id" if order.descending else "p.last_name, p.first_name, p.id"
            )
        clause = where(conditions)
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            total = await tx.fetchval(f"SELECT count(*) FROM patient.patients p{clause}", args.values)
            limit, offset = args.add(page.safe_page_size), args.add(page.offset)
            rows = await tx.fetch(
                "SELECT p.id, p.medical_record_number, p.first_name, p.last_name, p.date_of_birth, p.phone, p.status "
                f"FROM patient.patients p{clause} ORDER BY {order_by} LIMIT {limit} OFFSET {offset}",
                args.values,
            )
            await audit(tx, caller, now, AuditEntry("patient.search", "patient", metadata={"resultCount": len(rows)}))
        items = [
            PatientSummary.model_construct(
                id=row.id,
                medical_record_number=row.medical_record_number,
                first_name=row.first_name,
                last_name=row.last_name,
                date_of_birth=row.date_of_birth,
                phone=row.phone,
                status=ActiveStatus(row.status),
            )
            for row in rows
        ]
        return paged(items, page, total)

    async def get(self, caller: Caller, patient_id: UUID) -> Patient:
        """Return a patient's demographics and record the access in the audit trail."""
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            patient = await readable(tx, caller, patient_id)
            await audit(tx, caller, now, AuditEntry("patient.read", "patient", patient.id))
        return patient.view()

    async def create(self, caller: Caller, request: PatientRequest) -> Patient:
        """Register a patient with the selected organization and assign a medical record number."""
        caller.require(Permission.PATIENTS_WRITE)
        organization_id = caller.require_organization_id()
        now = self._clock()
        patient = _new_patient(organization_id, now, "", "", request.date_of_birth, request.administrative_gender)
        patient.apply_identity(request)
        patient.apply_contact(
            request.email, request.phone, request.address, request.preferred_name, request.preferred_language
        )
        async with self._db.transaction(caller.scope()) as tx:
            patient.medical_record_number = await _next_medical_record_number(tx, organization_id)
            await _insert_patient(tx, patient, caller.user_id)
            await audit(tx, caller, now, AuditEntry("patient.created", "patient", patient.id))
            await outbox(
                tx,
                caller.organization_id,
                now,
                "patient.registered",
                "patient",
                patient.id,
                {"patientId": str(patient.id)},
            )
        return patient.view()

    async def self_register(self, caller: Caller, request: SelfRegistrationRequest) -> Patient:
        """Register the signed-in user as a patient of the selected organization and link the record."""
        user_id = caller.require_user_id()
        organization_id = caller.require_organization_id()
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            linked = await tx.fetchval(
                """
                SELECT EXISTS (SELECT 1 FROM patient.patient_account_links
                    WHERE organization_id = :organization AND user_id = :user AND relationship = 'self')
                """,
                {"organization": organization_id, "user": user_id},
            )
            if linked:
                raise conflict("already_registered", "You are already registered as a patient of this organization.")
            caller.require(Permission.PATIENTS_SELF_REGISTER)
            user = await tx.fetchrow(
                "SELECT email, first_name, last_name, phone FROM identity.users WHERE id = :id", {"id": user_id}
            )
            assert user is not None
            patient = _new_patient(
                organization_id,
                now,
                user.first_name,
                user.last_name,
                request.date_of_birth,
                request.administrative_gender,
            )
            patient.medical_record_number = await _next_medical_record_number(tx, organization_id)
            phone = request.phone if request.phone is not None else user.phone
            patient.apply_contact(user.email, phone, request.address, None, request.preferred_language)
            await _insert_patient(tx, patient, user_id)
            await tx.execute(
                """
                INSERT INTO patient.patient_account_links (id, organization_id, patient_id, user_id, relationship,
                    created_at)
                VALUES (:id, :organization, :patient, :user, 'self', :now)
                """,
                {"id": new_id(), "organization": organization_id, "patient": patient.id, "user": user_id, "now": now},
            )
            await audit(tx, caller, now, AuditEntry("patient.self_registered", "patient", patient.id))
            await outbox(
                tx,
                caller.organization_id,
                now,
                "patient.registered",
                "patient",
                patient.id,
                {"patientId": str(patient.id)},
            )
        await self._access.invalidate_access(organization_id, user_id)
        return patient.view()

    async def update(self, caller: Caller, patient_id: UUID, request: PatientRequest) -> Patient:
        """Update a patient; patients may change only their contact details, never identity fields."""
        _ensure_can_write(caller, patient_id)
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            patient = await readable(tx, caller, patient_id, lock=True)
            ensure_version(patient.version, require_version(request.version))
            before = patient.comparable()
            if caller.has(Permission.PATIENTS_WRITE):
                patient.apply_identity(request)
                if request.status is not None:
                    patient.status = request.status
            elif (
                patient.first_name != request.first_name.strip()
                or patient.last_name != request.last_name.strip()
                or patient.middle_name != trimmed(request.middle_name)
                or patient.date_of_birth != request.date_of_birth
                or patient.gender != request.administrative_gender
            ):
                raise rule(
                    "identity_fields_locked", "Contact the practice to change your name, date of birth or gender."
                )
            patient.apply_contact(
                request.email, request.phone, request.address, request.preferred_name, request.preferred_language
            )
            if patient.comparable() != before:
                expected = patient.version
                patient.version, patient.updated_at = expected + 1, now
                affected = await tx.execute(
                    """
                    UPDATE patient.patients SET first_name = :first, middle_name = :middle, last_name = :last,
                        preferred_name = :preferred, date_of_birth = :dob, administrative_gender = :gender,
                        email = :email, phone = :phone, address_line1 = :line1, address_line2 = :line2, city = :city,
                        state = :state, postal_code = :postal, country = :country, preferred_language = :language,
                        status = :status, version = :version, updated_at = :now
                    WHERE id = :id AND version = :expected
                    """,
                    {
                        "id": patient.id,
                        "expected": expected,
                        "first": patient.first_name,
                        "middle": patient.middle_name,
                        "last": patient.last_name,
                        "preferred": patient.preferred_name,
                        "dob": patient.date_of_birth,
                        "gender": patient.gender.value,
                        "email": patient.email,
                        "phone": patient.phone,
                        "line1": patient.address_line1,
                        "line2": patient.address_line2,
                        "city": patient.city,
                        "state": patient.state,
                        "postal": patient.postal_code,
                        "country": patient.country,
                        "language": patient.preferred_language,
                        "status": patient.status.value,
                        "version": patient.version,
                        "now": now,
                    },
                )
                if affected == 0:
                    raise concurrency_conflict()
            await audit(tx, caller, now, AuditEntry("patient.updated", "patient", patient.id))
        return patient.view()

    async def list_emergency_contacts(self, caller: Caller, patient_id: UUID) -> list[EmergencyContact]:
        """List a patient's emergency contacts, primary first."""
        async with self._db.transaction(caller.scope()) as tx:
            patient = await readable(tx, caller, patient_id)
            rows = await tx.fetch(
                """
                SELECT id, name, relationship, phone, email, is_primary FROM patient.emergency_contacts
                WHERE organization_id = :organization AND patient_id = :patient
                """,
                {"organization": patient.organization_id, "patient": patient.id},
            )
        contacts = [_contact_view(row) for row in rows]
        contacts.sort(key=lambda contact: (not contact.is_primary, culture_key(contact.name)))
        return contacts

    @staticmethod
    async def _save_contact(
        tx: Tx,
        organization_id: UUID,
        patient_id: UUID,
        contact: EmergencyContact,
        was_primary: bool,
        insert: bool,
        now: datetime,
    ) -> None:
        """Write a contact and keep a single primary contact per patient."""
        if contact.is_primary and not was_primary:
            await tx.execute(
                """
                UPDATE patient.emergency_contacts SET is_primary = false, updated_at = :now
                WHERE organization_id = :organization AND patient_id = :patient AND is_primary AND id <> :id
                """,
                {"organization": organization_id, "patient": patient_id, "now": now, "id": contact.id},
            )
        params = {
            "id": contact.id,
            "name": contact.name,
            "relationship": contact.relationship,
            "phone": contact.phone,
            "email": contact.email,
            "primary": contact.is_primary,
            "now": now,
        }
        if insert:
            await tx.execute(
                """
                INSERT INTO patient.emergency_contacts (id, organization_id, patient_id, name, relationship, phone,
                    email, is_primary, created_at, updated_at)
                VALUES (:id, :organization, :patient, :name, :relationship, :phone, :email, :primary, :now, :now)
                """,
                {**params, "organization": organization_id, "patient": patient_id},
            )
        else:
            await tx.execute(
                """
                UPDATE patient.emergency_contacts SET name = :name, relationship = :relationship, phone = :phone,
                    email = :email, is_primary = :primary, updated_at = :now
                WHERE id = :id
                """,
                params,
            )

    async def add_emergency_contact(
        self, caller: Caller, patient_id: UUID, request: EmergencyContactRequest
    ) -> EmergencyContact:
        """Add an emergency contact; marking it primary demotes the previous primary contact."""
        _ensure_can_write(caller, patient_id)
        now = self._clock()
        contact = _contact_from(new_id(), request)
        async with self._db.transaction(caller.scope()) as tx:
            patient = await readable(tx, caller, patient_id)
            await self._save_contact(tx, patient.organization_id, patient.id, contact, False, True, now)
            await audit(tx, caller, now, AuditEntry("emergency_contact.created", "patient", patient.id))
        return contact

    @staticmethod
    async def _find_contact(tx: Tx, patient: StoredPatient, contact_id: UUID) -> Row:
        """Load a contact of the patient or raise not found."""
        row = await tx.fetchrow(
            """
            SELECT id, name, relationship, phone, email, is_primary FROM patient.emergency_contacts
            WHERE id = :id AND organization_id = :organization AND patient_id = :patient
            """,
            {"id": contact_id, "organization": patient.organization_id, "patient": patient.id},
        )
        if row is None:
            raise not_found("emergency contact")
        return row

    async def update_emergency_contact(
        self, caller: Caller, patient_id: UUID, contact_id: UUID, request: EmergencyContactRequest
    ) -> EmergencyContact:
        """Update an emergency contact."""
        _ensure_can_write(caller, patient_id)
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            patient = await readable(tx, caller, patient_id)
            current = await self._find_contact(tx, patient, contact_id)
            contact = _contact_from(current.id, request)
            await self._save_contact(tx, patient.organization_id, patient.id, contact, current.is_primary, False, now)
            await audit(tx, caller, now, AuditEntry("emergency_contact.updated", "patient", patient.id))
        return contact

    async def delete_emergency_contact(self, caller: Caller, patient_id: UUID, contact_id: UUID) -> None:
        """Remove an emergency contact."""
        _ensure_can_write(caller, patient_id)
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            patient = await readable(tx, caller, patient_id)
            await self._find_contact(tx, patient, contact_id)
            await tx.execute("DELETE FROM patient.emergency_contacts WHERE id = :id", {"id": contact_id})
            await audit(tx, caller, now, AuditEntry("emergency_contact.deleted", "patient", patient.id))

    async def list_consents(self, caller: Caller, patient_id: UUID) -> list[Consent]:
        """List a patient's consent history, most recent first."""
        async with self._db.transaction(caller.scope()) as tx:
            patient = await readable(tx, caller, patient_id)
            rows = await tx.fetch(
                f"""
                SELECT {_CONSENT_COLUMNS} FROM patient.patient_consents
                WHERE organization_id = :organization AND patient_id = :patient ORDER BY granted_at DESC
                """,
                {"organization": patient.organization_id, "patient": patient.id},
            )
        return [_consent_view(row) for row in rows]

    async def record_consent(self, caller: Caller, patient_id: UUID, request: ConsentRequest) -> Consent:
        """Record a consent; an active consent of the same type must be revoked first."""
        _ensure_can_write(caller, patient_id)
        recorded_by = caller.require_user_id()
        now = self._clock()
        consent = Consent.model_construct(
            id=new_id(),
            type=request.type,
            status=ConsentStatus.GRANTED,
            method=request.method,
            document_version=trimmed(request.document_version),
            granted_at=now,
        )
        async with self._db.transaction(caller.scope()) as tx:
            patient = await readable(tx, caller, patient_id)
            active = await tx.fetchval(
                """
                SELECT EXISTS (SELECT 1 FROM patient.patient_consents
                    WHERE organization_id = :organization AND patient_id = :patient AND type = :type
                      AND status = 'granted')
                """,
                {"organization": patient.organization_id, "patient": patient.id, "type": request.type.value},
            )
            if active:
                raise conflict("consent_already_granted", "An active consent of this type is already on file.")
            await tx.execute(
                """
                INSERT INTO patient.patient_consents (id, organization_id, patient_id, type, status, method,
                    document_version, granted_at, recorded_by_user_id, created_at)
                VALUES (:id, :organization, :patient, :type, 'granted', :method, :document, :now, :user, :now)
                """,
                {
                    "id": consent.id,
                    "organization": patient.organization_id,
                    "patient": patient.id,
                    "type": request.type.value,
                    "method": request.method.value,
                    "document": consent.document_version,
                    "now": now,
                    "user": recorded_by,
                },
            )
            await audit(
                tx,
                caller,
                now,
                AuditEntry(
                    "consent.granted",
                    "patient_consent",
                    consent.id,
                    metadata={"patientId": str(patient_id), "type": request.type.value},
                ),
            )
        return consent

    async def revoke_consent(self, caller: Caller, patient_id: UUID, consent_id: UUID) -> Consent:
        """Revoke a consent; the original record is kept for the audit trail."""
        _ensure_can_write(caller, patient_id)
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            patient = await readable(tx, caller, patient_id)
            row = await tx.fetchrow(
                f"""
                SELECT {_CONSENT_COLUMNS} FROM patient.patient_consents
                WHERE id = :id AND organization_id = :organization AND patient_id = :patient
                """,
                {"id": consent_id, "organization": patient.organization_id, "patient": patient.id},
            )
            if row is None:
                raise not_found("consent")
            revoked_by = caller.require_user_id()
            consent = _consent_view(row)
            try:
                ensure_revocable(consent.status)
            except DomainError as error:
                raise from_domain(error) from error
            await tx.execute(
                """
                UPDATE patient.patient_consents SET status = 'revoked', revoked_at = :now, revoked_by_user_id = :user
                WHERE id = :id
                """,
                {"id": consent.id, "now": now, "user": revoked_by},
            )
            await audit(
                tx,
                caller,
                now,
                AuditEntry(
                    "consent.revoked",
                    "patient_consent",
                    consent.id,
                    metadata={"patientId": str(patient_id), "type": consent.type.value},
                ),
            )
        return consent.model_copy(update={"status": ConsentStatus.REVOKED, "revoked_at": now})

    async def list_care_relationships(self, caller: Caller, patient_id: UUID) -> list[CareRelationship]:
        """List the providers with a care relationship to the patient, active ones first."""
        async with self._db.transaction(caller.scope()) as tx:
            patient = await readable(tx, caller, patient_id)
            rows = await tx.fetch(
                """
                SELECT id, provider_id, type, started_on, ended_on FROM patient.patient_provider_relationships
                WHERE organization_id = :organization AND patient_id = :patient
                ORDER BY (ended_on IS NOT NULL), started_on DESC
                """,
                {"organization": patient.organization_id, "patient": patient.id},
            )
        return [
            CareRelationship.model_construct(
                id=row.id,
                provider_id=row.provider_id,
                type=CareRelationshipType(row.type),
                started_on=row.started_on,
                ended_on=row.ended_on,
            )
            for row in rows
        ]

    async def add_care_relationship(
        self, caller: Caller, patient_id: UUID, request: CareRelationshipRequest
    ) -> CareRelationship:
        """Assign a provider to the patient's care, which grants that provider access."""
        caller.require(Permission.PATIENTS_WRITE)
        now = self._clock()
        relationship = CareRelationship.model_construct(
            id=new_id(), provider_id=request.provider_id, type=request.type, started_on=now.astimezone(UTC).date()
        )
        async with self._db.transaction(caller.scope()) as tx:
            patient = await readable(tx, caller, patient_id)
            provider_exists = await tx.fetchval(
                "SELECT EXISTS (SELECT 1 FROM provider.providers WHERE id = :id AND organization_id = :organization)",
                {"id": request.provider_id, "organization": patient.organization_id},
            )
            if not provider_exists:
                raise field_error("providerId", "The provider does not exist in this organization.")
            exists = await tx.fetchval(
                """
                SELECT EXISTS (SELECT 1 FROM patient.patient_provider_relationships
                    WHERE organization_id = :organization AND patient_id = :patient AND provider_id = :provider
                      AND type = :type AND ended_on IS NULL)
                """,
                {
                    "organization": patient.organization_id,
                    "patient": patient.id,
                    "provider": request.provider_id,
                    "type": request.type.value,
                },
            )
            if exists:
                raise conflict("relationship_exists", "This care relationship already exists.")
            await tx.execute(
                """
                INSERT INTO patient.patient_provider_relationships (id, organization_id, patient_id, provider_id, type,
                    started_on, created_at)
                VALUES (:id, :organization, :patient, :provider, :type, :started, :now)
                """,
                {
                    "id": relationship.id,
                    "organization": patient.organization_id,
                    "patient": patient.id,
                    "provider": request.provider_id,
                    "type": request.type.value,
                    "started": relationship.started_on,
                    "now": now,
                },
            )
            await audit(
                tx,
                caller,
                now,
                AuditEntry(
                    "care_relationship.created",
                    "patient",
                    patient.id,
                    metadata={"providerId": str(request.provider_id), "type": request.type.value},
                ),
            )
        return relationship

    async def end_care_relationship(self, caller: Caller, patient_id: UUID, relationship_id: UUID) -> None:
        """End a care relationship; the provider keeps access only through past appointments."""
        caller.require(Permission.PATIENTS_WRITE)
        now = self._clock()
        async with self._db.transaction(caller.scope()) as tx:
            provider_id = await tx.fetchval(
                """
                SELECT provider_id FROM patient.patient_provider_relationships
                WHERE id = :id AND patient_id = :patient AND organization_id = :organization
                """,
                {"id": relationship_id, "patient": patient_id, "organization": caller.organization_id},
            )
            if provider_id is None:
                raise not_found("care relationship")
            await tx.execute(
                "UPDATE patient.patient_provider_relationships SET ended_on = coalesce(ended_on, :today) WHERE id = :id",
                {"id": relationship_id, "today": now.astimezone(UTC).date()},
            )
            await audit(
                tx,
                caller,
                now,
                AuditEntry("care_relationship.ended", "patient", patient_id, metadata={"providerId": str(provider_id)}),
            )
