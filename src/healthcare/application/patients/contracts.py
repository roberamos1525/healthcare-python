"""Request and response models of the patient endpoints."""

from datetime import date
from uuid import UUID

from healthcare.application.core.models import ApiModel, RequestModel, UtcDateTime
from healthcare.application.core.validation import (
    PHONE_MESSAGE,
    PHONE_PATTERN,
    STATE_MESSAGE,
    ZIP_MESSAGE,
    ZIP_PATTERN,
    Validator,
    is_us_state,
)
from healthcare.domain.calendar import add_years
from healthcare.domain.enums import (
    ActiveStatus,
    AdministrativeGender,
    CareRelationshipType,
    ConsentMethod,
    ConsentStatus,
    ConsentType,
)


class Address(RequestModel):
    """A postal address; every part is optional."""

    line1: str | None = None
    line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None

    def check_address(self, v: Validator) -> None:
        """Apply the address rules under the ``address.`` prefix."""
        v.text("address.line1", self.line1).max_length(200)
        v.text("address.line2", self.line2).max_length(200)
        v.text("address.city", self.city).max_length(100)
        v.ensure("address.state", self.state is None or is_us_state(self.state), STATE_MESSAGE)
        v.text("address.postalCode", self.postal_code).matches(ZIP_PATTERN, ZIP_MESSAGE)


def _valid_date_of_birth(v: Validator, value: date, today: date) -> None:
    """Require a date of birth that is not in the future and not more than 130 years ago."""
    v.ensure("dateOfBirth", value <= today and value >= add_years(today, -130), "Enter a valid date of birth.")


class PatientRequest(RequestModel):
    """Body of POST /patients and PUT /patients/{patientId}."""

    first_name: str
    middle_name: str | None = None
    last_name: str
    preferred_name: str | None = None
    date_of_birth: date
    administrative_gender: AdministrativeGender
    email: str | None = None
    phone: str | None = None
    address: Address | None = None
    preferred_language: str | None = None
    status: ActiveStatus | None = None
    version: int | None = None

    def check(self, v: Validator, today: date) -> None:
        """Apply the demographic rules."""
        v.text("firstName", self.first_name).not_empty().max_length(100)
        v.text("middleName", self.middle_name).max_length(100)
        v.text("lastName", self.last_name).not_empty().max_length(100)
        v.text("preferredName", self.preferred_name).max_length(100)
        _valid_date_of_birth(v, self.date_of_birth, today)
        v.text("email", self.email).optional_email()
        v.text("phone", self.phone).phone()
        v.text("preferredLanguage", self.preferred_language).max_length(35)
        if self.address is not None:
            self.address.check_address(v)


class SelfRegistrationRequest(RequestModel):
    """Body of POST /patients/self-registration."""

    date_of_birth: date
    administrative_gender: AdministrativeGender
    phone: str | None = None
    address: Address | None = None
    preferred_language: str | None = None

    def check(self, v: Validator, today: date) -> None:
        """Apply the self-registration rules."""
        _valid_date_of_birth(v, self.date_of_birth, today)
        v.text("phone", self.phone).phone()
        v.text("preferredLanguage", self.preferred_language).max_length(35)
        if self.address is not None:
            self.address.check_address(v)


class EmergencyContactRequest(RequestModel):
    """Body of the emergency contact endpoints."""

    name: str
    relationship: str
    phone: str
    email: str | None = None
    is_primary: bool

    def check(self, v: Validator, today: date) -> None:
        """Apply the contact rules."""
        v.text("name", self.name).not_empty().max_length(200)
        v.text("relationship", self.relationship).not_empty().max_length(50)
        v.text("phone", self.phone).not_empty().matches(PHONE_PATTERN, PHONE_MESSAGE)
        v.text("email", self.email).optional_email()


class ConsentRequest(RequestModel):
    """Body of POST /patients/{patientId}/consents."""

    type: ConsentType
    method: ConsentMethod
    document_version: str | None = None

    def check(self, v: Validator, today: date) -> None:
        """Apply the consent rules."""
        v.text("documentVersion", self.document_version).max_length(50)


class CareRelationshipRequest(RequestModel):
    """Body of POST /patients/{patientId}/care-relationships."""

    provider_id: UUID
    type: CareRelationshipType

    def check(self, v: Validator, today: date) -> None:
        """Apply the relationship rules."""
        v.not_empty_id("providerId", self.provider_id)


class AddressView(ApiModel):
    """A postal address as returned by the API."""

    line1: str | None = None
    line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None


class Patient(ApiModel):
    """Full API representation of a patient."""

    id: UUID
    medical_record_number: str
    first_name: str
    middle_name: str | None = None
    last_name: str
    preferred_name: str | None = None
    date_of_birth: date
    administrative_gender: AdministrativeGender
    email: str | None = None
    phone: str | None = None
    address: AddressView | None = None
    preferred_language: str | None = None
    status: ActiveStatus
    version: int
    created_at: UtcDateTime
    updated_at: UtcDateTime


class PatientSummary(ApiModel):
    """List representation of a patient."""

    id: UUID
    medical_record_number: str
    first_name: str
    last_name: str
    date_of_birth: date
    phone: str | None = None
    status: ActiveStatus


class EmergencyContact(ApiModel):
    """API representation of an emergency contact."""

    id: UUID
    name: str
    relationship: str
    phone: str
    email: str | None = None
    is_primary: bool


class Consent(ApiModel):
    """API representation of a consent record."""

    id: UUID
    type: ConsentType
    status: ConsentStatus
    method: ConsentMethod
    document_version: str | None = None
    granted_at: UtcDateTime
    revoked_at: UtcDateTime | None = None


class CareRelationship(ApiModel):
    """API representation of a care relationship."""

    id: UUID
    provider_id: UUID
    type: CareRelationshipType
    started_on: date
    ended_on: date | None = None
