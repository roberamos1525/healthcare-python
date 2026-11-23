"""The composition root: builds every dependency from the settings and wires them explicitly."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from healthcare.application.access import AccessResolver
from healthcare.application.core.ports import Cache, Clock, system_clock
from healthcare.application.identity.service import AuthPolicy, IdentityService
from healthcare.application.organizations.clinics import ClinicService
from healthcare.application.organizations.service import OrganizationService
from healthcare.application.patients.service import PatientService
from healthcare.application.platform import AuditQueryService
from healthcare.application.providers.profiles import ProviderService
from healthcare.application.providers.schedules import ScheduleService
from healthcare.application.scheduling.appointments import AppointmentService
from healthcare.application.scheduling.availability import AvailabilityService
from healthcare.application.scheduling.waiting_list import WaitingListService
from healthcare.infrastructure.cache import MemoryCache, RedisCache
from healthcare.infrastructure.config import Settings
from healthcare.infrastructure.email import EmailWorker, MailQueue, QueueMailer, Sender, create_sender
from healthcare.infrastructure.jobs import Job, build_jobs, run_job
from healthcare.infrastructure.messaging import Publisher, create_publisher
from healthcare.infrastructure.persistence.database import SqlDatabase, create_engine
from healthcare.infrastructure.persistence.stores import AdvisoryProviderLock, SqlIdempotencyStore
from healthcare.infrastructure.security import (
    AesGcmFieldEncryptor,
    JwtService,
    Pbkdf2PasswordHasher,
    SigningKey,
    load_signing_key,
)

_LOGGER = logging.getLogger("healthcare.bootstrap")


@dataclass(slots=True)
class Services:
    """The use cases reached from the HTTP boundary."""

    identity: IdentityService
    organizations: OrganizationService
    clinics: ClinicService
    providers: ProviderService
    schedules: ScheduleService
    patients: PatientService
    availability: AvailabilityService
    appointments: AppointmentService
    waiting_list: WaitingListService
    audit: AuditQueryService


@dataclass(slots=True)
class Container:
    """A fully wired application instance (one per worker process)."""

    settings: Settings
    clock: Clock
    database: SqlDatabase
    cache: Cache
    signing_key: SigningKey
    jwt: JwtService
    access: AccessResolver
    services: Services
    mail_queue: MailQueue
    sender: Sender
    publisher: Publisher
    jobs: list[Job]
    tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def start_workers(self) -> None:
        """Start the email sender and, when enabled, the background jobs as supervised tasks."""
        self.tasks.append(asyncio.create_task(EmailWorker(self.mail_queue, self.sender).run(), name="EmailWorker"))
        if self.settings.workers.enabled:
            for job in self.jobs:
                self.tasks.append(asyncio.create_task(run_job(job), name=job.name))

    async def stop_workers(self) -> None:
        """Cancel the workers and wait until every one of them has stopped."""
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    async def close(self) -> None:
        """Stop the workers and release the broker, the cache client and the connection pool."""
        await self.stop_workers()
        await self.publisher.close()
        close = getattr(self.cache, "close", None)
        if close is not None:
            await close()
        await self.database.dispose()


def build_container(settings: Settings, clock: Clock = system_clock, sender: Sender | None = None) -> Container:
    """Create the application from the settings; tests may replace the clock and the email sender."""
    if not settings.database:
        raise ValueError("ConnectionStrings:Database is not configured.")
    auth = settings.auth
    key = load_signing_key(auth.signing_key_pem, auth.signing_key_path, settings.allows_temporary_keys)
    if key.temporary:
        _LOGGER.warning(
            "No token signing key is configured; using a temporary key. "
            "Tokens become invalid when the process restarts."
        )
    encryptor = AesGcmFieldEncryptor.from_setting(auth.field_encryption_key, settings.allows_temporary_keys)
    if encryptor.temporary:
        _LOGGER.warning(
            "No field encryption key is configured; using a temporary key. "
            "Stored MFA secrets become unreadable after a restart."
        )
    database = SqlDatabase(create_engine(settings.database))
    cache: Cache = RedisCache(settings.redis) if settings.redis.strip() else MemoryCache()
    mail_queue = MailQueue()
    mailer = QueueMailer(mail_queue, settings.app.frontend_base_url)
    publisher = create_publisher(
        settings.messaging.provider, settings.messaging.rabbit_mq_uri, settings.messaging.exchange
    )
    jwt = JwtService(key, auth.issuer, auth.audience, auth.access_token_minutes * 60, clock)
    access = AccessResolver(database, cache, clock)
    policy = AuthPolicy(
        access_token_lifetime=timedelta(minutes=auth.access_token_minutes),
        session_idle_timeout=timedelta(minutes=auth.session_idle_timeout_minutes),
        session_absolute=timedelta(hours=auth.session_absolute_lifetime_hours),
        refresh_reuse_grace=timedelta(seconds=auth.refresh_reuse_grace_seconds),
        max_failed_sign_ins=auth.max_failed_sign_in_attempts,
        lockout=timedelta(minutes=auth.lockout_minutes),
        email_verification_life=timedelta(hours=auth.email_verification_token_hours),
        password_reset_life=timedelta(minutes=auth.password_reset_token_minutes),
        mfa_challenge_life=timedelta(minutes=auth.mfa_challenge_minutes),
        mfa_issuer_name=auth.mfa_issuer_name,
    )
    services = Services(
        identity=IdentityService(
            database, Pbkdf2PasswordHasher(auth.password_hash_iterations), jwt, mailer, encryptor, access, clock, policy
        ),
        organizations=OrganizationService(database, access, mailer, clock, timedelta(days=auth.invitation_days)),
        clinics=ClinicService(database, clock),
        providers=ProviderService(database, access, clock),
        schedules=ScheduleService(database, clock),
        patients=PatientService(database, access, clock),
        availability=AvailabilityService(database, clock),
        appointments=AppointmentService(
            database,
            clock,
            AdvisoryProviderLock(),
            SqlIdempotencyStore(),
            timedelta(hours=settings.app.idempotency_retention_hours),
        ),
        waiting_list=WaitingListService(database, clock),
        audit=AuditQueryService(database),
    )
    return Container(
        settings=settings,
        clock=clock,
        database=database,
        cache=cache,
        signing_key=key,
        jwt=jwt,
        access=access,
        services=services,
        mail_queue=mail_queue,
        sender=sender or create_sender(settings.email),
        publisher=publisher,
        jobs=build_jobs(database, publisher, clock, settings.workers),
    )
