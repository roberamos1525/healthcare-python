# API Reference

The machine-readable contract is [openapi.yaml](openapi.yaml) (OpenAPI 3.1). It is **generated from
this implementation** and committed; the contract tests fail if the served document differs from the
committed file, and they validate real responses against its schemas. Every other backend (Java,
Rust, Go, Python) must expose the same operations, request and response shapes, status codes and
error codes. This page explains the conventions and lists the endpoints with the permission each one
requires.

A running service also serves the document at `GET /openapi/v1.yaml`.

---

## 1. Conventions

### 1.1 Base path, transport and authentication

- Base path: **`/api/v1`**. HTTPS outside local development.
- Authentication: `Authorization: Bearer <accessToken>` (RS256 JWT, 10 minutes). Obtain tokens from
  `POST /api/v1/auth/login` (and `/auth/mfa/challenge` when MFA is enabled); renew them with
  `POST /api/v1/auth/refresh`. Every endpoint requires a token unless listed as public below.
- A token whose session has been revoked is rejected with `401 session_expired` even before it
  expires. A stale token sent to a public endpoint is ignored.

### 1.2 Selecting the organization

Tenant-scoped endpoints require **`X-Organization-Id: <uuid>`**. Endpoints under
`/api/v1/organizations/{organizationId}` take the organization from the path. Account endpoints
(`/auth`, `/users`, `/invitations`, `/directory`) take no organization.

The server verifies the caller's relationship with the organization on every request. Missing header
→ `400 organization_required`; malformed → `400 invalid_organization_id`; no relationship →
`403 not_a_member` (or guest access when the organization allows self-scheduling); suspended
organization → `403 organization_suspended`.

### 1.3 JSON

