-- Tenant isolation, booking integrity and privileges.
-- Applied by the SecurityPoliciesAndConstraints migration as the schema owner.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'healthcare_app') THEN
        RAISE EXCEPTION 'Role "healthcare_app" does not exist. Run deploy/sql/create-roles.sql before applying migrations.';
    END IF;
END $$;

CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ---------------------------------------------------------------------------------------------
-- Session context. The application sets these three settings on every connection it opens.
-- ---------------------------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS app;

CREATE OR REPLACE FUNCTION app.current_organization_id() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$ SELECT nullif(current_setting('app.current_organization_id', true), '')::uuid $$;

CREATE OR REPLACE FUNCTION app.current_user_id() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$ SELECT nullif(current_setting('app.current_user_id', true), '')::uuid $$;

CREATE OR REPLACE FUNCTION app.is_system_scope() RETURNS boolean
    LANGUAGE sql STABLE
    AS $$ SELECT coalesce(current_setting('app.access_scope', true), '') = 'system' $$;

-- ---------------------------------------------------------------------------------------------
-- Double-booking protection: a provider cannot have two overlapping appointments that occupy time.
-- Cancelled and no-show appointments free their slot.
-- ---------------------------------------------------------------------------------------------
ALTER TABLE scheduling.appointments
    ADD CONSTRAINT ex_appointments_provider_no_overlap
    EXCLUDE USING gist (provider_id WITH =, tstzrange(starts_at, ends_at, '[)') WITH &&)
    WHERE (status NOT IN ('cancelled', 'no_show'));

-- Case-insensitive prefix search on names.
CREATE INDEX ix_patients_last_name_lower ON patient.patients (organization_id, lower(last_name) text_pattern_ops);
CREATE INDEX ix_patients_first_name_lower ON patient.patients (organization_id, lower(first_name) text_pattern_ops);
CREATE INDEX ix_providers_last_name_lower ON provider.providers (organization_id, lower(last_name) text_pattern_ops);

-- ---------------------------------------------------------------------------------------------
-- Row-level security. FORCE applies the policies to the table owner as well.
-- ---------------------------------------------------------------------------------------------
DO $$
DECLARE
    tenant_table text;
BEGIN
    FOREACH tenant_table IN ARRAY ARRAY[
        'organization.organization_settings',
        'organization.organization_memberships',
        'organization.staff_invitations',
        'organization.clinics',
        'organization.clinic_locations',
        'organization.clinic_holidays',
        'provider.providers',
        'provider.provider_specialties',
        'provider.provider_credentials',
        'provider.provider_locations',
        'provider.provider_schedules',
        'provider.schedule_exceptions',
        'patient.patients',
        'patient.patient_account_links',
        'patient.emergency_contacts',
        'patient.patient_provider_relationships',
        'patient.patient_consents',
        'scheduling.appointments',
        'scheduling.appointment_status_history',
        'scheduling.appointment_reminders',
        'scheduling.waiting_list_entries',
        'platform.idempotency_records'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', tenant_table);
        EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', tenant_table);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %s '
            'USING (organization_id = app.current_organization_id() OR app.is_system_scope()) '
            'WITH CHECK (organization_id = app.current_organization_id() OR app.is_system_scope())',
            tenant_table);
    END LOOP;
END $$;

-- Users can always read their own memberships and patient links, in any organization.
CREATE POLICY own_memberships ON organization.organization_memberships
    FOR SELECT USING (user_id = app.current_user_id());

CREATE POLICY own_patient_links ON patient.patient_account_links
    FOR SELECT USING (user_id = app.current_user_id());

-- Organizations: full access for the selected organization, read access for members and linked patients.
ALTER TABLE organization.organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE organization.organizations FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON organization.organizations
    USING (id = app.current_organization_id() OR app.is_system_scope())
    WITH CHECK (id = app.current_organization_id() OR app.is_system_scope());

CREATE POLICY member_read ON organization.organizations
    FOR SELECT USING (
        id IN (SELECT m.organization_id FROM organization.organization_memberships m WHERE m.user_id = app.current_user_id())
        OR id IN (SELECT l.organization_id FROM patient.patient_account_links l WHERE l.user_id = app.current_user_id()));

-- Audit events: anyone may append; only the owning organization (or system scope) may read.
ALTER TABLE platform.audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform.audit_events FORCE ROW LEVEL SECURITY;

CREATE POLICY audit_append ON platform.audit_events
    FOR INSERT WITH CHECK (
        organization_id IS NULL OR organization_id = app.current_organization_id() OR app.is_system_scope());

CREATE POLICY audit_read ON platform.audit_events
    FOR SELECT USING (organization_id = app.current_organization_id() OR app.is_system_scope());

-- Outbox: requests may append events for their organization; only the dispatcher (system scope) reads and updates.
ALTER TABLE platform.outbox_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform.outbox_events FORCE ROW LEVEL SECURITY;

CREATE POLICY outbox_append ON platform.outbox_events
    FOR INSERT WITH CHECK (
        organization_id IS NULL OR organization_id = app.current_organization_id() OR app.is_system_scope());

CREATE POLICY outbox_dispatch ON platform.outbox_events
    USING (app.is_system_scope())
    WITH CHECK (app.is_system_scope());

-- ---------------------------------------------------------------------------------------------
-- Privileges of the application role. It owns nothing and cannot bypass row-level security.
-- ---------------------------------------------------------------------------------------------
GRANT USAGE ON SCHEMA app, identity, organization, provider, patient, scheduling, platform TO healthcare_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA app TO healthcare_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA identity, organization, provider, patient, scheduling
    TO healthcare_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON platform.idempotency_records, platform.outbox_events TO healthcare_app;

-- The audit trail and appointment history are append-only.
GRANT SELECT, INSERT ON platform.audit_events TO healthcare_app;
REVOKE UPDATE, DELETE ON scheduling.appointment_status_history FROM healthcare_app;

-- Clinical and consent records are never hard-deleted by the application.
REVOKE DELETE ON patient.patients, patient.patient_consents, scheduling.appointments FROM healthcare_app;
