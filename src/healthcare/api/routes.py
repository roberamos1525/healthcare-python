"""The operations of the API under /api/v1, declared in the reference's controller order."""

import re
from typing import Final
from uuid import UUID

from starlette.responses import Response

from healthcare.api.responses import json_response, no_content
from healthcare.api.routing import Ctx, Router
from healthcare.application.core.errors import bad_request
from healthcare.application.identity.contracts import (
    ChangePasswordRequest,
    ConfirmTotpRequest,
    DisableTotpRequest,
    EmailRequest,
    LoginRequest,
    MessageResponse,
    MfaChallengeRequest,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    UpdateProfileRequest,
    VerifyEmailRequest,
)
from healthcare.application.organizations.contracts import (
    AcceptInvitationRequest,
    ClinicRequest,
    CreateInvitationRequest,
    CreateOrganizationRequest,
    HolidayRequest,
    LocationRequest,
    UpdateMemberRequest,
    UpdateOrganizationRequest,
    UpdateSettingsRequest,
)
from healthcare.application.patients.contracts import (
    CareRelationshipRequest,
    ConsentRequest,
    EmergencyContactRequest,
    PatientRequest,
    SelfRegistrationRequest,
)
from healthcare.application.platform import AuditFilter
from healthcare.application.providers.contracts import (
    CredentialRequest,
    CredentialReviewRequest,
    ProviderRequest,
    ScheduleExceptionRequest,
    ScheduleRequest,
)
from healthcare.application.scheduling.contracts import (
    AppointmentFilter,
    BookRequest,
    CancelRequest,
    RescheduleRequest,
    UpdateAppointmentRequest,
    WaitingListRequest,
)
from healthcare.domain.calendar import MIN_DATE
from healthcare.domain.enums import (
    ActiveStatus,
    AppointmentStatus,
    OrganizationStatus,
    ProviderStatus,
    WaitingListStatus,
)
from healthcare.infrastructure.bootstrap import Services

CHECK_INBOX: Final = "If the address can be used, an email with further instructions has been sent."
_IDEMPOTENCY_KEY: Final = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_NIL: Final = UUID(int=0)


def _location(ctx: Ctx, path: str) -> dict[str, str]:
    """Return the Location header of a created resource."""
    request = ctx.request
    scheme = request.url.scheme
    if ctx.forwarded_headers:
        proto = request.headers.get("x-forwarded-proto")
        if proto:
            scheme = proto.split(",")[0].strip().lower()
    return {"location": f"{scheme}://{request.headers.get('host', request.url.netloc)}{path}"}


