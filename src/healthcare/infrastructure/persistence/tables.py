"""SQLAlchemy Core descriptions of the tables written with Core statements.

These are query descriptions only; the schema itself is created by the reviewed SQL migrations.
"""

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData()

idempotency_records = Table(
    "idempotency_records",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("organization_id", UUID(as_uuid=True), nullable=False),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("operation", String(100), nullable=False),
    Column("key", String(128), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("response_body", JSONB),
    Column("resource_id", UUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    schema="platform",
)

outbox_events = Table(
    "outbox_events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("organization_id", UUID(as_uuid=True)),
    Column("event_type", String(100), nullable=False),
    Column("aggregate_type", String(100), nullable=False),
    Column("aggregate_id", UUID(as_uuid=True), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("processed_at", DateTime(timezone=True)),
    Column("attempts", Integer, nullable=False),
    Column("last_error", Text),
    schema="platform",
)

appointment_reminders = Table(
    "appointment_reminders",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("organization_id", UUID(as_uuid=True), nullable=False),
    Column("appointment_id", UUID(as_uuid=True), nullable=False),
    Column("channel", String(20), nullable=False),
    Column("scheduled_for", DateTime(timezone=True), nullable=False),
    Column("status", String(20), nullable=False),
    Column("queued_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    schema="scheduling",
)

appointments = Table(
    "appointments",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("organization_id", UUID(as_uuid=True), nullable=False),
    Column("status", String(20), nullable=False),
    Column("starts_at", DateTime(timezone=True), nullable=False),
    schema="scheduling",
)
