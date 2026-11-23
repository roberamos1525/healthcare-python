-- V1 initial schema: 6 schemas, 30 tables, keys, check constraints and indexes.
-- The shared database design of the HealthCare platform, reviewed against pg_dump --schema-only of a
-- database migrated by the reference implementation; every backend produces exactly these objects.
-- Row-level security, the exclusion constraint and privileges follow in V2 (0002_security_up.sql, a
-- byte-identical copy of the reference security script). Alembic runs this file through the driver's
-- simple query protocol inside the migration transaction, so it is applied atomically.

-- ---------------------------------------------------------------------------------------------
-- Schemas
-- ---------------------------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS identity;

CREATE SCHEMA IF NOT EXISTS organization;

CREATE SCHEMA IF NOT EXISTS patient;

CREATE SCHEMA IF NOT EXISTS platform;

CREATE SCHEMA IF NOT EXISTS provider;

CREATE SCHEMA IF NOT EXISTS scheduling;

-- ---------------------------------------------------------------------------------------------
-- Tables and check constraints
-- ---------------------------------------------------------------------------------------------

CREATE TABLE identity.mfa_factors (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    type character varying(20) NOT NULL,
    secret_encrypted character varying(500) NOT NULL,
    status character varying(40) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    confirmed_at timestamp with time zone,
    last_used_time_step bigint,
    CONSTRAINT ck_mfa_factors_status CHECK (status IN ('pending', 'active'))
);

CREATE TABLE identity.mfa_recovery_codes (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    code_hash character varying(64) NOT NULL,
    used_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL
);

CREATE TABLE identity.sessions (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    refresh_token_hash character varying(64) NOT NULL,
    previous_refresh_token_hash character varying(64),
    rotated_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    last_used_at timestamp with time zone NOT NULL,
    idle_expires_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    revoked_reason character varying(50),
    mfa_verified boolean NOT NULL,
    ip_address character varying(45),
    user_agent character varying(300)
);

CREATE TABLE identity.user_tokens (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    purpose character varying(40) NOT NULL,
    token_hash character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_user_tokens_purpose CHECK (purpose IN ('email_verification', 'password_reset', 'mfa_challenge'))
);

CREATE TABLE identity.users (
    id uuid NOT NULL,
    email character varying(254) NOT NULL,
    email_normalized character varying(254) NOT NULL,
    password_hash character varying(300),
    first_name character varying(100) NOT NULL,
    last_name character varying(100) NOT NULL,
    phone character varying(30),
    status character varying(40) NOT NULL,
    email_verified_at timestamp with time zone,
    platform_role character varying(40),
    mfa_enabled boolean NOT NULL,
    failed_login_count integer NOT NULL,
    lockout_until timestamp with time zone,
    last_login_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_users_platform_role CHECK (((platform_role IS NULL) OR ((platform_role)::text = 'platform_admin'::text))),
    CONSTRAINT ck_users_status CHECK (status IN ('pending_verification', 'active', 'suspended', 'deactivated'))
);

CREATE TABLE organization.clinic_holidays (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    clinic_location_id uuid,
    date date NOT NULL,
    name character varying(100) NOT NULL,
    created_at timestamp with time zone NOT NULL
);

CREATE TABLE organization.clinic_locations (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    clinic_id uuid NOT NULL,
    name character varying(200) NOT NULL,
    address_line1 character varying(200) NOT NULL,
    address_line2 character varying(200),
    city character varying(100) NOT NULL,
    state character(2) NOT NULL,
    postal_code character varying(10) NOT NULL,
    country character(2) NOT NULL,
    time_zone character varying(64) NOT NULL,
    phone character varying(30),
    status character varying(40) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    version integer NOT NULL,
    CONSTRAINT ck_clinic_locations_status CHECK (status IN ('active', 'inactive'))
);

CREATE TABLE organization.clinics (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    name character varying(200) NOT NULL,
    description character varying(2000),
    phone character varying(30),
    email character varying(254),
    status character varying(40) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    version integer NOT NULL,
    CONSTRAINT ck_clinics_status CHECK (status IN ('active', 'inactive'))
);

CREATE TABLE organization.organization_memberships (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    user_id uuid NOT NULL,
    roles text[] NOT NULL,
    status character varying(40) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_organization_memberships_roles CHECK (((roles <@ ARRAY['owner'::text, 'clinic_admin'::text, 'provider'::text, 'receptionist'::text, 'staff'::text]) AND (cardinality(roles) > 0))),
    CONSTRAINT ck_organization_memberships_status CHECK (status IN ('active', 'suspended', 'removed'))
);

CREATE TABLE organization.organization_settings (
    organization_id uuid NOT NULL,
    default_appointment_duration_minutes integer NOT NULL,
    minimum_booking_notice_minutes integer NOT NULL,
    booking_horizon_days integer NOT NULL,
    patient_cancellation_window_hours integer NOT NULL,
    reminder_offsets_minutes integer[] NOT NULL,
    allow_patient_self_scheduling boolean NOT NULL,
    auto_confirm_patient_bookings boolean NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_organization_settings_duration CHECK (((default_appointment_duration_minutes >= 5) AND (default_appointment_duration_minutes <= 480))),
    CONSTRAINT ck_organization_settings_horizon CHECK (((booking_horizon_days >= 1) AND (booking_horizon_days <= 365)))
);

