# Product Requirements Document

Healthcare Management SaaS Platform

| | |
|---|---|
| Document status | Baseline for Release 1 |
| Applies to | All backend implementations and the web frontend |
| Reference implementation | `HealthCare-C#` (.NET) |
| Market | United States, outpatient care |

This document defines what the product must do. How it is built is described in
[architecture.md](architecture.md), [database-schema.md](database-schema.md), [api.md](api.md) and
[openapi.yaml](openapi.yaml). Security and regulatory constraints are detailed in
[security-and-compliance.md](security-and-compliance.md).

---

## 1. Product overview

The platform is a business-to-business SaaS product with patient-facing services. Independent
healthcare providers and clinics use it to run their day-to-day operations: staff, locations,
provider schedules, patient registration and appointments. Patients use it to find a provider, book
and manage appointments, and, in later releases, exchange messages and documents with their care
team.

### 1.1 Goals

1. Give small and medium-sized practices one system for scheduling, patient administration and
   staff management, without requiring on-premises infrastructure.
2. Let patients book, reschedule and cancel appointments themselves, reducing front-desk workload.
3. Keep every organization's data isolated from every other organization's data.
4. Provide a foundation that can grow from an independent practice to a multi-location
   organization, and later to hospital-level capabilities, without a rewrite.

### 1.2 Target customers

| Segment | Typical size | Primary need |
|---|---|---|
| Independent doctors | 1 provider, 0–3 staff | Simple scheduling and patient list; the doctor is also the owner |
| Small and medium clinics | 2–30 providers | Shared front desk, multiple provider calendars, role separation |
| Healthcare practices | Several specialties | Provider onboarding, credential tracking, reporting |
| Multi-location organizations | 2+ locations, shared staff | Per-location time zones, holidays and schedules under one account |

### 1.3 Scope boundaries

The first releases target **outpatient clinics and independent practices**. The following are
explicitly out of scope and must not be implied by the product, its documentation or its user
interface:

- A complete hospital information system (inpatient admissions, bed management, operating theatre
  scheduling, pharmacy, laboratory information systems).
- Insurance claims processing, eligibility checks, clearinghouse integration and revenue-cycle
  management.
- E-prescribing and controlled-substance workflows.
- ONC Health IT certification or any other regulatory certification. The product makes no such
  claim.
- Any claim of HIPAA compliance. The product is designed with HIPAA requirements in mind; compliance
  is a property of an operated service and requires a formal assessment (see section 7).
- Generative or conversational features. The product is a conventional healthcare management
  system.

### 1.4 Programme structure

The product is implemented as independent projects that share one API contract and one database
design. Each project is built, tested and reviewed before the next begins.

| Order | Project | Stack |
|---|---|---|
| 1 | `HealthCare-C#` | ASP.NET Core / .NET (reference implementation) |
| 2 | `HealthCare-Java` | Java / Spring Boot |
| 3 | `HealthCare-Rust` | Rust / Axum |
| 4 | `HealthCareGo` | Go / Chi |
| 5 | Python backend | Added to the programme after the original brief; stack to be confirmed |
| 6 | `HealthCare-UI-React` | React / TypeScript |

The frontend selects a backend through a configurable base URL and must work unchanged against any
of them.

---

## 2. Users, roles and permissions

### 2.1 Personas

| Persona | Description | Main tasks |
|---|---|---|
| Patient | A member of the public receiving care | Register, find a provider, book and manage appointments, maintain their profile |
| Doctor / healthcare provider | A licensed clinician | Maintain a professional profile, set availability, see their schedule and their patients, document care |
| Receptionist / staff | Front-desk and administrative employees | Register patients, schedule and cancel appointments, check patients in |
| Clinic administrator | Runs clinic operations | Manage staff, providers, schedules, appointments and clinic reports |
| Organization owner | Account holder for the practice | Manage the organization, locations, invitations, settings and subscription |
| Platform administrator | Employee of the SaaS operator | Manage organizations and platform configuration, monitor system health |

### 2.2 Role model

- Roles are granted **per organization** through an organization membership. A membership may hold
  several roles at once; an independent doctor is typically both `owner` and `provider`.
