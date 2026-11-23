# HealthCare Platform — Python Backend

A multi-tenant practice management API for US healthcare organizations: accounts with MFA, organizations,
clinics and locations, providers with credential verification and weekly schedules, patients with
consents and minimum-necessary access, availability, concurrency-safe appointment booking with
idempotency, a waiting list, reminders, an audit trail and a transactional outbox.

This is the Python implementation of the platform. The .NET project (`HealthCare-C#`) is the reference
implementation: this backend serves the same API contract (`docs/openapi.yaml`), uses the same
PostgreSQL schema and security policies, and is interchangeable with it and with the Java, Rust and Go
backends against the same database and Redis (password hashes, tokens, idempotency hashes, stored
responses, outbox payloads and cache entries use the same formats).

> **Compliance notice.** The platform is designed with HIPAA's technical safeguards in mind (tenant
> isolation enforced by the database, audit trail without PHI, encryption of secrets, short-lived tokens,
> no PHI in logs, URLs or events), but software alone does not make an organization compliant. Before
> real protected health information is processed, a risk analysis, policies, Business Associate
> Agreements with every hosting and service provider, and an operational security programme are
> required. Only synthetic data (`@example.test` addresses) is used in this repository. See
> [docs/security-and-compliance.md](docs/security-and-compliance.md).

## Status

- **Implemented:** every operation of the contract (95 operations, under `/api/v1`, plus `/health/live`,
  `/health/ready`, `/.well-known/jwks.json` and `/openapi/v1.yaml`), all business rules of the
  reference, background jobs (outbox dispatch, reminder dispatch, maintenance, email sending), Redis
  cache with in-memory fallback, RabbitMQ or log publishing, JSON logging, OpenTelemetry traces over OTLP,
  graceful shutdown, Alembic migrations with baseline of a schema created by another backend.
- **Verified on the development workstation (Windows 11, local PostgreSQL 17.6, Redis 5):** 345 unit
  tests and 91 integration tests pass; `ruff` and `mypy --strict` are clean for `src` and `tests`; the
  migrated schema is identical (`pg_dump --schema-only`) to one migrated by the reference; a black-box
  comparison of 223 request/response exchanges against the reference found one remaining difference, a
  reference defect (`docs/troubleshooting.md`, section 4); the k6 smoke, contention, baseline and stress
  runs pass (numbers in [docs/testing.md](docs/testing.md)).
- **Not verified here:** Docker Compose, the container image and the CI workflow (no Docker on the
  workstation; the workflow has not run on a hosted runner), RabbitMQ publishing (no broker locally; the
  log publisher was used), OTLP export to a collector. Nothing is deployed.

## Technology

| Area | Choice (version resolved in `uv.lock`) |
|---|---|
| Language | Python 3.14.10 (CPython), managed with uv 0.13.9 |
| HTTP | FastAPI 0.165 on Starlette 1.10, served by Uvicorn 0.54 (httptools) with worker processes |
| Models and configuration | Pydantic 2.14 (contract models), pydantic-settings 2.16 |
| Database | PostgreSQL 17, SQLAlchemy 2.1 (asyncio, Core) with asyncpg 0.32, Alembic 1.20 with reviewed SQL |
| Security | PyJWT 2.15 (RS256), cryptography 50 (RSA, AES-256-GCM), `hashlib.pbkdf2_hmac`, RFC 6238 TOTP |
| Cache | redis-py 8.1 (`redis.asyncio`, RESP2), in-memory fallback |
| Email and events | aiosmtplib 5.1, aio-pika 10.2 (RabbitMQ) |
| Telemetry | Standard-library JSON logging, OpenTelemetry SDK 1.45 with the OTLP/HTTP exporter |
| Time zones | `zoneinfo` with the `tzdata` package (2026.6) |
| Tests and quality | pytest 9.2, pytest-asyncio 1.5, httpx 0.28, ruff 0.17, mypy 2.4 (strict), k6 2.4 |

## Layout