CREATE TABLE organization.organizations (
    id uuid NOT NULL,
    name character varying(200) NOT NULL,
    slug character varying(80) NOT NULL,
    type character varying(40) NOT NULL,
    status character varying(40) NOT NULL,
    contact_email character varying(254),
    phone character varying(30),
    time_zone character varying(64) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    version integer NOT NULL,
    CONSTRAINT ck_organizations_status CHECK (status IN ('active', 'suspended', 'closed')),
    CONSTRAINT ck_organizations_type CHECK (type IN ('independent_practice', 'clinic', 'multi_location_group'))
);

CREATE TABLE organization.staff_invitations (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    email character varying(254) NOT NULL,
    email_normalized character varying(254) NOT NULL,
    roles text[] NOT NULL,
    token_hash character varying(64) NOT NULL,
    status character varying(40) NOT NULL,
    invited_by_user_id uuid NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    accepted_at timestamp with time zone,
    accepted_by_user_id uuid,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_staff_invitations_status CHECK (status IN ('pending', 'accepted', 'revoked', 'expired'))
);

CREATE TABLE patient.emergency_contacts (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    name character varying(200) NOT NULL,
    relationship character varying(50) NOT NULL,
    phone character varying(30) NOT NULL,
    email character varying(254),
    is_primary boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);

CREATE TABLE patient.patient_account_links (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    user_id uuid NOT NULL,
    relationship character varying(40) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_patient_account_links_relationship CHECK (relationship IN ('self', 'authorized_representative'))
);

CREATE TABLE patient.patient_consents (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    type character varying(40) NOT NULL,
    status character varying(40) NOT NULL,
    method character varying(40) NOT NULL,
    document_version character varying(50),
    granted_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    recorded_by_user_id uuid NOT NULL,
    revoked_by_user_id uuid,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_patient_consents_method CHECK (method IN ('electronic', 'paper', 'verbal')),
    CONSTRAINT ck_patient_consents_status CHECK (status IN ('granted', 'revoked')),
    CONSTRAINT ck_patient_consents_type CHECK (type IN ('treatment', 'privacy_practices_acknowledgement', 'telehealth', 'email_communication', 'sms_communication', 'information_sharing'))
);

CREATE TABLE patient.patient_provider_relationships (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    provider_id uuid NOT NULL,
    type character varying(40) NOT NULL,
    started_on date NOT NULL,
    ended_on date,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_patient_provider_relationships_type CHECK (type IN ('primary_care', 'specialist', 'consulting'))
);

CREATE TABLE patient.patients (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    medical_record_number character varying(20) NOT NULL,
    first_name character varying(100) NOT NULL,
    middle_name character varying(100),
    last_name character varying(100) NOT NULL,
    preferred_name character varying(100),
    date_of_birth date NOT NULL,
    administrative_gender character varying(40) NOT NULL,
    email character varying(254),
    phone character varying(30),
    address_line1 character varying(200),
    address_line2 character varying(200),
    city character varying(100),
    state character(2),
    postal_code character varying(10),
    country character(2),
    preferred_language character varying(35),
    status character varying(40) NOT NULL,
    created_by_user_id uuid,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    version integer NOT NULL,
    CONSTRAINT ck_patients_administrative_gender CHECK (administrative_gender IN ('female', 'male', 'other', 'unknown')),
    CONSTRAINT ck_patients_status CHECK (status IN ('active', 'inactive'))
);

CREATE TABLE platform.audit_events (
    id uuid NOT NULL,
    occurred_at timestamp with time zone NOT NULL,
    organization_id uuid,
    actor_user_id uuid,
    action character varying(100) NOT NULL,
    resource_type character varying(50) NOT NULL,
    resource_id uuid,
    outcome character varying(40) NOT NULL,
    ip_address character varying(45),
    user_agent character varying(300),
    request_id character varying(100),
    metadata jsonb,
    CONSTRAINT ck_audit_events_outcome CHECK (outcome IN ('success', 'denied', 'failure'))
);

CREATE TABLE platform.idempotency_records (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    user_id uuid NOT NULL,
    operation character varying(100) NOT NULL,
    key character varying(200) NOT NULL,
    request_hash character varying(64) NOT NULL,
    response_body jsonb,
    resource_id uuid,
    created_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL
);

CREATE TABLE platform.outbox_events (
    id uuid NOT NULL,
    organization_id uuid,
    event_type character varying(100) NOT NULL,
    aggregate_type character varying(50) NOT NULL,
    aggregate_id uuid NOT NULL,
    payload jsonb NOT NULL,
    occurred_at timestamp with time zone NOT NULL,
    available_at timestamp with time zone NOT NULL,
    attempts integer NOT NULL,
    last_error character varying(500),
    processed_at timestamp with time zone
);

