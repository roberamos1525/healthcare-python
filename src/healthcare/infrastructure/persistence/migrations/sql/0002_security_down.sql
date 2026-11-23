-- Reverts the SecurityPoliciesAndConstraints migration.

REVOKE ALL ON ALL TABLES IN SCHEMA identity, organization, provider, patient, scheduling, platform FROM healthcare_app;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app FROM healthcare_app;
REVOKE USAGE ON SCHEMA app, identity, organization, provider, patient, scheduling, platform FROM healthcare_app;

DO $$
DECLARE
    policy record;
BEGIN
    FOR policy IN
        SELECT schemaname, tablename, policyname
        FROM pg_policies
        WHERE schemaname IN ('identity', 'organization', 'provider', 'patient', 'scheduling', 'platform')
    LOOP
        EXECUTE format('DROP POLICY %I ON %I.%I', policy.policyname, policy.schemaname, policy.tablename);
    END LOOP;

    FOR policy IN
        SELECT n.nspname AS schemaname, c.relname AS tablename, '' AS policyname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relrowsecurity AND n.nspname IN ('identity', 'organization', 'provider', 'patient', 'scheduling', 'platform')
    LOOP
        EXECUTE format('ALTER TABLE %I.%I NO FORCE ROW LEVEL SECURITY', policy.schemaname, policy.tablename);
        EXECUTE format('ALTER TABLE %I.%I DISABLE ROW LEVEL SECURITY', policy.schemaname, policy.tablename);
    END LOOP;
END $$;

DROP INDEX IF EXISTS provider.ix_providers_last_name_lower;
DROP INDEX IF EXISTS patient.ix_patients_first_name_lower;
DROP INDEX IF EXISTS patient.ix_patients_last_name_lower;

ALTER TABLE scheduling.appointments DROP CONSTRAINT IF EXISTS ex_appointments_provider_no_overlap;

DROP FUNCTION IF EXISTS app.is_system_scope();
DROP FUNCTION IF EXISTS app.current_user_id();
DROP FUNCTION IF EXISTS app.current_organization_id();
DROP SCHEMA IF EXISTS app;