```
src/healthcare/
  __main__.py            python -m healthcare [serve|migrate]
  domain/                enums, permission matrix, state machine and rules, calendar, slot generator
  application/
    core/                errors, Caller, ports, Pydantic base models, validation, paging, reference JSON, audit/outbox
    access.py            session check and tenant access resolution with caching
    identity/            accounts, sessions, MFA        organizations/  organizations, members, clinics
    providers/           profiles, credentials, schedules  patients/   patients, contacts, consents
    scheduling/          availability, appointments, waiting list   platform.py   audit trail
    security.py          token formats, recovery codes, TOTP
  infrastructure/
    config.py            pydantic-settings over Section__Key environment variables
    bootstrap.py         composition root (explicit wiring)
    persistence/         transaction wrapper (RLS scope), stores (advisory lock, idempotency), migrations (Alembic + SQL)
    security.py cache.py email.py messaging.py jobs.py telemetry.py
  api/
    app.py               FastAPI app, lifespan, operational endpoints     routes.py  all /api/v1 operations
    pipeline.py          outer ASGI middleware (trace, headers, CORS, logs)  context.py  auth pipeline, rate limits
    binding.py           three-stage body binder      query.py  query binding      routing.py  routing tree, 404/405
    responses.py         JSON and problem documents   openapi.yaml  copy of docs/openapi.yaml
tests/unit/              domain, slots, security, binding, core, scheduling, infrastructure
tests/integration/       auth, organizations, providers/patients, appointments, tenant isolation, jobs,
                         migrations, HTTP behaviour, contract
perf/                    k6 scripts (copied from the reference), runner, resource sampler, results
deploy/                  create-roles.sql (copy), postgres-init.sh for Compose
docs/                    see the documentation index below
```

## Quick start

### Without Docker (local PostgreSQL) — the commands used on the development workstation

PowerShell, PostgreSQL on `localhost:54329` with the roles `healthcare_migrator` and `healthcare_app`
(create them once with `psql -v migrator_password=... -v app_password=... -f deploy/sql/create-roles.sql`).

```powershell
# 1. Python 3.14 and the locked dependencies (creates .venv)
uv sync

# 2. A database for this backend, owned by the schema owner, with btree_gist (once)
& "$env:LOCALAPPDATA\HealthCareDev\pgsql\bin\psql.exe" -h localhost -p 54329 -U postgres -c 'CREATE DATABASE "healthcare_python" OWNER healthcare_migrator'
& "$env:LOCALAPPDATA\HealthCareDev\pgsql\bin\psql.exe" -h localhost -p 54329 -U postgres -d healthcare_python -c 'CREATE EXTENSION IF NOT EXISTS btree_gist'

# 3. Migrate as the schema owner
$env:ConnectionStrings__Migrations = "Host=localhost;Port=54329;Database=healthcare_python;Username=healthcare_migrator;Password=change-me"
.venv\Scripts\python.exe -m healthcare migrate

# 4. Run as the restricted application role (Development: temporary keys, email to .\mail-pickup)
$env:ASPNETCORE_ENVIRONMENT = "Development"
$env:ConnectionStrings__Database = "Host=localhost;Port=54329;Database=healthcare_python;Username=healthcare_app;Password=change-me"
$env:ConnectionStrings__Redis = "127.0.0.1:6379"   # optional; omit for the in-memory cache
.venv\Scripts\python.exe -m healthcare             # http://127.0.0.1:8080, Ctrl+C stops it gracefully
```

Use `127.0.0.1` rather than `localhost` for services that listen on IPv4 only on Windows (see
[docs/troubleshooting.md](docs/troubleshooting.md), 1.4). For several worker processes set
`HEALTHCARE_WORKERS` (each worker has its own connection pool).

### With Docker Compose

```bash
cp .env.example .env     # then change every password
docker compose up --build
# API http://localhost:8080, Mailpit http://localhost:8025, RabbitMQ http://localhost:15672
```

The `migrate` service applies the migrations with the same image and exits before the API starts.

### First requests