CREATE TABLE provider.provider_credentials (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    provider_id uuid NOT NULL,
    type character varying(40) NOT NULL,
    issuing_authority character varying(200) NOT NULL,
    identifier character varying(100) NOT NULL,
    issued_on date,
    expires_on date,
    verification_status character varying(40) NOT NULL,
    verified_by_user_id uuid,
    verified_at timestamp with time zone,
    verification_notes character varying(1000),
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_provider_credentials_type CHECK (type IN ('medical_license', 'board_certification', 'dea_registration', 'other')),
    CONSTRAINT ck_provider_credentials_verification_status CHECK (verification_status IN ('pending', 'verified', 'rejected'))
);

CREATE TABLE provider.provider_locations (
    provider_id uuid NOT NULL,
    clinic_location_id uuid NOT NULL,
    organization_id uuid NOT NULL
);

CREATE TABLE provider.provider_schedules (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    provider_id uuid NOT NULL,
    clinic_location_id uuid NOT NULL,
    day_of_week integer NOT NULL,
    start_time time without time zone NOT NULL,
    end_time time without time zone NOT NULL,
    slot_duration_minutes integer,
    effective_from date NOT NULL,
    effective_to date,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_provider_schedules_dates CHECK (((effective_to IS NULL) OR (effective_to >= effective_from))),
    CONSTRAINT ck_provider_schedules_day CHECK (((day_of_week >= 0) AND (day_of_week <= 6))),
    CONSTRAINT ck_provider_schedules_duration CHECK (((slot_duration_minutes IS NULL) OR ((slot_duration_minutes >= 5) AND (slot_duration_minutes <= 480)))),
    CONSTRAINT ck_provider_schedules_times CHECK ((end_time > start_time))
);

CREATE TABLE provider.provider_specialties (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    provider_id uuid NOT NULL,
    name character varying(100) NOT NULL,
    taxonomy_code character varying(20),
    is_primary boolean NOT NULL
);

CREATE TABLE provider.providers (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    user_id uuid,
    first_name character varying(100) NOT NULL,
    last_name character varying(100) NOT NULL,
    professional_title character varying(50),
    npi character(10),
    biography character varying(4000),
    default_appointment_duration_minutes integer,
    status character varying(40) NOT NULL,
    accepting_new_patients boolean NOT NULL,
    listed_in_directory boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    version integer NOT NULL,
    CONSTRAINT ck_providers_duration CHECK (((default_appointment_duration_minutes IS NULL) OR ((default_appointment_duration_minutes >= 5) AND (default_appointment_duration_minutes <= 480)))),
    CONSTRAINT ck_providers_npi CHECK (((npi IS NULL) OR (npi ~ '^[0-9]{10}$'::text))),
    CONSTRAINT ck_providers_status CHECK (status IN ('onboarding', 'active', 'inactive'))
);

CREATE TABLE provider.schedule_exceptions (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    provider_id uuid NOT NULL,
    clinic_location_id uuid,
    type character varying(40) NOT NULL,
    starts_at timestamp with time zone NOT NULL,
    ends_at timestamp with time zone NOT NULL,
    slot_duration_minutes integer,
    reason character varying(200),
    created_by_user_id uuid,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_schedule_exceptions_location CHECK ((((type)::text <> 'additional_hours'::text) OR (clinic_location_id IS NOT NULL))),
    CONSTRAINT ck_schedule_exceptions_range CHECK ((ends_at > starts_at)),
    CONSTRAINT ck_schedule_exceptions_type CHECK (type IN ('time_off', 'additional_hours'))
);

CREATE TABLE scheduling.appointment_reminders (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    appointment_id uuid NOT NULL,
    channel character varying(40) NOT NULL,
    scheduled_for timestamp with time zone NOT NULL,
    status character varying(40) NOT NULL,
    queued_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_appointment_reminders_channel CHECK (channel IN ('email', 'sms', 'in_app')),
    CONSTRAINT ck_appointment_reminders_status CHECK (status IN ('scheduled', 'queued', 'cancelled'))
);

CREATE TABLE scheduling.appointment_status_history (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    appointment_id uuid NOT NULL,
    from_status character varying(40),
    to_status character varying(40) NOT NULL,
    change_type character varying(20) NOT NULL,
    changed_by_user_id uuid,
    changed_at timestamp with time zone NOT NULL,
    reason character varying(500),
    previous_starts_at timestamp with time zone,
    previous_ends_at timestamp with time zone,
    CONSTRAINT ck_appointment_status_history_change_type CHECK (change_type IN ('booked', 'status_changed', 'rescheduled')),
    CONSTRAINT ck_appointment_status_history_from_status CHECK (from_status IN ('pending', 'confirmed', 'checked_in', 'in_progress', 'completed', 'cancelled', 'no_show')),
    CONSTRAINT ck_appointment_status_history_to_status CHECK (to_status IN ('pending', 'confirmed', 'checked_in', 'in_progress', 'completed', 'cancelled', 'no_show'))
);