def register_auth_routes(api: Router, s: Services) -> None:
    """Declare the account, session, MFA and user administration endpoints."""
    auth = s.identity

    @api.route("POST", "/api/v1/auth/register", anonymous=True, auth_limit=True)
    async def register(ctx: Ctx) -> Response:
        """Register an account."""
        await auth.register(ctx.caller, await ctx.body(RegisterRequest))
        return json_response(MessageResponse(message=CHECK_INBOX), 202)

    @api.route("POST", "/api/v1/auth/verify-email", anonymous=True, auth_limit=True)
    async def verify_email(ctx: Ctx) -> Response:
        """Verify an email address."""
        await auth.verify_email(ctx.caller, await ctx.body(VerifyEmailRequest))
        return no_content()

    @api.route("POST", "/api/v1/auth/resend-verification", anonymous=True, auth_limit=True)
    async def resend_verification(ctx: Ctx) -> Response:
        """Resend the verification email."""
        await auth.resend_verification(ctx.caller, await ctx.body(EmailRequest))
        return json_response(MessageResponse(message=CHECK_INBOX), 202)

    @api.route("POST", "/api/v1/auth/login", anonymous=True, auth_limit=True)
    async def login(ctx: Ctx) -> Response:
        """Sign in."""
        return json_response(await auth.login(ctx.caller, await ctx.body(LoginRequest)))

    @api.route("POST", "/api/v1/auth/mfa/challenge", anonymous=True, auth_limit=True)
    async def mfa_challenge(ctx: Ctx) -> Response:
        """Complete an MFA sign-in."""
        return json_response(await auth.complete_mfa_challenge(ctx.caller, await ctx.body(MfaChallengeRequest)))

    @api.route("POST", "/api/v1/auth/refresh", anonymous=True, auth_limit=True)
    async def refresh(ctx: Ctx) -> Response:
        """Rotate a refresh token."""
        return json_response(await auth.refresh(ctx.caller, await ctx.body(RefreshRequest)))

    @api.route("POST", "/api/v1/auth/logout")
    async def logout(ctx: Ctx) -> Response:
        """End the current session."""
        await auth.logout(ctx.caller)
        return no_content()

    @api.route("GET", "/api/v1/auth/me")
    async def me(ctx: Ctx) -> Response:
        """Return the signed-in user."""
        return json_response(await auth.current_user(ctx.caller))

    @api.route("PUT", "/api/v1/auth/me")
    async def update_me(ctx: Ctx) -> Response:
        """Update the signed-in user's profile."""
        return json_response(await auth.update_profile(ctx.caller, await ctx.body(UpdateProfileRequest)))

    @api.route("POST", "/api/v1/auth/password/forgot", anonymous=True, auth_limit=True)
    async def forgot_password(ctx: Ctx) -> Response:
        """Request a password reset email."""
        await auth.forgot_password(ctx.caller, await ctx.body(EmailRequest))
        return json_response(MessageResponse(message=CHECK_INBOX), 202)

    @api.route("POST", "/api/v1/auth/password/reset", anonymous=True, auth_limit=True)
    async def reset_password(ctx: Ctx) -> Response:
        """Reset the password with an emailed token."""
        await auth.reset_password(ctx.caller, await ctx.body(ResetPasswordRequest))
        return no_content()

    @api.route("POST", "/api/v1/auth/password/change")
    async def change_password(ctx: Ctx) -> Response:
        """Change the password."""
        await auth.change_password(ctx.caller, await ctx.body(ChangePasswordRequest))
        return no_content()

    @api.route("GET", "/api/v1/auth/sessions")
    async def sessions(ctx: Ctx) -> Response:
        """List active sessions."""
        return json_response(await auth.list_sessions(ctx.caller))

    @api.route("DELETE", "/api/v1/auth/sessions/{sessionId}")
    async def revoke_session(ctx: Ctx) -> Response:
        """Revoke a session."""
        await auth.revoke_session(ctx.caller, ctx.id("sessionId"))
        return no_content()

    @api.route("POST", "/api/v1/auth/mfa/totp/enroll")
    async def enroll_totp(ctx: Ctx) -> Response:
        """Start TOTP enrollment."""
        return json_response(await auth.enroll_totp(ctx.caller))

    @api.route("POST", "/api/v1/auth/mfa/totp/confirm")
    async def confirm_totp(ctx: Ctx) -> Response:
        """Confirm TOTP enrollment."""
        return json_response(await auth.confirm_totp(ctx.caller, await ctx.body(ConfirmTotpRequest)))

    @api.route("POST", "/api/v1/auth/mfa/totp/disable")
    async def disable_totp(ctx: Ctx) -> Response:
        """Disable TOTP."""
        await auth.disable_totp(ctx.caller, await ctx.body(DisableTotpRequest))
        return no_content()

    @api.route("POST", "/api/v1/users/{userId}/suspend")
    async def suspend_user(ctx: Ctx) -> Response:
        """Suspend an account."""
        await auth.suspend_user(ctx.caller, ctx.id("userId"))
        return no_content()

    @api.route("POST", "/api/v1/users/{userId}/reinstate")
    async def reinstate_user(ctx: Ctx) -> Response:
        """Reinstate an account."""
        await auth.reinstate_user(ctx.caller, ctx.id("userId"))
        return no_content()


