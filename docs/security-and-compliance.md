# Security and Compliance Design

> ## Important notice
>
> **This software is not claimed to be HIPAA compliant, and it is not ONC certified or approved by
> any regulator.**
>
> The platform is designed with the HIPAA Privacy, Security and Breach Notification Rules in mind,
> but implementing encryption, access control and audit logging does not make a system compliant.
> Compliance depends on how the service is deployed, operated and governed.
>
> Before the platform is used with real patient information, the operating organization must:
>
> 1. Complete a formal **security risk assessment** (45 CFR §164.308(a)(1)(ii)(A)).
> 2. Obtain a **professional compliance review** from qualified privacy and security counsel.
> 3. Sign **Business Associate Agreements** with every vendor that creates, receives, maintains or
>    transmits PHI, and verify each service's current HIPAA eligibility.
> 4. Adopt the administrative policies, workforce training and procedures that software cannot
>    provide.
>
> Until then, only **synthetic data** may be loaded into any environment.

This document describes the security design of the platform and the controls implemented in the
application. Infrastructure controls are described in [deployment.md](deployment.md).

---

## 1. Scope and responsibilities

| Party | HIPAA role | Responsibility |
|---|---|---|
| Healthcare organization (customer) | Covered entity | Decides who may access patient data; maintains its own policies and workforce training |
| Platform operator | Business associate | Operates the service; safeguards PHI; reports incidents to customers |
| Cloud and messaging vendors | Subcontractor business associates | Provide HIPAA-eligible services under a BAA |

The software supports these responsibilities; it does not discharge them.

---

## 2. Data classification

Every column in the data model belongs to one of four classes. The class determines where the data
may appear and how it is handled.

| Class | Definition | Handling |
|---|---|---|
| **PHI** | Individually identifiable health information, including demographic data held because the person is a patient | Tenant-isolated, encrypted in transit and at rest, access audited, never in logs, traces, error messages, events, email, SMS or analytics |
| **Sensitive credential** | Secrets that grant access | Stored only hashed or encrypted; never returned by the API after creation; never logged |
| **Confidential business data** | Customer and workforce data that is not PHI | Tenant-isolated, encrypted, access controlled |
| **Operational metadata** | Identifiers, timestamps, status codes | May appear in logs and events |

### 2.1 Classification by schema

| Schema | Tables | Class | Notes |
|---|---|---|---|
| `patient` | `patients`, `emergency_contacts`, `patient_consents`, `patient_provider_relationships`, `patient_account_links` | PHI | Name, date of birth, contact details and address are PHI in this context. Emergency contacts identify third parties and are handled as PHI. |
| `scheduling` | `appointments`, `appointment_status_history`, `appointment_reminders`, `waiting_list_entries` | PHI | The link between a patient and a provider or specialty is itself health information. The free-text visit reason is PHI. |
| `identity` | `users` | Confidential; PHI when the user is linked to a patient record | Email, name, phone |
| `identity` | `users.password_hash`, `sessions.refresh_token_hash`, `user_tokens.token_hash`, `mfa_factors.secret_encrypted`, `mfa_recovery_codes.code_hash` | Sensitive credential | One-way hashes, except the TOTP secret, which is encrypted because it must be read back |
| `organization` | `organizations`, `clinics`, `clinic_locations`, `organization_settings`, `clinic_holidays`, `organization_memberships`, `staff_invitations` | Confidential business data | Invitation tokens are stored as hashes |
| `provider` | `providers`, `provider_credentials`, `provider_specialties`, `provider_locations`, `provider_schedules`, `schedule_exceptions` | Confidential business data | License numbers are confidential. Directory-listed profile fields are public by the organization's choice. |
| `platform` | `audit_events` | Operational metadata about PHI access | Contains identifiers only, never record content |
| `platform` | `outbox_events`, `idempotency_records` | Operational metadata; idempotency responses may contain PHI | Idempotency records are tenant-isolated and purged after their retention window |