CREATE TABLE scheduling.appointments (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    provider_id uuid NOT NULL,
    clinic_location_id uuid NOT NULL,
    starts_at timestamp with time zone NOT NULL,
    ends_at timestamp with time zone NOT NULL,
    status character varying(40) NOT NULL,
    visit_type character varying(40) NOT NULL,
    reason character varying(500),
    booking_channel character varying(40) NOT NULL,
    booked_by_user_id uuid,
    cancelled_at timestamp with time zone,
    cancellation_reason character varying(500),
    cancelled_by_user_id uuid,
    checked_in_at timestamp with time zone,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    version integer NOT NULL,
    CONSTRAINT ck_appointments_booking_channel CHECK (booking_channel IN ('staff', 'patient_portal')),
    CONSTRAINT ck_appointments_range CHECK ((ends_at > starts_at)),
    CONSTRAINT ck_appointments_status CHECK (status IN ('pending', 'confirmed', 'checked_in', 'in_progress', 'completed', 'cancelled', 'no_show')),
    CONSTRAINT ck_appointments_visit_type CHECK (visit_type IN ('in_person', 'telehealth'))
);

CREATE TABLE scheduling.waiting_list_entries (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    patient_id uuid NOT NULL,
    provider_id uuid,
    clinic_location_id uuid,
    earliest_date date NOT NULL,
    latest_date date NOT NULL,
    notes character varying(500),
    status character varying(40) NOT NULL,
    fulfilled_appointment_id uuid,
    created_by_user_id uuid,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_waiting_list_entries_dates CHECK ((latest_date >= earliest_date)),
    CONSTRAINT ck_waiting_list_entries_status CHECK (status IN ('active', 'fulfilled', 'cancelled', 'expired'))
);

-- ---------------------------------------------------------------------------------------------
-- Primary keys, alternate keys and unique constraints
-- ---------------------------------------------------------------------------------------------

ALTER TABLE ONLY identity.mfa_factors
    ADD CONSTRAINT pk_mfa_factors PRIMARY KEY (id);

ALTER TABLE ONLY identity.mfa_recovery_codes
    ADD CONSTRAINT pk_mfa_recovery_codes PRIMARY KEY (id);

ALTER TABLE ONLY identity.sessions
    ADD CONSTRAINT pk_sessions PRIMARY KEY (id);

ALTER TABLE ONLY identity.user_tokens
    ADD CONSTRAINT pk_user_tokens PRIMARY KEY (id);

ALTER TABLE ONLY identity.users
    ADD CONSTRAINT pk_users PRIMARY KEY (id);

ALTER TABLE ONLY organization.clinic_locations
    ADD CONSTRAINT ak_clinic_locations_organization_id_id UNIQUE (organization_id, id);

ALTER TABLE ONLY organization.clinics
    ADD CONSTRAINT ak_clinics_organization_id_id UNIQUE (organization_id, id);

ALTER TABLE ONLY organization.clinic_holidays
    ADD CONSTRAINT pk_clinic_holidays PRIMARY KEY (id);

ALTER TABLE ONLY organization.clinic_locations
    ADD CONSTRAINT pk_clinic_locations PRIMARY KEY (id);

ALTER TABLE ONLY organization.clinics
    ADD CONSTRAINT pk_clinics PRIMARY KEY (id);

ALTER TABLE ONLY organization.organization_memberships
    ADD CONSTRAINT pk_organization_memberships PRIMARY KEY (id);

ALTER TABLE ONLY organization.organization_settings
    ADD CONSTRAINT pk_organization_settings PRIMARY KEY (organization_id);

ALTER TABLE ONLY organization.organizations
    ADD CONSTRAINT pk_organizations PRIMARY KEY (id);

ALTER TABLE ONLY organization.staff_invitations
    ADD CONSTRAINT pk_staff_invitations PRIMARY KEY (id);

ALTER TABLE ONLY patient.patients
    ADD CONSTRAINT ak_patients_organization_id_id UNIQUE (organization_id, id);

ALTER TABLE ONLY patient.emergency_contacts
    ADD CONSTRAINT pk_emergency_contacts PRIMARY KEY (id);

ALTER TABLE ONLY patient.patient_account_links
    ADD CONSTRAINT pk_patient_account_links PRIMARY KEY (id);

ALTER TABLE ONLY patient.patient_consents
    ADD CONSTRAINT pk_patient_consents PRIMARY KEY (id);

ALTER TABLE ONLY patient.patient_provider_relationships
    ADD CONSTRAINT pk_patient_provider_relationships PRIMARY KEY (id);

ALTER TABLE ONLY patient.patients
    ADD CONSTRAINT pk_patients PRIMARY KEY (id);

ALTER TABLE ONLY platform.audit_events
    ADD CONSTRAINT pk_audit_events PRIMARY KEY (id);