def register_organization_routes(api: Router, s: Services) -> None:
    """Declare organizations, settings, members and invitations."""
    orgs = s.organizations

    @api.route("GET", "/api/v1/organizations")
    async def list_organizations(ctx: Ctx) -> Response:
        """List organizations."""
        q = ctx.query()
        include_all, search, page = q.flag("all"), q.text("search"), q.page()
        q.finish()
        return json_response(await orgs.list_organizations(ctx.caller, include_all, search, page))

    @api.route("POST", "/api/v1/organizations")
    async def create_organization(ctx: Ctx) -> Response:
        """Create an organization."""
        organization = await orgs.create(ctx.caller, await ctx.body(CreateOrganizationRequest))
        return json_response(organization, 201, _location(ctx, f"/api/v1/organizations/{organization.id}"))

    @api.route("GET", "/api/v1/organizations/{organizationId}")
    async def get_organization(ctx: Ctx) -> Response:
        """Return an organization."""
        return json_response(await orgs.get(ctx.caller, ctx.id("organizationId")))

    @api.route("PUT", "/api/v1/organizations/{organizationId}")
    async def update_organization(ctx: Ctx) -> Response:
        """Update an organization."""
        request = await ctx.body(UpdateOrganizationRequest)
        return json_response(await orgs.update(ctx.caller, ctx.id("organizationId"), request))

    @api.route("GET", "/api/v1/organizations/{organizationId}/settings")
    async def get_settings(ctx: Ctx) -> Response:
        """Return the booking policy."""
        return json_response(await orgs.get_settings(ctx.caller, ctx.id("organizationId")))

    @api.route("PUT", "/api/v1/organizations/{organizationId}/settings")
    async def update_settings(ctx: Ctx) -> Response:
        """Update the booking policy."""
        request = await ctx.body(UpdateSettingsRequest)
        return json_response(await orgs.update_settings(ctx.caller, ctx.id("organizationId"), request))

    @api.route("POST", "/api/v1/organizations/{organizationId}/suspend")
    async def suspend_organization(ctx: Ctx) -> Response:
        """Suspend an organization."""
        return json_response(await orgs.set_status(ctx.caller, ctx.id("organizationId"), OrganizationStatus.SUSPENDED))

    @api.route("POST", "/api/v1/organizations/{organizationId}/reinstate")
    async def reinstate_organization(ctx: Ctx) -> Response:
        """Reinstate an organization."""
        return json_response(await orgs.set_status(ctx.caller, ctx.id("organizationId"), OrganizationStatus.ACTIVE))

    @api.route("GET", "/api/v1/organizations/{organizationId}/members")
    async def list_members(ctx: Ctx) -> Response:
        """List members."""
        q = ctx.query()
        page = q.page()
        q.finish()
        return json_response(await orgs.list_members(ctx.caller, page))

    @api.route("PUT", "/api/v1/organizations/{organizationId}/members/{membershipId}")
    async def update_member(ctx: Ctx) -> Response:
        """Update a member."""
        request = await ctx.body(UpdateMemberRequest)
        return json_response(await orgs.update_member(ctx.caller, ctx.id("membershipId"), request))

    @api.route("DELETE", "/api/v1/organizations/{organizationId}/members/{membershipId}")
    async def remove_member(ctx: Ctx) -> Response:
        """Remove a member."""
        await orgs.remove_member(ctx.caller, ctx.id("membershipId"))
        return no_content()

    @api.route("GET", "/api/v1/organizations/{organizationId}/invitations")
    async def list_invitations(ctx: Ctx) -> Response:
        """List invitations."""
        q = ctx.query()
        page = q.page()
        q.finish()
        return json_response(await orgs.list_invitations(ctx.caller, page))

    @api.route("POST", "/api/v1/organizations/{organizationId}/invitations")
    async def invite(ctx: Ctx) -> Response:
        """Invite a person."""
        return json_response(await orgs.invite(ctx.caller, await ctx.body(CreateInvitationRequest)), 201)

    @api.route("DELETE", "/api/v1/organizations/{organizationId}/invitations/{invitationId}")
    async def revoke_invitation(ctx: Ctx) -> Response:
        """Revoke an invitation."""
        await orgs.revoke_invitation(ctx.caller, ctx.id("invitationId"))
        return no_content()

    @api.route("POST", "/api/v1/invitations/accept")
    async def accept_invitation(ctx: Ctx) -> Response:
        """Accept an invitation."""
        return json_response(await orgs.accept_invitation(ctx.caller, await ctx.body(AcceptInvitationRequest)))