Planned modules (medical records, documents, messaging) hold clinical PHI and will be classified
when they are designed in detail.

### 2.2 Where PHI must never appear

| Location | Rule |
|---|---|
| Application logs | Identifiers, counts and status codes only. No names, dates of birth, contact details, visit reasons or request bodies. |
| Traces and metrics | Span attributes and metric labels carry identifiers and route templates only. SQL parameter values are not captured. |
| Error responses | Problem details describe the error class and a machine-readable code. They do not echo patient data. |
| Outbox events and message queues | Event type and resource identifiers only. Consumers load the data they are authorized to read. |
| Email and SMS | No clinical or scheduling detail beyond organization name, date and time, and a link that requires sign-in. |
| URLs | No PHI in paths or query strings other than opaque identifiers; searches by name use the query string over TLS and are excluded from access logs' query capture. |
| Analytics | Aggregates only. |

---

## 3. Access control

### 3.1 Authentication

| Control | Design |
|---|---|
| Passwords | PBKDF2-HMAC-SHA256, 600,000 iterations, 16-byte per-user random salt, versioned hash format so parameters can be raised (hashes are upgraded at the next sign-in). Passwords must be 12–128 characters and differ from the email address. Checking passwords against breached-password lists is not implemented yet (open item). |
| Access tokens | RS256-signed JWT, 10-minute lifetime, carrying user and session identifiers only. The private key is held in the secret manager; public keys are published as a JWK set. |
| Refresh tokens | Opaque random values, stored only as SHA-256 hashes in `identity.sessions`, rotated on every use. Reuse of a rotated token revokes the session. |
| Session limits | Idle timeout 60 minutes and absolute lifetime 12 hours (configurable), supporting automatic logoff (§164.312(a)(2)(iii)). |
| Multi-factor authentication | TOTP (RFC 6238, 6 digits, 30-second steps, ±1 step tolerance) with replay protection on the last used step, and 10 single-use recovery codes stored as hashes. The TOTP secret is encrypted with AES-256-GCM. Disabling MFA requires the password and a current code. |
| Lockout | 5 failed sign-ins (password or MFA code) lock the account for 15 minutes (configurable). Sign-in and recovery endpoints are rate limited to 20 requests per minute per client address, and all other requests to 600 per minute per user, per application instance. |
| One-time tokens | Email verification (24 h), password reset (60 min), MFA challenge (5 min) and invitation (7 days) tokens are random, single-use, time-limited and stored hashed. |
| Enumeration resistance | Registration, password recovery and resend-verification return the same response whether or not the account exists; sign-in returns the same error, after the same hashing work, for unknown accounts and wrong passwords. |
| Revocation | Sign-out, session revocation, password change or reset, suspension and refresh-token reuse revoke sessions and clear the cached session state, so access tokens stop working on the next request. Other session state is cached for at most 30 seconds. |

Federation with an external OpenID Connect identity provider is designed for and planned; it is not
implemented in Release 1.

### 3.2 Authorization and minimum necessary

Authorization is evaluated in the backend on every request, in three layers:

1. **Tenant relationship.** The organization named by `X-Organization-Id` (or the path on
   `/organizations` routes) is resolved against the caller's active membership and patient account
   links. A verified user with neither is a *guest* only when the organization allows patient
   self-scheduling (clinics, active providers, availability and self-registration); otherwise access
   is refused. Suspended organizations refuse everyone.