ALTER TABLE ONLY platform.idempotency_records
    ADD CONSTRAINT pk_idempotency_records PRIMARY KEY (id);

ALTER TABLE ONLY platform.outbox_events
    ADD CONSTRAINT pk_outbox_events PRIMARY KEY (id);

ALTER TABLE ONLY provider.providers
    ADD CONSTRAINT ak_providers_organization_id_id UNIQUE (organization_id, id);

ALTER TABLE ONLY provider.provider_credentials
    ADD CONSTRAINT pk_provider_credentials PRIMARY KEY (id);

ALTER TABLE ONLY provider.provider_locations
    ADD CONSTRAINT pk_provider_locations PRIMARY KEY (provider_id, clinic_location_id);

ALTER TABLE ONLY provider.provider_schedules
    ADD CONSTRAINT pk_provider_schedules PRIMARY KEY (id);

ALTER TABLE ONLY provider.provider_specialties
    ADD CONSTRAINT pk_provider_specialties PRIMARY KEY (id);

ALTER TABLE ONLY provider.providers
    ADD CONSTRAINT pk_providers PRIMARY KEY (id);

ALTER TABLE ONLY provider.schedule_exceptions
    ADD CONSTRAINT pk_schedule_exceptions PRIMARY KEY (id);

ALTER TABLE ONLY scheduling.appointments
    ADD CONSTRAINT ak_appointments_organization_id_id UNIQUE (organization_id, id);

ALTER TABLE ONLY scheduling.appointment_reminders
    ADD CONSTRAINT pk_appointment_reminders PRIMARY KEY (id);

ALTER TABLE ONLY scheduling.appointment_status_history
    ADD CONSTRAINT pk_appointment_status_history PRIMARY KEY (id);

ALTER TABLE ONLY scheduling.appointments
    ADD CONSTRAINT pk_appointments PRIMARY KEY (id);

ALTER TABLE ONLY scheduling.waiting_list_entries
    ADD CONSTRAINT pk_waiting_list_entries PRIMARY KEY (id);

-- ---------------------------------------------------------------------------------------------
-- Secondary indexes
-- ---------------------------------------------------------------------------------------------

CREATE INDEX ix_mfa_factors_user ON identity.mfa_factors USING btree (user_id);

CREATE INDEX ix_sessions_expires_at ON identity.sessions USING btree (expires_at);

CREATE INDEX ix_sessions_previous_refresh_token_hash ON identity.sessions USING btree (previous_refresh_token_hash);

CREATE INDEX ix_sessions_user_revoked ON identity.sessions USING btree (user_id, revoked_at);

CREATE INDEX ix_user_tokens_expires_at ON identity.user_tokens USING btree (expires_at);

CREATE INDEX ix_user_tokens_user_purpose ON identity.user_tokens USING btree (user_id, purpose);

CREATE UNIQUE INDEX ux_mfa_recovery_codes_user_code ON identity.mfa_recovery_codes USING btree (user_id, code_hash);

CREATE UNIQUE INDEX ux_sessions_refresh_token_hash ON identity.sessions USING btree (refresh_token_hash);

CREATE UNIQUE INDEX ux_user_tokens_token_hash ON identity.user_tokens USING btree (token_hash);

CREATE UNIQUE INDEX ux_users_email_normalized ON identity.users USING btree (email_normalized);

CREATE INDEX ix_clinic_locations_organization_clinic ON organization.clinic_locations USING btree (organization_id, clinic_id);

CREATE INDEX ix_clinic_locations_state_city ON organization.clinic_locations USING btree (state, city);

CREATE INDEX ix_clinics_organization_name ON organization.clinics USING btree (organization_id, name);

CREATE INDEX ix_memberships_user ON organization.organization_memberships USING btree (user_id);

CREATE INDEX ix_staff_invitations_invited_by_user_id ON organization.staff_invitations USING btree (invited_by_user_id);

CREATE INDEX ix_staff_invitations_organization_email ON organization.staff_invitations USING btree (organization_id, email_normalized);

CREATE UNIQUE INDEX ux_clinic_holidays_scope_date ON organization.clinic_holidays USING btree (organization_id, clinic_location_id, date) NULLS NOT DISTINCT;

CREATE UNIQUE INDEX ux_memberships_organization_user ON organization.organization_memberships USING btree (organization_id, user_id);

CREATE UNIQUE INDEX ux_organizations_slug ON organization.organizations USING btree (slug);

CREATE UNIQUE INDEX ux_staff_invitations_token_hash ON organization.staff_invitations USING btree (token_hash);

CREATE INDEX ix_care_relationships_patient ON patient.patient_provider_relationships USING btree (organization_id, patient_id);

CREATE INDEX ix_care_relationships_provider_patient ON patient.patient_provider_relationships USING btree (organization_id, provider_id, patient_id);

CREATE INDEX ix_emergency_contacts_patient ON patient.emergency_contacts USING btree (organization_id, patient_id);