def register_clinic_routes(api: Router, s: Services) -> None:
    """Declare clinics, locations and holidays."""
    clinics = s.clinics

    @api.route("GET", "/api/v1/clinics")
    async def list_clinics(ctx: Ctx) -> Response:
        """List clinics."""
        q = ctx.query()
        status, page = q.enum("status", ActiveStatus), q.page()
        q.finish()
        return json_response(await clinics.list_clinics(ctx.caller, status, page))

    @api.route("POST", "/api/v1/clinics")
    async def create_clinic(ctx: Ctx) -> Response:
        """Create a clinic."""
        clinic = await clinics.create_clinic(ctx.caller, await ctx.body(ClinicRequest))
        return json_response(clinic, 201, _location(ctx, f"/api/v1/clinics/{clinic.id}"))

    @api.route("GET", "/api/v1/clinics/{clinicId}")
    async def get_clinic(ctx: Ctx) -> Response:
        """Return a clinic."""
        return json_response(await clinics.get_clinic(ctx.caller, ctx.id("clinicId")))

    @api.route("PUT", "/api/v1/clinics/{clinicId}")
    async def update_clinic(ctx: Ctx) -> Response:
        """Update a clinic."""
        request = await ctx.body(ClinicRequest)
        return json_response(await clinics.update_clinic(ctx.caller, ctx.id("clinicId"), request))

    @api.route("GET", "/api/v1/clinics/{clinicId}/locations")
    async def list_clinic_locations(ctx: Ctx) -> Response:
        """List the locations of a clinic."""
        q = ctx.query()
        page = q.page()
        q.finish()
        return json_response(await clinics.list_locations(ctx.caller, ctx.id("clinicId"), page))

    @api.route("POST", "/api/v1/clinics/{clinicId}/locations")
    async def create_location(ctx: Ctx) -> Response:
        """Add a location."""
        request = await ctx.body(LocationRequest)
        location = await clinics.create_location(ctx.caller, ctx.id("clinicId"), request)
        return json_response(location, 201, _location(ctx, f"/api/v1/locations/{location.id}"))

    @api.route("GET", "/api/v1/locations")
    async def list_locations(ctx: Ctx) -> Response:
        """List every location of the organization."""
        q = ctx.query()
        page = q.page()
        q.finish()
        return json_response(await clinics.list_locations(ctx.caller, None, page))

    @api.route("GET", "/api/v1/locations/{locationId}")
    async def get_location(ctx: Ctx) -> Response:
        """Return a location."""
        return json_response(await clinics.get_location(ctx.caller, ctx.id("locationId")))

    @api.route("PUT", "/api/v1/locations/{locationId}")
    async def update_location(ctx: Ctx) -> Response:
        """Update a location."""
        request = await ctx.body(LocationRequest)
        return json_response(await clinics.update_location(ctx.caller, ctx.id("locationId"), request))

    @api.route("GET", "/api/v1/clinic-holidays")
    async def list_holidays(ctx: Ctx) -> Response:
        """List holidays."""
        q = ctx.query()
        date_from, date_to = q.date("from"), q.date("to")
        q.finish()
        return json_response(await clinics.list_holidays(ctx.caller, date_from, date_to))

    @api.route("POST", "/api/v1/clinic-holidays")
    async def create_holiday(ctx: Ctx) -> Response:
        """Add a holiday."""
        return json_response(await clinics.create_holiday(ctx.caller, await ctx.body(HolidayRequest)), 201)

    @api.route("DELETE", "/api/v1/clinic-holidays/{holidayId}")
    async def delete_holiday(ctx: Ctx) -> Response:
        """Remove a holiday."""
        await clinics.delete_holiday(ctx.caller, ctx.id("holidayId"))
        return no_content()