2. **Permission.** Each endpoint requires a named permission. Permissions are derived from the
   membership's roles (owner, clinic administrator, provider, receptionist, staff), from the patient
   link, or from the guest role. The matrix is listed in
   [architecture.md](architecture.md#33-permission-matrix).
3. **Resource scope.** Services narrow the result to what the caller needs:
   - a provider without administrative roles sees only assigned patients and their own calendar;
   - a patient sees only their own record and appointments;
   - staff roles see only administrative data, not clinical content (when the medical records
     module is added).

The platform administrator role has no implicit access to tenant PHI. Support access will require
an explicit, time-limited, audited grant (planned).

### 3.3 Tenant isolation

Isolation is enforced twice, so that a defect in one layer does not expose another organization's
data:

| Layer | Mechanism |
|---|---|
| Application | Every tenant-owned table carries `organization_id`; every reviewed SQL statement filters by the selected organization. |
| Database | PostgreSQL row-level security is enabled **and forced** on every tenant-owned table. Policies compare `organization_id` with the per-connection setting `app.current_organization_id`; `app.current_user_id` and `app.access_scope` support the few self-service and system paths. |
| Referential integrity | Child rows reference `(organization_id, id)` of their parent through composite foreign keys, so no row can point at another organization's patient, provider, location or appointment. |
| Database roles | The application connects as `healthcare_app`, which is not the table owner and has no `BYPASSRLS`; it cannot alter the schema, update or delete audit events or appointment history, or delete patients, consents and appointments. Schema changes run as `healthcare_migrator`. |
| Schemas | Tables are grouped into `identity`, `organization`, `provider`, `patient`, `scheduling` and `platform`, so privileges can be granted per data domain. |

Cross-tenant code paths (background workers, the provider directory, platform administration) set
the system access scope explicitly and are limited to a small, reviewed set of queries.

Tenant isolation is covered by automated tests at both layers, including direct SQL executed as the
application role.

---

## 4. Encryption

| Data state | Control |
|---|---|
| In transit, client to service | TLS 1.2 or later at the load balancer; HSTS; HTTP redirected to HTTPS |
| In transit, service to data stores | TLS required for PostgreSQL, Redis and the message broker in all non-local environments |
| At rest, database | Storage-level encryption with a customer-managed key (AWS KMS); encrypted automated backups and snapshots |
| At rest, cache and queues | Encryption at rest enabled on managed Redis and messaging services |
| At rest, object storage | Server-side encryption with KMS; bucket policies deny unencrypted transport |
| Field level | TOTP secrets encrypted with AES-256-GCM using a key from the secret manager |
| Key management | Keys held in KMS with rotation enabled; application secrets and signing keys in the secret manager; key use logged |

Local development uses unencrypted connections to local containers and must contain synthetic data
only.

---

## 5. Audit trail

`platform.audit_events` records who did what to which resource and when (§164.312(b)).

| Aspect | Design |
|---|---|
| Recorded events | Sign-in success and failure (with the reason), MFA changes and recovery-code use, password changes and resets, session revocation, refresh-token reuse; membership, invitation and role changes; organization, clinic, location, holiday, provider, credential, schedule and exception changes; creation, update, **read** and **search** of patient records (searches record the result count, never the search text); emergency contacts, care relationships, consent grant and revocation; appointment booking, life-cycle changes and rescheduling; waiting-list changes; platform administrator actions |
| Content | Time, organization, actor, action, resource type and identifier, outcome, client address, user agent, request identifier, and non-PHI metadata |
| Atomicity | Audit rows for a change are written in the same database transaction as the change |
| Integrity | Append-only: the application role has `INSERT` and `SELECT` only; no update or delete |
| Review | `GET /api/v1/audit-events` (owners and clinic administrators) filters by action, resource, actor and time range, so organizations can review access to their patients' data |
| Retention | At least six years (see section 7) |

Recommended operational additions: stream audit events to write-once storage (object lock) and
alert on unusual access patterns.

---

## 6. Consent

- Consents are recorded per patient and type: treatment, notice of privacy practices
  acknowledgement, telehealth, email communication, SMS communication and data sharing.
- Each record stores the status, the time granted, the version of the document accepted, the method
  of capture (electronic, paper, verbal) and the staff member who captured it.
- Revocation is recorded with its time; records are never deleted, so the consent history is
  preserved.
- Notification delivery (Release 2) checks communication consent and preferences before sending.

---

## 7. Data retention

Retention periods must be confirmed by counsel for each state in which customers operate; state
medical-record laws often exceed the HIPAA documentation requirement. The defaults below are a
starting point for policy, not legal advice.

| Data | Default retention | Mechanism |
|---|---|---|
| Patient records, consents, appointments | Life of the customer relationship plus the period required by state law (commonly 7–10 years; longer for minors) | Status changes rather than deletion (the application role cannot delete these rows); export and purge at contract end |
| Audit events | 6 years minimum (§164.316(b)(2)) | Partition by month; archive closed partitions to encrypted object storage (not yet implemented) |
| Sessions and one-time tokens | 30 days after expiry (`Workers:ExpiredCredentialRetentionDays`) | Hourly maintenance job |
| Idempotency records | 48 hours (`App:IdempotencyRetentionHours`) | Hourly maintenance job |
| Outbox events | 14 days after publication (`Workers:OutboxRetentionDays`) | Hourly maintenance job |
| Invitations and waiting-list entries | Marked expired when past their date; kept as history | Hourly maintenance job |
| Application logs | 90 days searchable, 1 year archived | Log platform lifecycle rules |
| Database backups | 35 days point-in-time; monthly snapshots for 1 year | Managed backup policy |

At contract termination the customer's data is returned in an agreed format and then destroyed,
including in backups as they age out; destruction is documented.

---

## 8. Backup and disaster recovery

| Objective | Target |
|---|---|
| Recovery point objective | ≤ 5 minutes (continuous WAL archiving, point-in-time recovery) |
| Recovery time objective | ≤ 1 hour for loss of an availability zone; ≤ 4 hours for a regional restore |

- Multi-AZ database with automatic failover; application instances in at least two zones.
- Encrypted automated backups; snapshots copied to a second region.
- Infrastructure defined as code so an environment can be rebuilt.
- Restore tests at least quarterly, with the result recorded. A backup that has not been restored
  is not considered a backup.
- These are design objectives. They have not been tested, because no environment has been deployed.

Procedures are in [deployment.md](deployment.md).

---

## 9. Incident response and breach notification

### 9.1 Response procedure outline

| Phase | Actions |
|---|---|
| Preparation | Named incident lead and on-call rota; contact list for counsel, customers and vendors; runbooks; alerting on authentication anomalies, authorization failures and bulk data access |
| Identification | Triage the alert; classify severity; open an incident record; preserve logs and audit data |
| Containment | Revoke sessions and credentials; rotate keys and secrets; isolate affected components; block malicious sources at the WAF |
| Eradication and recovery | Remove the cause; patch; restore from clean backups if required; verify integrity; monitor closely |
| Assessment | Determine whether unsecured PHI was acquired, accessed, used or disclosed; perform the four-factor risk assessment (§164.402) |
| Notification | Notify affected customers and, where required, individuals and regulators (below) |
| Review | Blameless post-incident review; corrective actions tracked to completion; documentation retained for six years |

### 9.2 Breach notification outline (45 CFR §§164.400–414)

| Notification | Responsible | Deadline |
|---|---|---|
| Business associate to covered entity | Platform operator | Without unreasonable delay and no later than 60 days after discovery; the BAA normally sets a much shorter period |
| Covered entity to affected individuals | Customer | Without unreasonable delay, no later than 60 days after discovery |
| Covered entity to HHS | Customer | 500 or more individuals: within 60 days of discovery. Fewer than 500: within 60 days after the end of the calendar year |
| Covered entity to media | Customer | Breaches affecting more than 500 residents of a state or jurisdiction |

State breach-notification laws may impose shorter deadlines and additional recipients. The exact
obligations must be confirmed with counsel when the incident response plan is formalized.

The audit trail supports the assessment by showing which records were accessed, by which account
and when.

---

## 10. Vendors and Business Associate Agreements

A BAA is required before PHI reaches any vendor service. HIPAA eligibility is service-specific and
changes over time; it must be verified against the vendor's current published list at the time of
adoption, and the service must be configured as that vendor requires.

| Vendor category | PHI exposure | Requirement |
|---|---|---|
| Cloud infrastructure (compute, database, cache, storage, queues, logging) | Yes | BAA; use only eligible services; encryption and logging configured |
| Email delivery | Limited (recipient address, appointment time) | BAA; message content restricted as in section 2.2 |
| SMS delivery | Limited | BAA; consent recorded; content restricted |
| Malware scanning for documents | Yes | BAA, or run inside the operator's own environment |
| Error tracking and observability | Must not receive PHI | Scrubbing verified; BAA if any risk remains |
| Payment provider | No PHI; no card data on the platform | PCI scope stays with the provider; do not send patient information |
| Customer support tools | Must not contain PHI | Policy and training; BAA if PHI could be entered |

---

## 11. Secret management

- No secrets in source code, container images or committed configuration. The repository contains
  only an example environment file with placeholder values.
- Secrets (database credentials, JWT signing key, field-encryption key, SMTP and broker
  credentials) are injected at runtime from a secret manager.
- Separate secrets per environment; production secrets are not readable from development or staging.
- The application and migration roles have separate credentials; only the migration job receives
  the owner credential.
- Rotation: database credentials and signing keys are rotated on a schedule and immediately after a
  suspected exposure. The JWK set can publish more than one key to allow rotation without downtime.
- Outside the Development and Testing environments the application refuses to start without the
  token signing key, and refuses to start if the field-encryption key is missing or is not a
  256-bit key. In Development, temporary keys are generated at startup and a warning is logged.

---

## 12. Application security controls

| Risk | Control |
|---|---|
| Injection | Bound parameters through SQLAlchemy `text()`/Core and asyncpg; no string-built SQL from user input (optional filters add placeholders, never values) |
| Broken access control | Three-layer authorization (section 3.2) and row-level security (section 3.3) |
| Invalid input | A three-stage binder and the reference's validation rules on every request model; strict JSON (no numbers as strings, enums as named values only); consistent RFC 9457 validation errors; 1 MB request body limit; page size capped at 100 |
| Abuse and brute force | Rate limiting per client address on authentication endpoints and per user elsewhere (per instance); account lockout |
| Duplicate submissions | Idempotency keys stored transactionally |
| Lost updates | Optimistic concurrency with an explicit version on mutable aggregates |
| Information leakage | Generic error messages with stable codes; stack traces and SQL never returned; database errors translated; no `Server` header; security headers (`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`, `Cache-Control: no-store`); HSTS outside development |
| Cross-origin misuse | CORS limited to configured origins; tokens sent in the `Authorization` header, so cross-site request forgery through ambient cookies does not apply |
| Vulnerable dependencies | Dependency and container image scanning in CI; pinned versions |
| File uploads (Release 3) | Type and size validation, malware scanning, storage outside the web root in object storage |

---

## 13. Threat model summary

A STRIDE-style summary of the principal threats. It is a starting point for the formal risk
assessment, not a substitute for it.

| Category | Threat | Primary mitigations | Residual risk and follow-up |
|---|---|---|---|
| Spoofing | Credential stuffing or password guessing against user accounts | Slow password hashing, lockout, rate limiting, MFA, enumeration-resistant responses | MFA is optional per user; consider enforcing it for staff roles by organization policy |
| Spoofing | Stolen refresh token replayed | Rotation with reuse detection, hashed storage, idle and absolute session limits, session listing and revocation | Token theft from a compromised device remains possible until the session expires or is revoked |
| Spoofing | Forged access token | RS256 signature validation, issuer, audience and lifetime checks; private key in the secret manager | Key compromise requires rotation; rotation procedure must be rehearsed |
| Tampering | Modification of requests in transit | TLS everywhere, HSTS | — |
| Tampering | Unauthorized alteration of records or audit data | Permission checks, optimistic concurrency, append-only audit privileges, status-history tables | A database administrator can alter data; mitigate with restricted access, database activity logging and write-once audit export |
| Repudiation | A user denies having viewed or changed a record | Audit events written in the same transaction, with actor, session, address and request identifier | Shared workstations weaken attribution; organizational policy must forbid shared accounts |
| Information disclosure | Cross-tenant data access through an application defect | Query filters plus forced row-level security; non-owner application role; isolation tests | System-scope code paths bypass the tenant predicate by design; keep them few and reviewed |
| Information disclosure | Insecure direct object reference | Tenant and resource-scope checks on every lookup; non-sequential identifiers | — |
| Information disclosure | PHI in logs, traces, events or notifications | Logging rules (section 2.2), structured logging of identifiers only, event payloads without PHI | Requires continuous review; add automated log scanning for patterns such as dates of birth and emails |
| Information disclosure | Excessive access by staff | Role-based permissions, provider and patient resource scoping, read auditing | Curiosity access within a role is detected, not prevented; audit review is an organizational duty |
| Information disclosure | Loss of a backup or snapshot | Encrypted backups with KMS keys, restricted snapshot sharing | — |
| Denial of service | Request floods or expensive queries | WAF and rate limiting, mandatory pagination with capped page size, availability limited to 31 days, request size limit, 5-second lock timeout on booking | Volumetric attacks rely on the provider's network protections; database statement timeouts are recommended but not yet configured |
| Denial of service | Slot exhaustion by abusive bookings | Authenticated, verified accounts only; booking rules; rate limiting; audit | Consider per-patient limits on open appointments |
| Elevation of privilege | A staff member grants themselves a higher role | Role changes require a permission the grantee lacks; administrators cannot manage owners; changes audited | — |
| Elevation of privilege | Compromised application bypasses tenant policy | Application role cannot bypass row-level security or alter schema | A compromised application can still set the tenant setting; row-level security defends against defects, not full compromise — network isolation and least-privilege infrastructure limit the blast radius |
| Elevation of privilege | Abuse of platform administrator role | No implicit PHI access, actions audited | Support-access workflow with explicit grants is planned |

---

## 14. Development and test data

- Development, test, staging and demonstration environments use **synthetic data only**. Names,
  dates of birth, addresses, phone numbers and identifiers are generated and do not correspond to
  real people.
- Production data is never copied to another environment.
- Test fixtures use the reserved `example.test` domain, fictitious 555-01xx phone numbers and
  randomly generated, check-digit-valid NPIs that are not tied to real providers.
- Any discovery of real patient data outside production is treated as a security incident.

---

## 15. HIPAA Security Rule mapping (technical safeguards)

This table shows where each technical safeguard is addressed in the design. It is an aid to the
risk assessment, not evidence of compliance.

| Safeguard | Reference | Design element |
|---|---|---|
| Unique user identification | §164.312(a)(2)(i) | Individual accounts; no shared credentials; actor recorded on every audit event |
| Emergency access procedure | §164.312(a)(2)(ii) | Not yet defined — requires an operational procedure and the planned support-access workflow |
| Automatic logoff | §164.312(a)(2)(iii) | Idle and absolute session timeouts |
| Encryption and decryption | §164.312(a)(2)(iv) | Section 4 |
| Audit controls | §164.312(b) | Section 5 |
| Integrity | §164.312(c)(1) | Constraints, transactions, optimistic concurrency, status history; record versioning planned with medical records |
| Person or entity authentication | §164.312(d) | Section 3.1 |
| Transmission security | §164.312(e)(1) | TLS for all external and internal connections |

Administrative and physical safeguards (workforce training, sanction policy, facility access,
device and media controls, contingency planning) are the responsibility of the operator and its
customers and are outside the scope of the software.

---

## 16. Open items before production use

1. Formal security risk assessment and compliance review.
2. Signed BAAs and verified HIPAA eligibility for every service in the deployment.
3. Written policies: access management, incident response, breach notification, retention and
   disposal, contingency plan, workforce training.
4. Independent penetration test and remediation.
5. Emergency access procedure and audited support-access workflow.
6. Enforced MFA policy for workforce accounts; breached-password screening.
7. Write-once export of the audit trail and automated review of access anomalies.
8. Tested backup restoration and failover in the deployed environment.