```bash
curl -i http://127.0.0.1:8080/health/ready
curl -i -X POST http://127.0.0.1:8080/api/v1/auth/register -H "Content-Type: application/json" \
  -d '{"email":"jane.doe@example.test","password":"Correct-Horse-42","firstName":"Jane","lastName":"Doe"}'
# Open the verification email (Mailpit or ./mail-pickup), then:
curl -i -X POST http://127.0.0.1:8080/api/v1/auth/verify-email -H "Content-Type: application/json" -d '{"token":"..."}'
curl -i -X POST http://127.0.0.1:8080/api/v1/auth/login -H "Content-Type: application/json" \
  -d '{"email":"jane.doe@example.test","password":"Correct-Horse-42"}'
```

## Configuration

All settings are environment variables with the reference's keys (`Section__Key`, matched
case-insensitively; arrays as `Section__Key__0`, `__1`, … or a comma-separated value), validated once at
startup by a pydantic-settings model. There are no configuration files. **Secrets must come from the
environment or a secret manager — never from the repository.**

| Environment variable | Default | Secret | Purpose |
|---|---|:-:|---|
| `ASPNETCORE_ENVIRONMENT` (or `HEALTHCARE_ENVIRONMENT`) | `Production` | | `Development`/`Testing` allow temporary keys; Development also defaults email to `File` and CORS to `http://localhost:5173` |
| `ASPNETCORE_HTTP_PORTS` (or `HTTP_PORTS`, `PORT`, `ASPNETCORE_URLS`) | 8080 | | Listening port |
| `HEALTHCARE_WORKERS` | 1 (2 in the image) | | Uvicorn worker processes |
| `ConnectionStrings__Database` | — (required) | ✔ | Connection as `healthcare_app` (`Host=…;Port=…;Database=…;Username=…;Password=…;SSL Mode=…` or a `postgresql://` URL). `Maximum Pool Size=` per worker process (default 100) |
| `ConnectionStrings__Migrations` | — (required for `migrate`) | ✔ | Connection as `healthcare_migrator` |
| `ConnectionStrings__Redis` | empty → in-memory cache | ✔ | `host:port[,password=…][,ssl=true][,defaultDatabase=n]` or a `redis://` URL; required with more than one worker or instance |
| `Auth__Issuer` / `Auth__Audience` | `healthcare-platform` / `healthcare-api` | | JWT issuer and audience |
| `Auth__SigningKeyPem` / `Auth__SigningKeyPath` | empty | ✔ | RSA private key (PEM, PKCS#8 or PKCS#1) for RS256; required outside Development/Testing |
| `Auth__FieldEncryptionKey` | empty | ✔ | Base64 256-bit key for AES-256-GCM (MFA secrets); required outside Development/Testing |
| `Auth__AccessTokenMinutes` | 10 | | Access-token lifetime |
| `Auth__SessionIdleTimeoutMinutes` / `Auth__SessionAbsoluteLifetimeHours` | 60 / 12 | | Session limits |
| `Auth__MaxFailedSignInAttempts` / `Auth__LockoutMinutes` | 5 / 15 | | Lockout |
| `Auth__PasswordHashIterations` | 600000 | | PBKDF2-HMAC-SHA256 iterations |
| `Auth__MfaIssuerName` | `HealthCare Platform` | | Label in authenticator apps |
| `App__FrontendBaseUrl` | `http://localhost:5173` | | Base URL for links in emails |
| `App__IdempotencyRetentionHours` | 48 | | How long idempotency keys are remembered |
| `Email__Provider` | `None` (`File` in Development) | | `Smtp`, `File` or `None` |
| `Email__FromAddress` / `Email__FromName` | `no-reply@healthcare.localhost` / `HealthCare Platform` | | Sender |
| `Email__SmtpHost` / `Email__SmtpPort` / `Email__SmtpUseStartTls` | `localhost` / 1025 / false | | SMTP relay |
| `Email__SmtpUsername` / `Email__SmtpPassword` | not set | ✔ | SMTP credentials |
| `Email__PickupDirectory` | `mail-pickup` | | Folder for the `File` provider |
| `Messaging__Provider` / `Messaging__RabbitMqUri` / `Messaging__Exchange` | `Log` / empty / `healthcare.events` | ✔ (URI) | Event publication |
| `Workers__Enabled` | true | | Run outbox, reminder and maintenance jobs in every worker process |
| `Workers__OutboxPollSeconds` / `Workers__ReminderPollSeconds` / `Workers__MaintenanceIntervalMinutes` | 2 / 30 / 60 | | Job intervals |
| `Workers__BatchSize` | 50 | | Rows claimed per iteration |
| `RateLimiting__Enabled` / `RateLimiting__GeneralPerMinute` / `RateLimiting__AuthPerMinute` | true / 600 / 20 | | Limits per worker process |
| `Cors__AllowedOrigins__0`, `__1`, … | none (`http://localhost:5173` in Development) | | Browser origins |
| `ForwardedHeaders__Enabled` | false | | Trust `X-Forwarded-For/Proto` behind a trusted load balancer |
| `Database__MigrateOnStartup` | false | | Apply migrations before serving (development only) |
| `Serilog__MinimumLevel__Default` | `Information` | | Log level (`Logging__LogLevel__Default` also works) |
| `HostOptions__ShutdownTimeoutSeconds` | 30 | | Time allowed for in-flight requests on shutdown |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | not set | | Enables OTLP/HTTP export of traces |

Less common settings with the reference defaults: `Auth__RefreshReuseGraceSeconds` 10,
`Auth__EmailVerificationTokenHours` 24, `Auth__PasswordResetTokenMinutes` 60,
`Auth__MfaChallengeMinutes` 5, `Auth__InvitationDays` 7, `Workers__OutboxRetentionDays` 14,
`Workers__ExpiredCredentialRetentionDays` 30.

## Tests

```powershell
# Unit tests (no external dependencies)
.venv\Scripts\python.exe -m pytest tests/unit -q

# Integration, security, tenant-isolation, job, migration, HTTP and contract tests against a PostgreSQL
# server (administrator connection string). The session fixture creates the two roles if missing, creates a
# fresh database healthcare_test_<random>, applies this implementation's migrations, serves the API on a
# random local port and drops the database afterwards. Without the variable it starts postgres:17-alpine
# with Testcontainers (`uv sync --group containers`; needs Docker). HEALTHCARE_TEST_REDIS runs the suite
# against Redis instead of the memory cache.
$env:HEALTHCARE_TEST_POSTGRES = "Host=localhost;Port=54329;Username=postgres;Database=postgres"
.venv\Scripts\python.exe -m pytest -q

# Static checks
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m ruff format --check src tests
.venv\Scripts\python.exe -m mypy --strict src tests

# k6 (production-style server with worker processes; Mailpit on 127.0.0.1:8025/1025)
powershell -File perf\run-measured.ps1 -Name smoke -Script smoke.js
```

Details, counts and the measured load-test numbers: [docs/testing.md](docs/testing.md).

## Documentation

| Document | Contents |
|---|---|
| [docs/product-requirements.md](docs/product-requirements.md) | Product scope (shared by all backends) |
| [docs/architecture.md](docs/architecture.md) | Structure, tenancy, booking, jobs, key decisions, **Python-specific design decisions** |
| [docs/api.md](docs/api.md) / [docs/openapi.yaml](docs/openapi.yaml) | The API and the contract (copied unchanged) |
| [docs/database-schema.md](docs/database-schema.md) | Shared schema, row-level security, migrations with Alembic |
| [docs/security-and-compliance.md](docs/security-and-compliance.md) | Threat model, HIPAA safeguards mapping |
| [docs/capacity-model.md](docs/capacity-model.md) | Sizing, plus this implementation's measurements |
| [docs/deployment.md](docs/deployment.md) | Building and running this backend, AWS reference design, operations |
| [docs/testing.md](docs/testing.md) | Test suites and measured results |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Problems actually hit and how they were solved |
| [docs/hardest-challenge.md](docs/hardest-challenge.md) | Making FastAPI and Pydantic answer exactly like the reference |

## Deployment

One container image (`Dockerfile`, multi-stage, non-root) serves the API and, started with `migrate`,
applies the migrations. See [docs/deployment.md](docs/deployment.md).