def register_provider_routes(api: Router, s: Services) -> None:
    """Declare providers, credentials, the directory, schedules, exceptions and availability."""
    providers, schedules = s.providers, s.schedules

    @api.route("GET", "/api/v1/providers")
    async def list_providers(ctx: Ctx) -> Response:
        """List providers."""
        q = ctx.query()
        search, specialty, location_id = q.text("search"), q.text("specialty"), q.guid("locationId")
        status, sort, page = q.enum("status", ProviderStatus), q.text("sort"), q.page()
        q.finish()
        return json_response(
            await providers.list_providers(ctx.caller, search, specialty, location_id, status, sort, page)
        )

    @api.route("POST", "/api/v1/providers")
    async def create_provider(ctx: Ctx) -> Response:
        """Create a provider."""
        provider = await providers.create(ctx.caller, await ctx.body(ProviderRequest))
        return json_response(provider, 201, _location(ctx, f"/api/v1/providers/{provider.id}"))

    @api.route("GET", "/api/v1/providers/{providerId}")
    async def get_provider(ctx: Ctx) -> Response:
        """Return a provider."""
        return json_response(await providers.get(ctx.caller, ctx.id("providerId")))

    @api.route("PUT", "/api/v1/providers/{providerId}")
    async def update_provider(ctx: Ctx) -> Response:
        """Update a provider."""
        request = await ctx.body(ProviderRequest)
        return json_response(await providers.update(ctx.caller, ctx.id("providerId"), request))

    @api.route("POST", "/api/v1/providers/{providerId}/activate")
    async def activate_provider(ctx: Ctx) -> Response:
        """Activate a provider."""
        return json_response(await providers.activate(ctx.caller, ctx.id("providerId")))

    @api.route("POST", "/api/v1/providers/{providerId}/deactivate")
    async def deactivate_provider(ctx: Ctx) -> Response:
        """Deactivate a provider."""
        return json_response(await providers.deactivate(ctx.caller, ctx.id("providerId")))

    @api.route("GET", "/api/v1/providers/{providerId}/credentials")
    async def list_credentials(ctx: Ctx) -> Response:
        """List credentials."""
        return json_response(await providers.list_credentials(ctx.caller, ctx.id("providerId")))

    @api.route("POST", "/api/v1/providers/{providerId}/credentials")
    async def add_credential(ctx: Ctx) -> Response:
        """Add a credential."""
        request = await ctx.body(CredentialRequest)
        return json_response(await providers.add_credential(ctx.caller, ctx.id("providerId"), request), 201)

    @api.route("POST", "/api/v1/providers/{providerId}/credentials/{credentialId}/review")
    async def review_credential(ctx: Ctx) -> Response:
        """Review a credential."""
        request = await ctx.body(CredentialReviewRequest)
        return json_response(
            await providers.review_credential(ctx.caller, ctx.id("providerId"), ctx.id("credentialId"), request)
        )

    @api.route("GET", "/api/v1/directory/providers")
    async def directory(ctx: Ctx) -> Response:
        """Search the provider directory."""
        q = ctx.query()
        search, specialty, city, state, page = (
            q.text("search"),
            q.text("specialty"),
            q.text("city"),
            q.text("state"),
            q.page(),
        )
        q.finish()
        return json_response(await providers.search_directory(ctx.caller, search, specialty, city, state, page))

    @api.route("GET", "/api/v1/provider-schedules")
    async def list_schedules(ctx: Ctx) -> Response:
        """List weekly schedules."""
        q = ctx.query()
        provider_id = q.guid("providerId")
        q.finish()
        return json_response(await schedules.list_schedules(ctx.caller, provider_id or _NIL))

    @api.route("POST", "/api/v1/provider-schedules")
    async def create_schedule(ctx: Ctx) -> Response:
        """Create a weekly schedule block."""
        return json_response(await schedules.create_schedule(ctx.caller, await ctx.body(ScheduleRequest)), 201)

    @api.route("PUT", "/api/v1/provider-schedules/{scheduleId}")
    async def update_schedule(ctx: Ctx) -> Response:
        """Update a weekly schedule block."""
        request = await ctx.body(ScheduleRequest)
        return json_response(await schedules.update_schedule(ctx.caller, ctx.id("scheduleId"), request))

    @api.route("DELETE", "/api/v1/provider-schedules/{scheduleId}")
    async def delete_schedule(ctx: Ctx) -> Response:
        """Remove a weekly schedule block."""
        await schedules.delete_schedule(ctx.caller, ctx.id("scheduleId"))
        return no_content()

    @api.route("GET", "/api/v1/schedule-exceptions")
    async def list_exceptions(ctx: Ctx) -> Response:
        """List schedule exceptions."""
        q = ctx.query()
        provider_id, starts, ends = q.guid("providerId"), q.instant("from"), q.instant("to")
        q.finish()
        return json_response(await schedules.list_exceptions(ctx.caller, provider_id or _NIL, starts, ends))

    @api.route("POST", "/api/v1/schedule-exceptions")
    async def create_exception(ctx: Ctx) -> Response:
        """Create a schedule exception."""
        request = await ctx.body(ScheduleExceptionRequest)
        return json_response(await schedules.create_exception(ctx.caller, request), 201)

    @api.route("DELETE", "/api/v1/schedule-exceptions/{exceptionId}")
    async def delete_exception(ctx: Ctx) -> Response:
        """Remove a schedule exception."""
        await schedules.delete_exception(ctx.caller, ctx.id("exceptionId"))
        return no_content()

    @api.route("GET", "/api/v1/availability")
    async def availability(ctx: Ctx) -> Response:
        """Return open slots."""
        q = ctx.query()
        provider_id, location_id, date_from, date_to = (
            q.guid("providerId"),
            q.guid("locationId"),
            q.date("from"),
            q.date("to"),
        )
        q.finish()
        return json_response(
            await s.availability.get_availability(
                ctx.caller, provider_id or _NIL, location_id, date_from or MIN_DATE, date_to or MIN_DATE
            )
        )