CREATE INDEX ix_patient_account_links_organization_id_patient_id ON patient.patient_account_links USING btree (organization_id, patient_id);

CREATE INDEX ix_patient_consents_patient_type ON patient.patient_consents USING btree (organization_id, patient_id, type);

CREATE INDEX ix_patient_links_user ON patient.patient_account_links USING btree (user_id);

CREATE INDEX ix_patients_organization_dob ON patient.patients USING btree (organization_id, date_of_birth);

CREATE INDEX ix_patients_organization_name ON patient.patients USING btree (organization_id, last_name, first_name);

CREATE UNIQUE INDEX ux_patient_links_patient_user ON patient.patient_account_links USING btree (patient_id, user_id);

CREATE UNIQUE INDEX ux_patients_organization_mrn ON patient.patients USING btree (organization_id, medical_record_number);

CREATE INDEX ix_audit_events_actor_time ON platform.audit_events USING btree (actor_user_id, occurred_at);

CREATE INDEX ix_audit_events_organization_time ON platform.audit_events USING btree (organization_id, occurred_at DESC);

CREATE INDEX ix_audit_events_resource ON platform.audit_events USING btree (organization_id, resource_type, resource_id);

CREATE INDEX ix_idempotency_records_expires_at ON platform.idempotency_records USING btree (expires_at);

CREATE INDEX ix_outbox_events_pending ON platform.outbox_events USING btree (available_at) WHERE (processed_at IS NULL);

CREATE INDEX ix_outbox_events_processed_at ON platform.outbox_events USING btree (processed_at);

CREATE UNIQUE INDEX ux_idempotency_records_scope_key ON platform.idempotency_records USING btree (organization_id, user_id, operation, key);

CREATE INDEX ix_provider_credentials_organization_provider ON provider.provider_credentials USING btree (organization_id, provider_id);

CREATE INDEX ix_provider_locations_location ON provider.provider_locations USING btree (clinic_location_id);

CREATE INDEX ix_provider_locations_organization_id_clinic_location_id ON provider.provider_locations USING btree (organization_id, clinic_location_id);

CREATE INDEX ix_provider_locations_organization_id_provider_id ON provider.provider_locations USING btree (organization_id, provider_id);

CREATE INDEX ix_provider_schedules_organization_id_clinic_location_id ON provider.provider_schedules USING btree (organization_id, clinic_location_id);

CREATE INDEX ix_provider_schedules_provider_day ON provider.provider_schedules USING btree (organization_id, provider_id, day_of_week);

CREATE INDEX ix_provider_specialties_organization_id_provider_id ON provider.provider_specialties USING btree (organization_id, provider_id);

CREATE INDEX ix_providers_directory ON provider.providers USING btree (last_name, first_name) WHERE (listed_in_directory AND ((status)::text = 'active'::text));

CREATE INDEX ix_providers_organization_name ON provider.providers USING btree (organization_id, last_name, first_name);

CREATE INDEX ix_providers_user_id ON provider.providers USING btree (user_id);

CREATE INDEX ix_schedule_exceptions_organization_id_clinic_location_id ON provider.schedule_exceptions USING btree (organization_id, clinic_location_id);

CREATE INDEX ix_schedule_exceptions_provider_start ON provider.schedule_exceptions USING btree (organization_id, provider_id, starts_at);

CREATE UNIQUE INDEX ux_provider_specialties_provider_name ON provider.provider_specialties USING btree (provider_id, name);

CREATE UNIQUE INDEX ux_providers_organization_npi ON provider.providers USING btree (organization_id, npi) WHERE (npi IS NOT NULL);

CREATE UNIQUE INDEX ux_providers_organization_user ON provider.providers USING btree (organization_id, user_id) WHERE (user_id IS NOT NULL);

CREATE INDEX ix_appointment_history_appointment ON scheduling.appointment_status_history USING btree (organization_id, appointment_id, changed_at);

CREATE INDEX ix_appointment_reminders_appointment ON scheduling.appointment_reminders USING btree (organization_id, appointment_id);

CREATE INDEX ix_appointment_reminders_due ON scheduling.appointment_reminders USING btree (scheduled_for) WHERE ((status)::text = 'scheduled'::text);

CREATE INDEX ix_appointments_location_start ON scheduling.appointments USING btree (organization_id, clinic_location_id, starts_at);

CREATE INDEX ix_appointments_patient_start ON scheduling.appointments USING btree (organization_id, patient_id, starts_at);

CREATE INDEX ix_appointments_provider_start ON scheduling.appointments USING btree (organization_id, provider_id, starts_at);

CREATE INDEX ix_appointments_status_start ON scheduling.appointments USING btree (organization_id, status, starts_at);

CREATE INDEX ix_waiting_list_entries_organization_id_clinic_location_id ON scheduling.waiting_list_entries USING btree (organization_id, clinic_location_id);

CREATE INDEX ix_waiting_list_entries_organization_id_provider_id ON scheduling.waiting_list_entries USING btree (organization_id, provider_id);