- A user may hold memberships in several organizations and may also be a patient of any of them.
- Patients are not staff members. A patient's access comes from a link between their user account
  and a patient record in a specific organization.
- `platform_admin` is a platform-level role. It grants no implicit access to an organization's
  patient data. Support access to tenant data must be explicitly authorized and audited (later
  release).
- Permissions are enforced by the backend on every request. Frontend role checks are a usability
  aid only.

### 2.3 Permission matrix (Release 1)

| Capability | Owner | Clinic admin | Provider | Receptionist | Staff | Patient |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| Manage organization profile and booking settings | ✔ | – | – | – | – | – |
| View organization profile and settings | ✔ | ✔ | ✔ | ✔ | ✔ | – |
| Manage clinics, locations and holidays | ✔ | ✔ | – | – | – | – |
| Invite staff, change member roles | ✔ | ✔ (not owners) | – | – | – | – |
| Create and edit provider profiles | ✔ | ✔ | own profile | – | – | – |
| Verify provider credentials | ✔ | ✔ | – | – | – | – |
| Manage provider schedules and time off | ✔ | ✔ | own | – | – | – |
| View provider list and availability | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ (active providers) |
| Register and edit patients | ✔ | ✔ | – | ✔ | – | own contact details |
| View patient demographics | ✔ | ✔ | assigned patients | ✔ | ✔ | own record |
| Record and revoke patient consents, edit emergency contacts | ✔ | ✔ | – | ✔ | – | own |
| Assign providers to a patient's care | ✔ | ✔ | – | ✔ | – | – |
| Book, reschedule, cancel appointments | ✔ | ✔ | own calendar | ✔ | – | own appointments |
| Confirm, check in, mark no-show | ✔ | ✔ | own calendar | ✔ | – | – |
| Start and complete a visit | ✔ | ✔ | own calendar | – | – | – |
| View appointments | ✔ | ✔ | own calendar | ✔ | ✔ | own appointments |
| Manage the waiting list | ✔ | ✔ | – | ✔ | view only | own entries |
| Review the audit trail | ✔ | ✔ | – | – | – | – |

