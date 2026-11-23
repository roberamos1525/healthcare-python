-- Creates the two database roles used by the platform. Run once per PostgreSQL instance as a
-- superuser (or the RDS master user) BEFORE applying migrations.
--
--   psql -v migrator_password='...' -v app_password='...' -f deploy/sql/create-roles.sql
--
-- healthcare_migrator owns the schema and runs migrations.
-- healthcare_app is used by the running application: it owns nothing, cannot bypass row-level
-- security and receives only the table privileges granted by the migrations.

SELECT format('CREATE ROLE healthcare_migrator LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS', :'migrator_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'healthcare_migrator')
\gexec

SELECT format('CREATE ROLE healthcare_app LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'healthcare_app')
\gexec

-- Create the database owned by the migrator when it does not exist yet.
SELECT 'CREATE DATABASE healthcare OWNER healthcare_migrator'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'healthcare')
\gexec

\connect healthcare

-- btree_gist backs the exclusion constraint that prevents double booking. Creating an extension
-- needs elevated rights, so it is installed here rather than by the migrator.
CREATE EXTENSION IF NOT EXISTS btree_gist;

GRANT CONNECT ON DATABASE healthcare TO healthcare_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