| Topic | Rule |
|---|---|
| Property names | camelCase |
| Nulls | Omitted from responses |
| Enumerations | snake_case strings, e.g. `checked_in`, `in_person`, `medical_license`. Integers are rejected |
| Numbers | Strict: numbers must be JSON numbers, not strings |
| Timestamps | ISO 8601 instants; responses are UTC with `Z` (`2026-12-01T15:00:00Z`). Requests may use any offset and are normalized to UTC |
| Dates | `YYYY-MM-DD` (`dateOfBirth`, `effectiveFrom`, holiday `date`) |
| Local times | `HH:mm`, 24-hour (`startTime`, `endTime` of schedule blocks; interpreted in the location's time zone) |
| Identifiers | UUID strings |
| Body size | 1 MB maximum |

### 1.4 Pagination, sorting and filtering

List endpoints accept `page` (1-based, default 1) and `pageSize` (default 25, maximum 100; larger
values are clamped) and return:

```json
{ "items": [ ... ], "page": 1, "pageSize": 25, "totalItems": 134, "totalPages": 6 }
```

`sort` takes a comma-separated list of fields; a leading `-` means descending (`sort=-startsAt`).
Only documented fields are accepted (`400 validation_failed` otherwise):

| Endpoint | Sort fields (default first) |
|---|---|
| `GET /providers` | `lastName`, `createdAt` |
| `GET /patients` | `lastName`, `createdAt` |
| `GET /appointments` | `startsAt`, `createdAt` |

Text search (`search`, `specialty`, `city`) is case-insensitive; wildcard characters are matched
literally. Patient search matches a name prefix or the exact medical record number. Enum filters use
the snake_case value (`status=confirmed`).

### 1.5 Optimistic concurrency

Updates to organizations, clinics, locations, providers, patients and appointments must include the
`version` the client last read. If the record changed in the meantime the response is
`409 concurrency_conflict`; reload and retry. Successful updates return the incremented `version`.

### 1.6 Idempotency

`POST /api/v1/appointments` **requires** an `Idempotency-Key` header:

- 8–128 characters from `A–Z a–z 0–9 _ -` (otherwise `400 idempotency_key_required`). Use a new
  random value (for example a UUID) per booking attempt and reuse it for retries of that attempt.
- Keys are scoped to the organization, the user and the operation, and are remembered for 48 hours.
- Same key, same body → the original response is returned with `201` and
  **`Idempotent-Replayed: true`**; no new appointment is created. The first response carries
  `Idempotent-Replayed: false`.
- Same key, different body → `422 idempotency_key_reused`.
- If the original request is still running, the retry waits for it and then replays its result; if
  it cannot be replayed yet, `409 request_in_progress`.

### 1.7 Errors

Errors use RFC 9457 problem details with media type `application/problem+json`:

```json
{
  "type": "urn:healthcare:problem:validation_failed",
  "title": "The request is invalid.",
  "status": 400,
  "code": "validation_failed",
  "detail": "One or more fields are invalid.",
  "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
  "errors": { "email": ["'Email' is not a valid email address."], "address.state": ["Enter a two-letter US state code, for example TX."] }
}
```

Clients should branch on `code`, not on `detail`. Messages never contain patient data, SQL or stack
traces.

| Status | Meaning |
|---|---|
| 200 | Success with body |
| 201 | Created (with `Location` where a canonical URL exists) |
| 202 | Accepted; an email may have been sent (registration, recovery) |
| 204 | Success without body |
| 400 | Malformed request or validation failure |
| 401 | Not authenticated, invalid credentials or ended session (`WWW-Authenticate: Bearer`) |
| 403 | Authenticated but not permitted |
| 404 | Not found, or outside the caller's scope |
| 409 | Conflict with current state: slot taken, stale version, invalid transition, duplicate |
| 422 | Valid request that breaks a business rule |
| 429 | Rate limited (`Retry-After: 60`) |
| 500 | Unexpected error (`internal_error`) |

### 1.8 Rate limits

Per instance, one-minute fixed windows: 600 requests per user (per address when anonymous), and
20 per address for `register`, `verify-email`, `resend-verification`, `login`, `mfa/challenge`,
`refresh`, `password/forgot` and `password/reset`. Production deployments add global limits at the
WAF.

---

## 2. Endpoints

"Org" = requires `X-Organization-Id`. Permissions refer to the matrix in
[architecture.md](architecture.md#33-permission-matrix). "Own" means the caller is the provider
linked to the resource; "self" means the caller is the patient (portal user) linked to the record.

### 2.1 Authentication and account

| Method | Path | Access | Success |
|---|---|---|---|
| POST | `/auth/register` | Public | 202 |
| POST | `/auth/verify-email` | Public | 204 |
| POST | `/auth/resend-verification` | Public | 202 |
| POST | `/auth/login` | Public | 200 `LoginResponse` (tokens, or `mfaRequired` + `mfaToken`) |
| POST | `/auth/mfa/challenge` | Public (MFA token) | 200 `LoginResponse` |
| POST | `/auth/refresh` | Public (refresh token) | 200 `TokenResponse` |
| POST | `/auth/logout` | Signed in | 204 |
| GET | `/auth/me` | Signed in | 200 user, memberships with roles and permissions, patient profiles |
| PUT | `/auth/me` | Signed in | 200 |
| POST | `/auth/password/forgot` | Public | 202 |
| POST | `/auth/password/reset` | Public (reset token) | 204 |
| POST | `/auth/password/change` | Signed in | 204 |
| GET | `/auth/sessions` | Signed in | 200 |
| DELETE | `/auth/sessions/{sessionId}` | Signed in (own sessions) | 204 |
| POST | `/auth/mfa/totp/enroll` | Signed in | 200 secret and `otpauth://` URI |
| POST | `/auth/mfa/totp/confirm` | Signed in | 200 ten recovery codes (shown once) |
| POST | `/auth/mfa/totp/disable` | Signed in (password + code) | 204 |
| POST | `/users/{userId}/suspend` | `platform_admin` | 204 |
| POST | `/users/{userId}/reinstate` | `platform_admin` | 204 |

Passwords: 12–128 characters and different from the email address.

### 2.2 Organizations, members and invitations

| Method | Path | Access | Success |
|---|---|---|---|
| GET | `/organizations` | Signed in (own memberships); `?all=true` `platform_admin`; `?search=` | 200 page |
| POST | `/organizations` | Signed in; caller becomes owner | 201 |
| GET | `/organizations/{organizationId}` | `organization.read` or `platform_admin` | 200 |
| PUT | `/organizations/{organizationId}` | `organization.manage` | 200 |
| GET | `/organizations/{organizationId}/settings` | `organization.read` | 200 |
| PUT | `/organizations/{organizationId}/settings` | `organization.manage` | 200 |
| POST | `/organizations/{organizationId}/suspend` | `platform_admin` | 200 |
| POST | `/organizations/{organizationId}/reinstate` | `platform_admin` | 200 |
| GET | `/organizations/{organizationId}/members` | `members.read` | 200 page |
| PUT | `/organizations/{organizationId}/members/{membershipId}` | `members.manage`; owner role changes need an owner | 200 |
| DELETE | `/organizations/{organizationId}/members/{membershipId}` | `members.manage`; owners only by owners | 204 |
| GET | `/organizations/{organizationId}/invitations` | `members.read` | 200 page |
| POST | `/organizations/{organizationId}/invitations` | `members.manage` | 201 |
| DELETE | `/organizations/{organizationId}/invitations/{invitationId}` | `members.manage` | 204 |
| POST | `/invitations/accept` | Signed in with the invited, verified email | 200 |

Settings: `defaultAppointmentDurationMinutes` 5–480 in steps of 5; `minimumBookingNoticeMinutes`
0–10,080; `bookingHorizonDays` 1–365; `patientCancellationWindowHours` 0–168;
`reminderOffsetsMinutes` up to five distinct values between 15 and 20,160.

### 2.3 Clinics, locations and holidays (Org)

| Method | Path | Access | Success |
|---|---|---|---|
| GET | `/clinics?status=` | `clinics.read` | 200 page |
| POST | `/clinics` | `clinics.manage` | 201 |
| GET | `/clinics/{clinicId}` | `clinics.read` | 200 |
| PUT | `/clinics/{clinicId}` | `clinics.manage` (with `version`) | 200 |
| GET | `/clinics/{clinicId}/locations` | `clinics.read` | 200 page |
| POST | `/clinics/{clinicId}/locations` | `clinics.manage` | 201 |
| GET | `/locations` | `clinics.read` | 200 page |
| GET | `/locations/{locationId}` | `clinics.read` | 200 |
| PUT | `/locations/{locationId}` | `clinics.manage` (with `version`) | 200 |
| GET | `/clinic-holidays?from=&to=` | `clinics.read` | 200 list (max 500) |
| POST | `/clinic-holidays` | `clinics.manage` | 201 |
| DELETE | `/clinic-holidays/{holidayId}` | `clinics.manage` | 204 |

Locations require a US state code, a ZIP or ZIP+4 code and an IANA time zone.

### 2.4 Providers, schedules and availability

| Method | Path | Access | Success |
|---|---|---|---|
| GET | `/providers?search=&specialty=&locationId=&status=&sort=` (Org) | `providers.read` (patients and guests see active providers only) | 200 page |
| POST | `/providers` (Org) | `providers.manage` | 201 (status `onboarding`) |
| GET | `/providers/{providerId}` (Org) | `providers.read` | 200 |
| PUT | `/providers/{providerId}` (Org) | `providers.manage`, or own (`providers.manage_own`, cannot change `userId`) | 200 |
| POST | `/providers/{providerId}/activate` (Org) | `providers.manage`; needs a verified, unexpired medical license | 200 |
| POST | `/providers/{providerId}/deactivate` (Org) | `providers.manage` | 200 |
| GET | `/providers/{providerId}/credentials` (Org) | `credentials.verify`, `providers.manage` or own | 200 list |
| POST | `/providers/{providerId}/credentials` (Org) | `providers.manage` or own | 201 |
| POST | `/providers/{providerId}/credentials/{credentialId}/review` (Org) | `credentials.verify`; not on one's own profile | 200 |
| GET | `/directory/providers?search=&specialty=&city=&state=` | Signed in; no organization | 200 page of listed, active providers in organizations that allow self-scheduling |
| GET | `/provider-schedules?providerId=` (Org) | `schedules.read` | 200 list |
| POST | `/provider-schedules` (Org) | `schedules.manage` or own | 201 |
| PUT | `/provider-schedules/{scheduleId}` (Org) | `schedules.manage` or own | 200 |
| DELETE | `/provider-schedules/{scheduleId}` (Org) | `schedules.manage` or own | 204 |
| GET | `/schedule-exceptions?providerId=&from=&to=` (Org) | `schedules.read` | 200 list |
| POST | `/schedule-exceptions` (Org) | `schedules.manage` or own | 201 (time off reports `affectedAppointments`) |
| DELETE | `/schedule-exceptions/{exceptionId}` (Org) | `schedules.manage` or own | 204 |
| GET | `/availability?providerId=&locationId=&from=&to=` (Org) | `availability.read` | 200 open slots |

Availability: `from` and `to` are local dates, inclusive, at most 31 days apart. Each slot carries
`locationId`, `startsAt`, `endsAt` (UTC) and the location `timeZone`. Patients and guests see only
slots after the minimum booking notice; everyone is limited to the booking horizon.

### 2.5 Patients (Org)

| Method | Path | Access | Success |
|---|---|---|---|
| GET | `/patients?search=&dateOfBirth=&status=&sort=` | `patients.read` (all), `patients.read_assigned` (provider: care relationship or appointment), `patients.self` (own) | 200 page; the search is audited |
| POST | `/patients` | `patients.write` | 201 (medical record number assigned) |
| POST | `/patients/self-registration` | `patients.self_register` (guest of an organization that allows self-scheduling) | 201 |
| GET | `/patients/{patientId}` | Visible to caller; read is audited | 200 |
| PUT | `/patients/{patientId}` | `patients.write`, or self (contact details only) | 200 |
| GET | `/patients/{patientId}/emergency-contacts` | Visible | 200 list |
| POST | `/patients/{patientId}/emergency-contacts` | `patients.write` or self | 201 |
| PUT | `/patients/{patientId}/emergency-contacts/{contactId}` | `patients.write` or self | 200 |
| DELETE | `/patients/{patientId}/emergency-contacts/{contactId}` | `patients.write` or self | 204 |
| GET | `/patients/{patientId}/consents` | Visible | 200 list |
| POST | `/patients/{patientId}/consents` | `patients.write` or self | 201 |
| POST | `/patients/{patientId}/consents/{consentId}/revoke` | `patients.write` or self | 200 |
| GET | `/patients/{patientId}/care-relationships` | Visible | 200 list |
| POST | `/patients/{patientId}/care-relationships` | `patients.write` | 201 |
| DELETE | `/patients/{patientId}/care-relationships/{relationshipId}` | `patients.write` | 204 (ends the relationship) |

Patients outside the caller's scope return `404`.

### 2.6 Appointments and waiting list (Org)

| Method | Path | Access | Success |
|---|---|---|---|
| GET | `/appointments?providerId=&patientId=&locationId=&status=&from=&to=&sort=` | `appointments.read` (all), `appointments.read_own` (own calendar), `appointments.self` (own) | 200 page |
| POST | `/appointments` + `Idempotency-Key` | `appointments.manage`, `appointments.manage_own` for own calendar, or `appointments.self` for own patient record | 201 (`confirmed` for staff; `pending` for patients unless auto-confirm) |
| GET | `/appointments/{appointmentId}` | Visible | 200 |
| PUT | `/appointments/{appointmentId}` | Staff, own provider or self; `visitType`, `reason`, `version` | 200 |
| POST | `/appointments/{appointmentId}/confirm` | Staff or own provider | 200 |
| POST | `/appointments/{appointmentId}/cancel` | Staff, own provider, or self outside the cancellation window | 200 |
| POST | `/appointments/{appointmentId}/reschedule` | Staff, own provider, or self outside the cancellation window | 200 |
| POST | `/appointments/{appointmentId}/check-in` | Staff or own provider | 200 |
| POST | `/appointments/{appointmentId}/start` | `appointments.conduct` and staff or own provider | 200 |
| POST | `/appointments/{appointmentId}/complete` | `appointments.conduct` and staff or own provider | 200 |
| POST | `/appointments/{appointmentId}/no-show` | Staff or own provider; only after the start time | 200 |
| GET | `/appointments/{appointmentId}/history` | Visible | 200 list |
| GET | `/waiting-list?status=&providerId=` | `waiting_list.manage`, `appointments.read`, or self | 200 page |
| POST | `/waiting-list` | `waiting_list.manage` or self | 201 |
| DELETE | `/waiting-list/{entryId}` | `waiting_list.manage` or self | 204 |

"Staff" means `appointments.manage`. `startsAt` must match an open slot exactly; the end time comes
from the slot length. A booking may reference `waitingListEntryId` to fulfil that entry. Life cycle:
`pending → confirmed → checked_in → in_progress → completed`, with `cancelled` (from pending,
confirmed or checked in) and `no_show` (from confirmed).

### 2.7 Audit (Org)

| Method | Path | Access | Success |
|---|---|---|---|
| GET | `/audit-events?action=&resourceType=&resourceId=&actorUserId=&from=&to=` | `audit.read` | 200 page, newest first |

### 2.8 Operational endpoints (not under `/api/v1`, not in the contract)

| Path | Purpose |
|---|---|
| `GET /health/live` | Liveness |
| `GET /health/ready` | Readiness (database; cache reported as degraded when unavailable) |
| `GET /.well-known/jwks.json` | Public signing key |
| `GET /openapi/v1.yaml` | OpenAPI document |

---

## 3. Error codes

| Code | Status | Meaning |
|---|---|---|
| `validation_failed` | 400 | One or more fields invalid; see `errors` |
| `bad_request` | 400 | The body could not be read |
| `organization_required` | 400 | `X-Organization-Id` missing |
| `invalid_organization_id` | 400 | `X-Organization-Id` is not a UUID |
| `invalid_token` | 400 / 401 | 400: verification or reset link invalid or expired. 401: access token has unusable claims |
| `invalid_invitation` | 400 | Invitation token invalid, used, revoked or expired |
| `idempotency_key_required` | 400 | Missing or malformed `Idempotency-Key` |
| `unauthorized` | 401 | No valid access token |
| `session_expired` | 401 | The session behind the token has ended |
| `invalid_credentials` | 401 | Wrong email or password (same response for unknown accounts) |
| `account_locked` | 401 | Too many failed attempts; try again later |
| `invalid_mfa_token` | 401 | MFA challenge expired; sign in again |
| `invalid_mfa_code` | 401 | Wrong TOTP or recovery code |
| `invalid_refresh_token` | 401 | Refresh token unknown or already used (reuse revokes the session) |
| `forbidden` | 403 | Missing permission |
| `not_a_member` | 403 | No relationship with the selected organization |
| `organization_suspended` | 403 | The organization is suspended or closed |
| `email_not_verified` | 403 | Verify the email address before signing in |
| `account_suspended` | 403 | The account is suspended or deactivated |
| `owner_required` | 403 | Only an owner can change owner memberships |
| `invitation_email_mismatch` | 403 | Signed in with a different email than the invitation |
| `self_verification_not_allowed` | 403 | Reviewers cannot verify their own credentials |
| `not_found` | 404 | Resource missing or outside the caller's scope |
| `concurrency_conflict` | 409 | Stale `version` |
| `slot_unavailable` | 409 | The slot is taken, not open, or outside the allowed window |
| `schedule_busy` | 409 | The provider's calendar is locked by another request; retry |
| `request_in_progress` | 409 | The original idempotent request has not finished |
| `transient_conflict` | 409 | Serialization failure or deadlock; retry |
| `invalid_status_transition` | 409 | The appointment cannot move to that status |
| `appointment_not_reschedulable` | 409 | Only pending or confirmed appointments can move |
| `appointment_closed` | 409 | Completed, cancelled or no-show appointments cannot be edited |
| `schedule_overlap` | 409 | Working hours overlap an existing block |
| `already_member` | 409 | The person is already a member |
| `already_registered` | 409 | The user is already a patient of the organization |
| `already_linked` | 409 | The account is already linked to the patient |
| `email_in_use` | 409 | Account email already exists (concurrent registration) |
| `npi_in_use` | 409 | NPI already used in the organization |
| `user_already_provider` | 409 | The user is already linked to a provider profile |
| `duplicate_specialty` | 409 | Same specialty listed twice |
| `holiday_exists` | 409 | A holiday already exists for that date and scope |
| `duplicate_resource` | 409 | Another unique value conflict |
| `consent_already_granted` | 409 | An active consent of that type exists |
| `consent_already_revoked` | 409 | The consent is already revoked |
| `relationship_exists` | 409 | The care relationship already exists |
| `invitation_not_pending` | 409 | Only pending invitations can be revoked |
| `mfa_already_enabled` / `mfa_not_enabled` / `mfa_enrollment_not_started` | 409 | MFA state does not allow the action |
| `organization_status_unchanged` | 409 | The organization is already in that state |
| `user_not_suspended` | 409 | The account is not suspended |
| `waiting_list_entry_closed` | 409 | The entry is no longer active |
| `last_owner` | 422 | An organization must keep one active owner |
| `provider_not_verified` | 422 | A verified, unexpired medical license is required |
| `provider_not_at_location` | 422 | The provider is not assigned to that location |
| `user_not_provider_member` | 422 | The linked user must be a member with the provider role |
| `identity_fields_locked` | 422 | Patients cannot change name, date of birth or gender |
| `patient_inactive` | 422 | Cannot book for an inactive patient |
| `cancellation_window_passed` | 422 | Too close to the appointment for online changes |
| `appointment_not_started` | 422 | No-show before the start time |
| `idempotency_key_reused` | 422 | The key was used with a different request |
| `cannot_suspend_self` | 422 | Administrators cannot suspend their own account |
| `invalid_time_range` | 422 | End is not after start |
| `invalid_verification_decision` | 422 | Credential decision must be verified or rejected |
| `invalid_reference` | 422 | A referenced record does not exist or is in use |
| `constraint_violation` | 422 | A data consistency rule was violated |
| `rate_limited` | 429 | Too many requests |
| `internal_error` | 500 | Unexpected error |