CREATE INDEX ix_waiting_list_patient ON scheduling.waiting_list_entries USING btree (organization_id, patient_id);

CREATE INDEX ix_waiting_list_status_created ON scheduling.waiting_list_entries USING btree (organization_id, status, created_at);

-- ---------------------------------------------------------------------------------------------
-- Foreign keys (composite tenant keys keep references inside one organization)
-- ---------------------------------------------------------------------------------------------

ALTER TABLE ONLY identity.mfa_factors
    ADD CONSTRAINT fk_mfa_factors_users_user_id FOREIGN KEY (user_id) REFERENCES identity.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY identity.mfa_recovery_codes
    ADD CONSTRAINT fk_mfa_recovery_codes_users_user_id FOREIGN KEY (user_id) REFERENCES identity.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY identity.sessions
    ADD CONSTRAINT fk_sessions_users_user_id FOREIGN KEY (user_id) REFERENCES identity.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY identity.user_tokens
    ADD CONSTRAINT fk_user_tokens_users_user_id FOREIGN KEY (user_id) REFERENCES identity.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY organization.clinic_holidays
    ADD CONSTRAINT fk_clinic_holidays_clinic_locations_organization_id_clinic_loc FOREIGN KEY (organization_id, clinic_location_id) REFERENCES organization.clinic_locations(organization_id, id);

ALTER TABLE ONLY organization.clinic_holidays
    ADD CONSTRAINT fk_clinic_holidays_organizations_organization_id FOREIGN KEY (organization_id) REFERENCES organization.organizations(id) ON DELETE RESTRICT;

ALTER TABLE ONLY organization.clinic_locations
    ADD CONSTRAINT fk_clinic_locations_clinics_organization_id_clinic_id FOREIGN KEY (organization_id, clinic_id) REFERENCES organization.clinics(organization_id, id) ON DELETE RESTRICT;

ALTER TABLE ONLY organization.clinics
    ADD CONSTRAINT fk_clinics_organizations_organization_id FOREIGN KEY (organization_id) REFERENCES organization.organizations(id) ON DELETE RESTRICT;

ALTER TABLE ONLY organization.organization_memberships
    ADD CONSTRAINT fk_organization_memberships_organizations_organization_id FOREIGN KEY (organization_id) REFERENCES organization.organizations(id) ON DELETE RESTRICT;

ALTER TABLE ONLY organization.organization_memberships
    ADD CONSTRAINT fk_organization_memberships_users_user_id FOREIGN KEY (user_id) REFERENCES identity.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY organization.organization_settings
    ADD CONSTRAINT fk_organization_settings_organizations_organization_id FOREIGN KEY (organization_id) REFERENCES organization.organizations(id) ON DELETE RESTRICT;

ALTER TABLE ONLY organization.staff_invitations
    ADD CONSTRAINT fk_staff_invitations_organizations_organization_id FOREIGN KEY (organization_id) REFERENCES organization.organizations(id) ON DELETE RESTRICT;

ALTER TABLE ONLY organization.staff_invitations
    ADD CONSTRAINT fk_staff_invitations_users_invited_by_user_id FOREIGN KEY (invited_by_user_id) REFERENCES identity.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY patient.emergency_contacts
    ADD CONSTRAINT fk_emergency_contacts_patients_organization_id_patient_id FOREIGN KEY (organization_id, patient_id) REFERENCES patient.patients(organization_id, id) ON DELETE RESTRICT;

ALTER TABLE ONLY patient.patient_account_links
    ADD CONSTRAINT fk_patient_account_links_patients_organization_id_patient_id FOREIGN KEY (organization_id, patient_id) REFERENCES patient.patients(organization_id, id) ON DELETE RESTRICT;

ALTER TABLE ONLY patient.patient_account_links
    ADD CONSTRAINT fk_patient_account_links_users_user_id FOREIGN KEY (user_id) REFERENCES identity.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY patient.patient_consents
    ADD CONSTRAINT fk_patient_consents_patients_organization_id_patient_id FOREIGN KEY (organization_id, patient_id) REFERENCES patient.patients(organization_id, id) ON DELETE RESTRICT;

ALTER TABLE ONLY patient.patient_provider_relationships
    ADD CONSTRAINT fk_patient_provider_relationships_patients_organization_id_pat FOREIGN KEY (organization_id, patient_id) REFERENCES patient.patients(organization_id, id) ON DELETE RESTRICT;

ALTER TABLE ONLY patient.patient_provider_relationships
    ADD CONSTRAINT fk_patient_provider_relationships_providers_organization_id_pr FOREIGN KEY (organization_id, provider_id) REFERENCES provider.providers(organization_id, id) ON DELETE RESTRICT;

ALTER TABLE ONLY patient.patients
    ADD CONSTRAINT fk_patients_organizations_organization_id FOREIGN KEY (organization_id) REFERENCES organization.organizations(id) ON DELETE RESTRICT;