"Assigned patients" means patients with an active patient–provider relationship or an appointment
with that provider. This implements the minimum-necessary principle for clinicians. The
authoritative permission list is in [architecture.md](architecture.md#33-permission-matrix).

A verified user with no relationship to an organization that allows self-scheduling is treated as a
**guest** of it: they can view clinics, active providers and availability and register themselves
as a patient. If self-scheduling is disabled they have no access.

The platform administrator can list, suspend and reinstate organizations and user accounts, and read
an organization's profile. These actions are audited. The role grants no access to patient data.

---

## 3. Functional requirements

Each module lists its requirements (FR) and acceptance criteria (AC). The **Release** column in
section 8 states when each module is delivered.

### Module 1 — Authentication and user management

| ID | Requirement |
|---|---|
| FR-1.1 | A person can register an account with an email address and password. |
| FR-1.2 | The email address must be verified through a single-use, time-limited link before the account can sign in. |
| FR-1.3 | Users sign in with email and password and receive a short-lived access token and a rotating refresh token. |
| FR-1.4 | Users can sign out; signing out revokes the session immediately. |
| FR-1.5 | Users can recover a forgotten password through a single-use, time-limited link. A successful reset revokes all existing sessions. |
| FR-1.6 | Users can enable multi-factor authentication with a time-based one-time password (TOTP) application and receive ten single-use recovery codes. |
| FR-1.7 | Users can list their active sessions and revoke any of them. |
| FR-1.8 | Sessions expire after a period of inactivity (default 60 minutes) and after an absolute maximum lifetime (default 12 hours). |
| FR-1.9 | Users can view and update their profile (name, phone) and change their password. |
| FR-1.10 | Accounts are locked temporarily after repeated failed sign-in attempts (default 5 failures, 15 minutes). |
| FR-1.11 | A platform administrator can suspend and reinstate an account. Suspension revokes all sessions. |
| FR-1.12 | Authorization is role-based and scoped to an organization membership or patient link. |
| FR-1.13 | The identity model supports linking external OpenID Connect identities to a user (federation delivered in a later release). |

Acceptance criteria:

- AC-1.a Registration, forgotten-password and resend-verification responses do not reveal whether
  an email address is already registered.
- AC-1.b An unverified or suspended account cannot obtain tokens.
- AC-1.c Presenting a refresh token that has already been rotated revokes the whole session.
- AC-1.d With MFA enabled, a correct password alone never yields an access token.
- AC-1.e Passwords are never stored or logged in clear text; tokens are stored only as hashes.
- AC-1.f A request with a revoked session is rejected immediately after sign-out, revocation,
  password change or reset and suspension (at most 30 seconds later for other session changes), not
  at access-token expiry.

### Module 2 — Organization and clinic management

| ID | Requirement |
|---|---|
| FR-2.1 | A verified user can register an organization or an independent practice and becomes its owner. |
| FR-2.2 | An organization can have several clinics, and each clinic several locations. |
| FR-2.3 | Each location has a postal address and an IANA time zone used for scheduling and display. |
| FR-2.4 | Owners maintain the organization profile and settings; owners and clinic administrators maintain clinic and location information. |
| FR-2.5 | Owners and clinic administrators can invite staff by email with one or more roles. Invitations expire after 7 days and can be revoked. |
| FR-2.6 | An invited person accepts the invitation and becomes a member with the offered roles. |
| FR-2.7 | Member roles can be changed and memberships suspended or removed. An organization always keeps at least one active owner. |
| FR-2.8 | Organization settings control booking rules: default appointment length, minimum booking notice, booking horizon, cancellation window, reminder offsets, whether patients may self-schedule and whether patient bookings are confirmed automatically. |
| FR-2.9 | Clinic holidays can be defined for one location or for all locations. |
| FR-2.10 | Organizations subscribe to a plan (see Module 9). |

Acceptance criteria:

- AC-2.a Data created by one organization is never returned to a member of another organization,
  including by guessing identifiers.
- AC-2.b An invitation can be accepted once, only before it expires, and only by the invited email
  address.
- AC-2.c A clinic administrator cannot grant or remove the owner role.
- AC-2.d The last active owner cannot be removed or demoted.

### Module 3 — Doctor and provider management

| ID | Requirement |
|---|---|
| FR-3.1 | Administrators can create provider profiles and link them to a member's user account. |
| FR-3.2 | A profile holds name, professional title, National Provider Identifier (NPI), biography, specialties and whether the provider accepts new patients. |
| FR-3.3 | Qualifications and licenses are recorded as credentials with issuing authority, identifier, issue and expiry dates. |
| FR-3.4 | Credentials are verified or rejected by an authorized administrator; the decision is audited. |
| FR-3.5 | A provider can be activated for clinical work only when they hold a verified, unexpired license. |
| FR-3.6 | Providers are assigned to one or more practice locations. |
| FR-3.7 | Weekly working hours are defined per provider and location, with effective dates. |
| FR-3.8 | Time off (vacation, leave) and additional working hours are recorded as schedule exceptions. |
| FR-3.9 | Each provider has a default appointment duration that a schedule block may override. |
| FR-3.10 | Patients can search a directory of listed, active providers by name, specialty and location. |

Acceptance criteria:

- AC-3.a An NPI must be ten digits and pass the NPI check-digit validation.
- AC-3.b Appointments cannot be booked with a provider who is not active.
- AC-3.c Working-hour blocks for one provider cannot overlap on the same weekday and date range.
- AC-3.d The directory exposes only public profile information.

### Module 4 — Patient management

| ID | Requirement |
|---|---|
| FR-4.1 | Staff can register patients; patients can register themselves with an organization that allows self-scheduling. |
| FR-4.2 | A patient record holds legal name, preferred name, date of birth, administrative gender, contact information, address and preferred language. |
| FR-4.3 | Each patient receives a medical record number unique within the organization. |
| FR-4.4 | Emergency contacts can be added, changed and removed. |
| FR-4.5 | Patient–provider relationships (for example primary care) are recorded with start and end dates. |
| FR-4.6 | Consents (treatment, notice of privacy practices, telehealth, email and SMS communication, data sharing) are recorded with the method of capture and can be revoked; history is retained. |
| FR-4.7 | A patient's user account is linked to their patient record so they can use the patient portal. |
| FR-4.8 | Patient medical history and documents are provided by Modules 6 and 8. |

Acceptance criteria:

- AC-4.a Patient records are scoped to one organization. The same person registered with two
  organizations has two independent records.
- AC-4.b A provider without administrative roles can open only their assigned patients.
- AC-4.c A patient can open only their own record.
- AC-4.d Every read of a patient record by staff is written to the audit trail.
- AC-4.e Updates use optimistic concurrency; a stale update is rejected rather than silently
  overwriting newer data.

### Module 5 — Appointment management

| ID | Requirement |
|---|---|
| FR-5.1 | Bookable slots are generated from working hours, additional hours, time off, clinic holidays and existing appointments, in the location's time zone. |
| FR-5.2 | Staff can book an appointment for a patient; patients can book for themselves. |
| FR-5.3 | Appointments follow a defined life cycle: Pending → Confirmed → Checked in → In progress → Completed, with Cancelled and No show as terminal alternatives. |
| FR-5.4 | Appointments can be confirmed, cancelled (with a reason) and rescheduled. |
| FR-5.5 | Front-desk staff check patients in; providers start and complete visits. |
| FR-5.6 | Every status change is recorded with who made it and when. |
| FR-5.7 | Reminders are scheduled for each appointment according to the organization's reminder offsets. |
| FR-5.8 | A waiting list records patients who want an earlier or unavailable slot. |
| FR-5.9 | Appointment lists can be filtered by provider, patient, location, status and date range, sorted, and are paginated (at most 100 items per page). |
| FR-5.10 | Availability can be requested for up to 31 days at a time. |

Acceptance criteria:

- AC-5.a Two active appointments for the same provider can never overlap. This is guaranteed by
  the database, not only by application checks.
- AC-5.b When several clients request the same slot at the same time, exactly one succeeds and the
  others receive HTTP 409.
- AC-5.c Repeating a booking request with the same idempotency key returns the original
  appointment and creates nothing new. Reusing a key with a different request body is rejected.
- AC-5.d Slots respect daylight-saving transitions; a local time that does not exist is never
  offered.
- AC-5.e Invalid transitions (for example completing a cancelled appointment) are rejected.
- AC-5.f Patients cannot book inside the minimum-notice period or beyond the booking horizon, and
  cannot cancel inside the cancellation window without staff involvement.
- AC-5.g Cancelling an appointment cancels its pending reminders.

### Module 6 — Medical records

| ID | Requirement |
|---|---|
| FR-6.1 | Providers record encounters linked to an appointment or created ad hoc. |
| FR-6.2 | Clinical notes, diagnoses, allergies and medication history are recorded per patient. |
| FR-6.3 | Clinical records are versioned. A finalized note cannot be edited; corrections are made by an amendment that preserves the original. |
| FR-6.4 | Only authorized providers and the patient (for released content) can read clinical records. |
| FR-6.5 | The data model maps to HL7 FHIR R4 resources (Patient, Practitioner, Encounter, Condition, AllergyIntolerance, MedicationStatement, DocumentReference). |

Acceptance criteria: every create, read, amend and finalize action is audited; no user role can
delete or silently alter a finalized record.

### Module 7 — Patient communication

| ID | Requirement |
|---|---|
| FR-7.1 | Patients and providers exchange messages in secure threads inside the application. |
| FR-7.2 | Appointment notifications are delivered by email, SMS and in-app, according to the recipient's preferences and recorded consents. |
| FR-7.3 | Delivery status is tracked per channel, and failed deliveries are retried with back-off. |

Acceptance criteria: email and SMS contain no clinical detail — at most the organization name, date,
time and a link that requires sign-in.

### Module 8 — Document management

| ID | Requirement |
|---|---|
| FR-8.1 | Authorized users upload and download documents associated with a patient. |
| FR-8.2 | Documents have a category, an owner, access permissions and metadata. |
| FR-8.3 | File size and type are validated; files are scanned for malware before they become downloadable. |
| FR-8.4 | File content is stored in encrypted object storage, never in the relational database. |

Acceptance criteria: download links are short-lived and bound to an authorization check; a file that
has not passed scanning cannot be downloaded.

### Module 9 — Subscription and billing

| ID | Requirement |
|---|---|
| FR-9.1 | The operator defines subscription plans with limits (providers, locations). |
| FR-9.2 | Organizations subscribe, upgrade, downgrade and cancel. |
| FR-9.3 | Invoices, billing history and payment status are available to the owner. |
| FR-9.4 | Usage is tracked against plan limits. |
| FR-9.5 | Payments are handled by an external payment provider. Card details never reach the platform. |

### Module 10 — Reporting and analytics

| ID | Requirement |
|---|---|
| FR-10.1 | Organization dashboards show patient and provider totals, appointment volumes, completion, cancellation and no-show rates. |
| FR-10.2 | Clinic performance and provider utilization are reported per period. |
| FR-10.3 | The operator sees organization usage reports. |
| FR-10.4 | Reports read from pre-aggregated data or a read replica, not from the transactional write path. |

Acceptance criteria: reports contain aggregates only and respect organization boundaries.

---

## 4. Cross-cutting requirements

| ID | Requirement |
|---|---|
| CR-1 | **Audit trail.** Security-relevant actions and access to patient data are recorded in an append-only log with actor, action, resource, outcome and time. |
| CR-2 | **Idempotency.** Operations where a retry could create a duplicate accept an `Idempotency-Key` header. It is mandatory for appointment creation. |
| CR-3 | **Reliable events.** Domain events are written to a transactional outbox in the same transaction as the change and published asynchronously. |
| CR-4 | **Consistent API.** All endpoints live under `/api/v1`, use the same pagination, sorting, filtering and error format, and are described by one OpenAPI document. |
| CR-5 | **Time.** All timestamps are stored in UTC. Scheduling uses the location's IANA time zone. |
| CR-6 | **Identifiers.** All entities use UUID identifiers. |
| CR-7 | **Language.** All user-facing text is clear, professional US English. |

---

## 5. Non-functional requirements

### 5.1 Scalability

- Support 100,000+ registered users, thousands of providers and many independent organizations.
- The application tier is stateless and scales horizontally behind a load balancer.
- Work that does not need to block a request (email, reminders, event publication) runs
  asynchronously.
- The data tier supports read replicas for reporting and read-heavy queries.

The sizing assumptions are documented in [capacity-model.md](capacity-model.md).

### 5.2 Performance targets

These are engineering targets to design and test against. They are not measured results.

| Target | Value |
|---|---|
| Common API reads | p95 under 300 ms |
| Appointment booking | p95 under 500 ms, excluding external dependencies |
| API availability | 99.9 % per month |
| Conflicting confirmed appointments | Zero |

### 5.3 Reliability

- Consistent exception handling; errors never expose internal details.
- Retries with back-off for transient failures; idempotent operations.
- Database transactions around every multi-step change.
- Liveness and readiness health checks.
- Graceful shutdown: in-flight requests complete and background workers stop cleanly.
- Background workers recover unfinished work after a restart.
- Documented backup and restoration procedures, tested regularly.

### 5.4 Security

- HTTPS everywhere; encryption at rest.
- Strong authentication with optional MFA; authorization at resource level.
- Audit logging, rate limiting, input validation, parameterized queries.
- Secure file uploads (Module 8).
- Secrets held in a secret manager, never in source code.
- Defense in depth against cross-tenant access: application filters and database row-level
  security.

### 5.5 Maintainability

- Layered architecture with clear module boundaries and dependency injection.
- Consistent naming; every function documented with a short comment.
- Automated unit, integration, API, contract and security tests.
- Documentation kept with the code.

---

## 6. Testing requirements

| Level | Purpose |
|---|---|
| Unit | Business rules in isolation: slot generation, status transitions, permission matrix, hashing, TOTP |
| Integration | Database transactions, constraints, row-level security, messaging |
| API | Validation, authorization, response contracts, error handling |
| Contract | Every backend exposes exactly the operations in the shared OpenAPI document |
| Security | Authentication, authorization, tenant isolation, common web vulnerabilities |
| Performance | k6 scenarios for registration, sign-in, provider search, availability, booking, cancellation, concurrent booking and patient record access at increasing load |

Performance runs record requests per second, p50/p95/p99 latency, error rate, database connection
usage, CPU, memory, queue processing time and cache hit ratio. Only measured values are reported,
together with the environment they were measured in.

All test data is synthetic. Real patient information is never used in development or testing.

---

## 7. Compliance constraints

The product is designed with the HIPAA Privacy, Security and Breach Notification Rules in mind:

- Protected health information (PHI) is classified and handled accordingly.
- Access follows the minimum-necessary principle.
- Data is encrypted in transit and at rest.
- Access to PHI is audited.
- Patient consent is recorded and revocable.
- Retention, backup, disaster recovery and incident response procedures are defined.
- Vendors that handle PHI require a Business Associate Agreement and verified HIPAA eligibility.
- PHI never appears in logs, traces, error messages or analytics.

**The product does not claim HIPAA compliance, ONC certification or any regulatory approval.** A
formal security risk assessment and a professional compliance review are required before the
platform is used with real patient information.

---

## 8. Release plan

| Release | Contents | Status |
|---|---|---|
| **Release 1** | Module 1 Authentication and user management (local credentials, MFA, sessions) · Module 2 Organization and clinic management (without subscriptions) · Module 3 Provider management · Module 4 Patient management (without medical history and documents) · Module 5 Appointment management (reminders are scheduled and published as events; delivery arrives with Release 2) · Audit log · Idempotency · Transactional outbox | Implemented and tested in the C# reference implementation; other backends follow |
| Release 2 | Module 7 Notifications and secure messaging: email, SMS and in-app delivery, preferences, delivery tracking and retries | Planned |
| Release 3 | Module 6 Medical records with versioning and amendment · Module 8 Document management with object storage and malware scanning | Planned |
| Release 4 | Module 9 Subscription and billing through an external payment provider · Module 10 Reporting and analytics | Planned |
| Release 5 | HL7 FHIR R4 integration layer · External OpenID Connect identity-provider federation · Audited support access for platform administrators · Appointment slot holds | Planned |

Notes:

- Release 1 is delivered in each backend in turn (C#, Java, Rust, Go, Python) and then in the React
  frontend. Later releases follow the same order.
- Items listed as planned are designed at the level of the data model and API outline but are not
  implemented. They are not exposed as endpoints until they work.

### Known limitations of Release 1

- Staff cannot yet link an existing patient record to a patient's portal account; a patient who
  self-registers with an organization receives a new patient record. Matching and merging duplicate
  records is future work.
- Account email (verification, password reset, invitations) is sent through SMTP from an in-memory
  queue; a message lost in a crash must be requested again. The durable, multi-channel notification
  pipeline arrives in Release 2.
- Appointment reminders are published as events but not yet delivered to patients.
- Platform administrators manage organizations and accounts only; they have no support access to
  tenant data.
- Rate limits are enforced per application instance; global limits belong at the WAF.
- The event publisher supports RabbitMQ; the SNS/SQS publisher for the AWS reference deployment is
  not yet written.
- Roles and permissions are fixed in code; organizations cannot define custom roles.

---

## 9. Glossary

| Term | Meaning |
|---|---|
| Organization | A customer account (tenant): a practice, clinic group or independent provider |
| Clinic | A business unit of an organization |
| Location | A physical site of a clinic, with an address and time zone |
| Membership | A user's staff relationship with an organization, carrying one or more roles |
| Provider | A clinician who delivers care and has a bookable schedule |
| Patient account link | The connection between a user account and a patient record in one organization |
| PHI | Protected health information as defined by HIPAA |
| NPI | National Provider Identifier, a ten-digit US identifier for healthcare providers |
| MRN | Medical record number, unique per patient within an organization |
| BAA | Business Associate Agreement |
| Idempotency key | A client-generated value that makes a retried request safe |