def register_patient_routes(api: Router, s: Services) -> None:
    """Declare patients, contacts, consents and care relationships."""
    patients = s.patients

    @api.route("GET", "/api/v1/patients")
    async def list_patients(ctx: Ctx) -> Response:
        """Search patients."""
        q = ctx.query()
        search, date_of_birth, status = q.text("search"), q.date("dateOfBirth"), q.enum("status", ActiveStatus)
        sort, page = q.text("sort"), q.page()
        q.finish()
        return json_response(await patients.list_patients(ctx.caller, search, date_of_birth, status, sort, page))

    @api.route("POST", "/api/v1/patients")
    async def create_patient(ctx: Ctx) -> Response:
        """Register a patient."""
        patient = await patients.create(ctx.caller, await ctx.body(PatientRequest))
        return json_response(patient, 201, _location(ctx, f"/api/v1/patients/{patient.id}"))

    @api.route("POST", "/api/v1/patients/self-registration")
    async def self_register(ctx: Ctx) -> Response:
        """Register the signed-in user as a patient."""
        patient = await patients.self_register(ctx.caller, await ctx.body(SelfRegistrationRequest))
        return json_response(patient, 201, _location(ctx, f"/api/v1/patients/{patient.id}"))

    @api.route("GET", "/api/v1/patients/{patientId}")
    async def get_patient(ctx: Ctx) -> Response:
        """Return a patient."""
        return json_response(await patients.get(ctx.caller, ctx.id("patientId")))

    @api.route("PUT", "/api/v1/patients/{patientId}")
    async def update_patient(ctx: Ctx) -> Response:
        """Update a patient."""
        request = await ctx.body(PatientRequest)
        return json_response(await patients.update(ctx.caller, ctx.id("patientId"), request))

    @api.route("GET", "/api/v1/patients/{patientId}/emergency-contacts")
    async def list_contacts(ctx: Ctx) -> Response:
        """List emergency contacts."""
        return json_response(await patients.list_emergency_contacts(ctx.caller, ctx.id("patientId")))

    @api.route("POST", "/api/v1/patients/{patientId}/emergency-contacts")
    async def add_contact(ctx: Ctx) -> Response:
        """Add an emergency contact."""
        request = await ctx.body(EmergencyContactRequest)
        return json_response(await patients.add_emergency_contact(ctx.caller, ctx.id("patientId"), request), 201)

    @api.route("PUT", "/api/v1/patients/{patientId}/emergency-contacts/{contactId}")
    async def update_contact(ctx: Ctx) -> Response:
        """Update an emergency contact."""
        request = await ctx.body(EmergencyContactRequest)
        return json_response(
            await patients.update_emergency_contact(ctx.caller, ctx.id("patientId"), ctx.id("contactId"), request)
        )

    @api.route("DELETE", "/api/v1/patients/{patientId}/emergency-contacts/{contactId}")
    async def delete_contact(ctx: Ctx) -> Response:
        """Remove an emergency contact."""
        await patients.delete_emergency_contact(ctx.caller, ctx.id("patientId"), ctx.id("contactId"))
        return no_content()

    @api.route("GET", "/api/v1/patients/{patientId}/consents")
    async def list_consents(ctx: Ctx) -> Response:
        """List consents."""
        return json_response(await patients.list_consents(ctx.caller, ctx.id("patientId")))

    @api.route("POST", "/api/v1/patients/{patientId}/consents")
    async def record_consent(ctx: Ctx) -> Response:
        """Record a consent."""
        request = await ctx.body(ConsentRequest)
        return json_response(await patients.record_consent(ctx.caller, ctx.id("patientId"), request), 201)

    @api.route("POST", "/api/v1/patients/{patientId}/consents/{consentId}/revoke")
    async def revoke_consent(ctx: Ctx) -> Response:
        """Revoke a consent."""
        return json_response(await patients.revoke_consent(ctx.caller, ctx.id("patientId"), ctx.id("consentId")))

    @api.route("GET", "/api/v1/patients/{patientId}/care-relationships")
    async def list_relationships(ctx: Ctx) -> Response:
        """List care relationships."""
        return json_response(await patients.list_care_relationships(ctx.caller, ctx.id("patientId")))

    @api.route("POST", "/api/v1/patients/{patientId}/care-relationships")
    async def add_relationship(ctx: Ctx) -> Response:
        """Add a care relationship."""
        request = await ctx.body(CareRelationshipRequest)
        return json_response(await patients.add_care_relationship(ctx.caller, ctx.id("patientId"), request), 201)

    @api.route("DELETE", "/api/v1/patients/{patientId}/care-relationships/{relationshipId}")
    async def end_relationship(ctx: Ctx) -> Response:
        """End a care relationship."""
        await patients.end_care_relationship(ctx.caller, ctx.id("patientId"), ctx.id("relationshipId"))
        return no_content()