ALTER TABLE ONLY platform.idempotency_records
    ADD CONSTRAINT fk_idempotency_records_organizations_organization_id FOREIGN KEY (organization_id) REFERENCES organization.organizations(id) ON DELETE RESTRICT;

ALTER TABLE ONLY provider.provider_credentials
    ADD CONSTRAINT fk_provider_credentials_providers_organization_id_provider_id FOREIGN KEY (organization_id, provider_id) REFERENCES provider.providers(organization_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY provider.provider_locations
    ADD CONSTRAINT fk_provider_locations_clinic_locations_organization_id_clinic_ FOREIGN KEY (organization_id, clinic_location_id) REFERENCES organization.clinic_locations(organization_id, id) ON DELETE RESTRICT;

ALTER TABLE ONLY provider.provider_locations
    ADD CONSTRAINT fk_provider_locations_providers_organization_id_provider_id FOREIGN KEY (organization_id, provider_id) REFERENCES provider.providers(organization_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY provider.provider_schedules
    ADD CONSTRAINT fk_provider_schedules_clinic_locations_organization_id_clinic_ FOREIGN KEY (organization_id, clinic_location_id) REFERENCES organization.clinic_locations(organization_id, id) ON DELETE RESTRICT;

ALTER TABLE ONLY provider.provider_schedules
    ADD CONSTRAINT fk_provider_schedules_providers_organization_id_provider_id FOREIGN KEY (organization_id, provider_id) REFERENCES provider.providers(organization_id, id) ON DELETE RESTRICT;

ALTER TABLE ONLY provider.provider_specialties
    ADD CONSTRAINT fk_provider_specialties_providers_organization_id_provider_id FOREIGN KEY (organization_id, provider_id) REFERENCES provider.providers(organization_id, id) ON DELETE CASCADE;

ALTER TABLE ONLY provider.providers
    ADD CONSTRAINT fk_providers_organizations_organization_id FOREIGN KEY (organization_id) REFERENCES organization.organizations(id) ON DELETE RESTRICT;

ALTER TABLE ONLY provider.providers
    ADD CONSTRAINT fk_providers_users_user_id FOREIGN KEY (user_id) REFERENCES identity.users(id);

ALTER TABLE ONLY provider.schedule_exceptions
    ADD CONSTRAINT fk_schedule_exceptions_clinic_locations_organization_id_clinic FOREIGN KEY (organization_id, clinic_location_id) REFERENCES organization.clinic_locations(organization_id, id);

ALTER TABLE ONLY provider.schedule_exceptions
    ADD CONSTRAINT fk_schedule_exceptions_providers_organization_id_provider_id FOREIGN KEY (organization_id, provider_id) REFERENCES provider.providers(organization_id, id) ON DELETE RESTRICT;

ALTER TABLE ONLY scheduling.appointment_reminders
    ADD CONSTRAINT fk_appointment_reminders_appointments_organization_id_appointm FOREIGN KEY (organization_id, appointment_id) REFERENCES scheduling.appointments(organization_id, id) ON DELETE RESTRICT;

ALTER TABLE ONLY scheduling.appointment_status_history
    ADD CONSTRAINT fk_appointment_status_history_appointments_organization_id_app FOREIGN KEY (organization_id, appointment_id) REFERENCES scheduling.appointments(organization_id, id) ON DELETE RESTRICT;

ALTER TABLE ONLY scheduling.appointments
    ADD CONSTRAINT fk_appointments_clinic_locations_organization_id_clinic_locati FOREIGN KEY (organization_id, clinic_location_id) REFERENCES organization.clinic_locations(organization_id, id) ON DELETE RESTRICT;

ALTER TABLE ONLY scheduling.appointments
    ADD CONSTRAINT fk_appointments_patients_organization_id_patient_id FOREIGN KEY (organization_id, patient_id) REFERENCES patient.patients(organization_id, id) ON DELETE RESTRICT;

ALTER TABLE ONLY scheduling.appointments
    ADD CONSTRAINT fk_appointments_providers_organization_id_provider_id FOREIGN KEY (organization_id, provider_id) REFERENCES provider.providers(organization_id, id) ON DELETE RESTRICT;

ALTER TABLE ONLY scheduling.waiting_list_entries
    ADD CONSTRAINT fk_waiting_list_entries_clinic_locations_organization_id_clini FOREIGN KEY (organization_id, clinic_location_id) REFERENCES organization.clinic_locations(organization_id, id);

ALTER TABLE ONLY scheduling.waiting_list_entries
    ADD CONSTRAINT fk_waiting_list_entries_patients_organization_id_patient_id FOREIGN KEY (organization_id, patient_id) REFERENCES patient.patients(organization_id, id) ON DELETE RESTRICT;

ALTER TABLE ONLY scheduling.waiting_list_entries
    ADD CONSTRAINT fk_waiting_list_entries_providers_organization_id_provider_id FOREIGN KEY (organization_id, provider_id) REFERENCES provider.providers(organization_id, id);
