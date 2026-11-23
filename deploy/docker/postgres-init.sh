#!/bin/sh
# First-start initialization for the local PostgreSQL container: creates the platform roles and database.
set -eu

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname postgres \
     -v migrator_password="$HEALTHCARE_MIGRATOR_PASSWORD" \
     -v app_password="$HEALTHCARE_APP_PASSWORD" \
     -f /docker-entrypoint-initdb.d/sql/create-roles.sql