def register_appointment_routes(api: Router, s: Services) -> None:
    """Declare appointments, the waiting list and the audit trail."""
    appointments = s.appointments

    @api.route("GET", "/api/v1/appointments")
    async def list_appointments(ctx: Ctx) -> Response:
        """List appointments."""
        q = ctx.query()
        filters = AppointmentFilter(
            provider_id=q.guid("providerId"),
            patient_id=q.guid("patientId"),
            location_id=q.guid("locationId"),
            status=q.enum("status", AppointmentStatus),
            starts_from=q.instant("from"),
            starts_to=q.instant("to"),
            sort=q.text("sort"),
        )
        page = q.page()
        q.finish()
        return json_response(await appointments.list_appointments(ctx.caller, filters, page))

    @api.route("POST", "/api/v1/appointments")
    async def book(ctx: Ctx) -> Response:
        """Book an appointment; the body is bound before the Idempotency-Key header is checked."""
        request = await ctx.body(BookRequest)
        key = ",".join(ctx.request.headers.getlist("idempotency-key"))
        if not key.strip() or not _IDEMPOTENCY_KEY.match(key):
            raise bad_request(
                "idempotency_key_required",
                "Send an Idempotency-Key header containing 8 to 128 letters, digits, hyphens or underscores.",
            )
        result = await appointments.book(ctx.caller, request, key)
        headers = {
            "idempotent-replayed": "true" if result.replayed else "false",
            **_location(ctx, f"/api/v1/appointments/{result.appointment.id}"),
        }
        return json_response(result.appointment, 201, headers)

    @api.route("GET", "/api/v1/appointments/{appointmentId}")
    async def get_appointment(ctx: Ctx) -> Response:
        """Return an appointment."""
        return json_response(await appointments.get(ctx.caller, ctx.id("appointmentId")))

    @api.route("PUT", "/api/v1/appointments/{appointmentId}")
    async def update_appointment(ctx: Ctx) -> Response:
        """Update an appointment."""
        request = await ctx.body(UpdateAppointmentRequest)
        return json_response(await appointments.update(ctx.caller, ctx.id("appointmentId"), request))

    @api.route("POST", "/api/v1/appointments/{appointmentId}/confirm")
    async def confirm(ctx: Ctx) -> Response:
        """Confirm an appointment."""
        return json_response(await appointments.confirm(ctx.caller, ctx.id("appointmentId")))

    @api.route("POST", "/api/v1/appointments/{appointmentId}/cancel")
    async def cancel(ctx: Ctx) -> Response:
        """Cancel an appointment."""
        request = await ctx.body(CancelRequest)
        return json_response(await appointments.cancel(ctx.caller, ctx.id("appointmentId"), request))

    @api.route("POST", "/api/v1/appointments/{appointmentId}/reschedule")
    async def reschedule(ctx: Ctx) -> Response:
        """Reschedule an appointment."""
        request = await ctx.body(RescheduleRequest)
        return json_response(await appointments.reschedule(ctx.caller, ctx.id("appointmentId"), request))

    @api.route("POST", "/api/v1/appointments/{appointmentId}/check-in")
    async def check_in(ctx: Ctx) -> Response:
        """Check a patient in."""
        return json_response(await appointments.check_in(ctx.caller, ctx.id("appointmentId")))

    @api.route("POST", "/api/v1/appointments/{appointmentId}/start")
    async def start(ctx: Ctx) -> Response:
        """Start a visit."""
        return json_response(await appointments.start(ctx.caller, ctx.id("appointmentId")))

    @api.route("POST", "/api/v1/appointments/{appointmentId}/complete")
    async def complete(ctx: Ctx) -> Response:
        """Complete a visit."""
        return json_response(await appointments.complete(ctx.caller, ctx.id("appointmentId")))

    @api.route("POST", "/api/v1/appointments/{appointmentId}/no-show")
    async def no_show(ctx: Ctx) -> Response:
        """Mark a no-show."""
        return json_response(await appointments.mark_no_show(ctx.caller, ctx.id("appointmentId")))

    @api.route("GET", "/api/v1/appointments/{appointmentId}/history")
    async def history(ctx: Ctx) -> Response:
        """Return the history of an appointment."""
        return json_response(await appointments.history(ctx.caller, ctx.id("appointmentId")))

    @api.route("GET", "/api/v1/waiting-list")
    async def list_waiting(ctx: Ctx) -> Response:
        """List the waiting list."""
        q = ctx.query()
        status, provider_id, page = q.enum("status", WaitingListStatus), q.guid("providerId"), q.page()
        q.finish()
        return json_response(await s.waiting_list.list_entries(ctx.caller, status, provider_id, page))

    @api.route("POST", "/api/v1/waiting-list")
    async def add_waiting(ctx: Ctx) -> Response:
        """Add a waiting list entry."""
        return json_response(await s.waiting_list.create(ctx.caller, await ctx.body(WaitingListRequest)), 201)

    @api.route("DELETE", "/api/v1/waiting-list/{entryId}")
    async def cancel_waiting(ctx: Ctx) -> Response:
        """Cancel a waiting list entry."""
        await s.waiting_list.cancel(ctx.caller, ctx.id("entryId"))
        return no_content()

    @api.route("GET", "/api/v1/audit-events")
    async def audit_events(ctx: Ctx) -> Response:
        """List the audit trail."""
        q = ctx.query()
        filters = AuditFilter(
            action=q.text("action"),
            resource_type=q.text("resourceType"),
            resource_id=q.guid("resourceId"),
            actor_user_id=q.guid("actorUserId"),
            occurred_from=q.instant("from"),
            occurred_to=q.instant("to"),
        )
        page = q.page()
        q.finish()
        return json_response(await s.audit.list_events(ctx.caller, filters, page))


def register_routes(api: Router, services: Services) -> None:
    """Declare every operation of the API."""
    register_auth_routes(api, services)
    register_organization_routes(api, services)
    register_clinic_routes(api, services)
    register_provider_routes(api, services)
    register_patient_routes(api, services)
    register_appointment_routes(api, services)
