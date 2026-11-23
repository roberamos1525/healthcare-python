-- Reverts V1: removes the six schemas of the platform and everything in them.

DROP SCHEMA IF EXISTS scheduling CASCADE;
DROP SCHEMA IF EXISTS patient CASCADE;
DROP SCHEMA IF EXISTS provider CASCADE;
DROP SCHEMA IF EXISTS organization CASCADE;
DROP SCHEMA IF EXISTS identity CASCADE;
DROP TABLE IF EXISTS platform.audit_events, platform.idempotency_records, platform.outbox_events;
