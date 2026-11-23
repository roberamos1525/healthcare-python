# Testing

All test data is **synthetic**: names such as "Jordan Sample", addresses on "Test Street", phone
numbers in the 555-01xx range, email addresses on the reserved `example.test` domain and randomly
generated, check-digit-valid NPIs that are not tied to real providers.

---

## 1. Strategy

| Level | Purpose | Where |
|---|---|---|
| Unit | Business rules in isolation: slot generation (time zones, daylight saving), appointment state machine, provider activation, lockout, permission matrix, NPI, TOTP, hashing, encryption, JWT, validation messages, body and query binding, routing-tree resolution, paging, sorting, LIKE escaping, reference JSON, canonical request hashing, configuration, connection strings, error translation, cache helpers | `tests/unit/` |
| Integration | Real PostgreSQL with this implementation's migrations and row-level security; transactions, constraints, locks, background jobs, transaction-scoped tenant settings, baselining an existing schema | `tests/integration/test_migrations.py`, `test_jobs.py`, `test_tenant_isolation.py` |
| API | The full HTTP stack (Uvicorn, FastAPI routes, the ASGI pipeline, binding, problem rendering) on a random local port: validation, authorization, status codes, problem details, headers | `tests/integration/test_auth.py`, `test_organizations.py`, `test_providers_patients.py`, `test_appointments.py`, `test_http_behaviour.py` |
| Contract | The served document equals `docs/openapi.yaml`; every operation documents its errors and security; every operation is implemented; every operation except the 8 anonymous ones returns 401 without a token; real responses and problems conform to the documented schemas | `tests/integration/test_contract.py` |
| Security | Tenant isolation at API and database level, privileges of the application role, token forgery, SQL injection, security headers, rate limiting, enumeration resistance | `tests/integration/test_tenant_isolation.py`, `test_auth.py` |
| Cross-implementation | The same requests against the reference implementation and this one, compared field by field; a request hash stored by the reference recomputed here; a schema diff against a reference-migrated database; the shared k6 scripts | Section 4 |
| Performance | The shared k6 load profiles and booking-contention scenario | `perf/k6`, section 5 |

Integration tests never mock the database. The session fixture (`tests/integration/environment.py`)
creates the `healthcare_migrator` and `healthcare_app` roles if missing, creates a uniquely named database
`healthcare_test_<random>`, installs `btree_gist`, applies the real Alembic migrations as the migrator,
builds the real application container (connected as the restricted application role) and serves it with
Uvicorn on a random local port **in the test event loop**, so tests, server and database pool share one
loop and concurrent requests (`asyncio.gather`) really run concurrently. All tests call the API over
HTTP with httpx; the database is dropped at the end of the session. Emails are captured in memory
(`CapturingSender`) so tests can follow verification, reset and invitation links. Password hashing uses
1,000 iterations, background polling is disabled and the jobs are invoked directly, and rate limiting is
disabled except in the rate-limiting test, which builds a second application container with its own
settings and calls it through `httpx.ASGITransport`. Without `HEALTHCARE_TEST_POSTGRES` the fixture
starts `postgres:17-alpine` with Testcontainers (optional dependency group `containers`; needs Docker,
which is not available on the development workstation, so this path has not been run).

---

## 2. Coverage

### 2.1 Unit tests (174 test functions, 345 test cases with parametrization)

| File | Functions | Covers |
|---|---:|---|
| `test_slots.py` | 24 | The reference's slot generator cases: slots in the location time zone; weekday and effective-date filtering (inclusive); no partial slots; holidays skip weekly hours but keep additional hours; time off and booked appointments removed, half-open intervals; earliest/latest limits (notice and horizon); **America/Chicago spring-forward gap skipped and the repeated fall-back hour used once**; hourly slots across fall-back do not overlap; overlapping candidates dropped; multi-day ranges; several locations merged in start order; another zone maps the same local hours to other instants |
| `test_domain.py` | 39 | Allowed and forbidden transitions with the reference code, kind and message; terminal statuses; slot occupation; reschedule rules; **the full permission matrix** per role, combined and unknown roles, assignable roles, ordinal order; lockout; activation needs a verified current license, board certification is not a license, review needs a final decision; NPI check digit; weekly overlap; consent revocation; enumeration matching (snake_case, PascalCase, defaults); dates, `HH:mm` times, day numbers, 29 February, IANA zones, local-to-UTC in both hemispheres |
| `test_security.py` | 36 | **TOTP RFC 6238 vectors** (secret `12345678901234567890`: 59 → 287082, 1111111109 → 081804, 1111111111 → 050471, 1234567890 → 005924, 2000000000 → 279037), ±1 step only, malformed and foreign codes, base32, `otpauth` URI; tokens, hashes, base64url (strict alphabet), recovery code format, random ranges, RFC 3986 escaping; **PBKDF2 format** `pbkdf2-sha256$600000$<salt>$<hash>`, an RFC 7914 vector, hashes in the reference format, rehash flag, malformed hashes, hashing on a worker thread; **AES-GCM** round trip, random nonce, **tamper and foreign-key rejection**; keys required outside development; RS256 issue/validate with the reference header and claims, 30 s leeway, expired, wrong audience/issuer, wrong key, `alg: none`, HMAC keyed with the public key; JWKS and key id |
| `test_core.py` | 40 | **Sort parsing** (case-insensitive fields, `-` prefix, empty segments, unknown fields rejected); **LIKE escaping**; page clamping and total pages; validation rules with the reference messages (not empty, lengths in UTF-16 units, email, phone, ZIP, states, time zones, ranges, durations, identifiers), display names, ordered and de-duplicated field errors; caller permissions, denial reasons, organization switching, platform role; reference JSON escaping and member order; instant formats and parsing; version checks; UUIDv7 and the UTC clock |
| `test_binding.py` | 16 | Every value kind; absent defaults (enum first member); every stage-1 message and path, including list elements and malformed JSON; stage-2 keys at top level and in lists before the rules; `null` per kind; duplicate and case-insensitive names; unknown properties; byte-order mark; non-UTF-8 bodies; whitespace-only bodies; media types; the 1 MiB limit |
| `test_scheduling.py` | 5 | The canonical booking request byte for byte (fractional seconds, escaped text with `+`), **a request hash stored by the reference API recomputed exactly** (`583834…262a`), offset independence, booking validation |
| `test_infrastructure.py` | 14 | Configuration defaults, `Section__Key`, arrays, Development defaults, listening port; Npgsql and URL connection strings, pool size, SSL modes; the scope settings; SQLSTATE translation to problems; Redis connection strings; memory cache expiry; trace-parent validation; path normalization; the fixed-window limiter; GUID forms; query binding (case-insensitive names, first value, blanks, `for Page` only for paging, enum message); **routing-tree resolution** (literal precedence, 405 before the GUID constraint, ordinal `Allow`) |

### 2.2 Integration, API, security and contract tests (91)

Every scenario of the reference implementation's integration suite is ported, plus the checks that the
Go port added (idle pooled connections, scope switching, implemented operations, migrations, HTTP
behaviour).

| File | Tests | Covers |
|---|---:|---|
| `test_auth.py` | 16 | Register → verify → sign in → `/me`; unverified sign-in refused; identical errors for wrong password and unknown account; duplicate registration indistinguishable and the owner notified; lockout; refresh rotation and reuse revoking the session; logout invalidating the access token immediately; listing and revoking sessions; password reset ending sessions; forgot-password enumeration resistance; **TOTP enrollment, replay protection and recovery codes**; suspension and reinstatement; platform-admin-only suspension; field validation errors; malformed JSON; profile update and password change |
| `test_organizations.py` | 11 | Creator becomes owner; optimistic concurrency; settings read and validation; invitations accepted with the given roles; invitation bound to the invited email; staff without management permissions; last owner protected; clinic admins cannot change owners and demotion applies immediately; platform admin listing and suspension with immediate effect; clinics, locations, addresses, time zones and holidays; tenant endpoints require an organization |
| `test_providers_patients.py` | 16 | Activation requires a verified license; NPI validation and uniqueness; search by name and specialty, literal wildcards, unknown sort field; specialty replacement with optimistic concurrency and duplicates; schedule overlap and location assignment; availability with time off and holidays and the 31-day limit; directory search; registration by receptionists, MRN, audit of reads; audit metadata free of patient details; providers see only patients in their care; self-registration, identity fields locked, contact update; single primary emergency contact; consents; read-only staff; nested address validation; ending a care relationship |
| `test_appointments.py` | 20 | Staff booking confirmed with history, reminders, PHI-free outbox payload; idempotent replay with `Idempotent-Replayed`; key reuse (422); missing and malformed keys (400); taken, off-grid and out-of-hours slots (409); **20 concurrent requests for one slot → exactly one 201**; **10 concurrent retries with one key → one appointment**; **raw SQL overlap rejected with 23P01**; life cycle and invalid transitions; no-show before start; receptionists cannot start visits; cancellation frees the slot; rescheduling; optimistic concurrency; patient portal rules; cancellation window; minimum booking notice; inactive provider; waiting list fulfilment; filters, sorting, pagination |
| `test_tenant_isolation.py` | 12 | Cross-tenant reads 404; header switching gives guest access only; self-scheduling disabled → `not_a_member`; cross-tenant writes refused; **row-level security as the application role in plain SQL**; the application role cannot update audit events, delete patients, rewrite history or alter tables; idle pooled connections carry no tenant settings after requests; a scoped transaction sees only its organization and `set_scope` switches it; SQL injection in search; forged, `alg: none`, tampered and garbage tokens → 401; security headers; auth rate limit |
| `test_jobs.py` | 3 | Outbox dispatch publishes once; due reminders queued as PHI-free events and reminders of cancelled visits dropped; maintenance removes expired idempotency records and expires invitations |
| `test_contract.py` | 6 | Served document equals `docs/openapi.yaml`; all 95 operations declare errors and security; every operation is implemented; every protected operation needs a bearer token; 18 GET responses and a validation problem conform to the documented schemas |
| `test_http_behaviour.py` | 5 | Binding stages (415, empty body, wrong type, missing property, 413); unknown routes (401/404), non-GUID identifiers, 405 with `Allow: GET, PUT`; CORS; health, JWKS and contract endpoints; problem trace id and no-cache headers |
| `test_migrations.py` | 2 | 30 tables, forced RLS, policies, the exclusion constraint, history at revision `0002`; an idempotent second run; **a schema without Alembic history is baselined** without re-running the scripts, keeping existing data |

---

## 3. Running the tests

```powershell
.venv\Scripts\python.exe -m pytest tests/unit -q
$env:HEALTHCARE_TEST_POSTGRES = "Host=localhost;Port=54329;Username=postgres;Database=postgres"
.venv\Scripts\python.exe -m pytest -q                       # unit + integration
$env:HEALTHCARE_TEST_REDIS = "127.0.0.1:6379"               # optional: Redis instead of the memory cache
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests; .venv\Scripts\python.exe -m ruff format --check src tests
.venv\Scripts\python.exe -m mypy --strict src tests
```

---

## 4. Results

Run on 23 November 2026 on the development workstation (Windows 11, Python 3.14.10, PostgreSQL 17.6 on
port 54329, Redis 5.0.14):

| Check | Result |
|---|---|
| Unit tests | **345 passed** (174 functions) in 1.3 s |
| Unit + integration tests (memory cache) | **436 passed** (345 unit, 91 integration) in 20 s |
| Unit + integration tests with `HEALTHCARE_TEST_REDIS=127.0.0.1:6379` | **436 passed** in 55 s |
| `ruff check src tests`, `ruff format --check` | clean |
| `mypy --strict src tests` | no issues (90 files) |
| Schema comparison | `pg_dump --schema-only` of a database migrated by this backend and of one migrated by the reference (release build): 2,561 lines each, identical apart from the history table and pg_dump's random `\restrict` token |
| Black-box comparison with the reference (release build containing commit `239973c`) | 223 exchanges: 222 identical in status, selected headers, normalized body and member order; 1 difference, a reference defect (`specialties: [null]` → 500 there, 400 here; [troubleshooting.md](troubleshooting.md#4-defect-in-the-reference-implementation-not-reproduced)) |

The black-box comparison found two real differences during development (whitespace-only bodies, and
`GET /patients/self-registration` → 405 `Allow: POST`); both are fixed and covered by unit tests
([troubleshooting.md](troubleshooting.md#3-behaviour-differences-found-against-the-reference-fixed)).
The script itself is not part of the repository (it needs a running reference build).

---

## 5. Performance tests

The scripts in `perf/k6/` are copied unchanged from the reference implementation, so all backends are
measured with the same workload.

| Script | Purpose |
|---|---|
| `perf/k6/smoke.js` | One virtual user runs each flow of `load.js` once; every check must pass |
| `perf/k6/load.js` | Mixed workload of six ramping-VU scenarios: registration 5 %, sign-in 10 %, provider directory search 20 %, availability 30 %, booking followed by cancellation 15 %, patient record read and search 20 %. `PROFILE=baseline` ramps 5 → 10 → 20 VUs, `stress` 20 → 50 → 100 VUs, each over 3 min. Thresholds: p95 < 300 ms for reads, p95 < 500 ms for booking, unexpected error rate < 1 % |
| `perf/k6/booking-contention.js` | `CONTENDERS` virtual users (default 50) send one booking each for the same slot at the same moment; requires exactly one 201 and nothing but 409 otherwise |
| `perf/run-measured.ps1` | Stops whatever listens on 8080 (with its process tree), starts a fresh production-style server, waits for `/health/ready`, resets Redis statistics, runs one k6 script with optional sampling and records the outbox lag |
| `perf/sample-resources.ps1` | Every 5 s: CPU (% of the whole machine) and working set **summed over the whole server process tree** (uv launcher, Uvicorn supervisor, all worker processes), the number of processes, PostgreSQL connections of `healthcare_app`; Redis hit and miss counters at the end |
| `perf/summarize.py` | Extracts the figures below from the k6 summaries and samples |

Raw k6 output and summaries, the resource samples and the outbox queries of every run are in
`perf/results/`.

### 5.1 Results

Measured on 23 November 2026 on a single developer workstation. **These figures are single-workstation
measurements, useful for regression comparison only; they are not a statement of production
capacity.**

Environment: Windows 11, 13th Gen Intel Core i5-13420H (12 logical cores), 15.7 GB RAM; Python 3.14.10;
the server started with `python -m healthcare` (Uvicorn, httptools, **8 worker processes**,
`asyncio` event loop) with `ASPNETCORE_ENVIRONMENT=Production` (real RS256 and AES keys read from files
outside the repository), PBKDF2 with 600,000 iterations, Redis cache, background workers enabled in every
worker process, `Log` event publisher, email via SMTP to Mailpit, rate limiting disabled,
`Maximum Pool Size=15` per worker, log level Warning; PostgreSQL 17.6, Redis 5.0.14 and Mailpit on the
same machine; k6 2.4.0 on the same machine; database `healthcare_python` (each load run creates its own
practice). The runner addresses the API as `127.0.0.1` ([troubleshooting.md](troubleshooting.md), 1.4).

**Smoke** (`smoke.js`): **10 / 10 checks passed (100 %)**, 0 unexpected errors, 0 of 139 requests failed,
k6 exit code 0.

**Booking contention** (`booking-contention.js`, `CONTENDERS=50`): **1 × 201, 49 × 409**, 0 other
responses, 50 / 50 checks passed, both thresholds (`bookings_created == 1`, `unexpected_responses == 0`)
passed. The conflicts queue behind the per-provider advisory lock: all 88 requests (setup and the 50
bookings) had a median of 321 ms and a maximum of 1.33 s; the 39 successful requests (setup plus the one
booking) had a median of 5.3 ms.

**Mixed load** (`load.js`, each run on a freshly started server):

| Profile | Peak VUs | Requests | Throughput | Failed requests | Checks | Bookings | All requests p50 / p95 / p99 / max |
|---|---:|---:|---:|---:|---|---:|---|
| baseline | 20 | 3,758 | 19.7 req/s | 0 % | 3,629 / 3,629 | 236 | 4.7 / 129.8 / 154.4 / 243 ms |
| stress | 100 | 18,599 | 97.5 req/s | 0 % | 18,469 / 18,469 | 1,163 | 5.9 / 134.2 / 216.1 / 6,244 ms |

Every threshold passed in both runs (k6 exit code 0); the unexpected-error rate was 0 %. Throughput is
set by the scripts' think times, not by saturation. The "all requests" p95 and p99 are dominated by
registration and sign-in, which are deliberately slow because of PBKDF2 with 600,000 iterations (they
run on worker threads, so they do not stall other requests).

Latency by endpoint (k6 `name` tag), p50 / p95 / p99 / max in ms:

| Endpoint | baseline | stress | Target (p95) |
|---|---|---|---|
| `appointment_book` (POST /appointments) | 11.0 / 17.3 / 32.8 / 46.0 | 15.2 / 28.4 / 41.8 / 65.5 | < 500 ms ✔ |
| `availability` | 4.8 / 7.4 / 19.4 / 49.8 | 6.4 / 12.3 / 95.8 / 6,020 | < 300 ms ✔ |
| `directory_search` | 4.0 / 7.8 / 11.9 / 30.1 | 5.3 / 10.8 / 16.7 / 5,621 | < 300 ms ✔ |
| `patient_read` | 3.8 / 5.3 / 7.3 / 15.4 | 4.6 / 9.4 / 15.4 / 4,970 | < 300 ms ✔ |

In the stress run a few read requests took 5–6 s (the maxima above) while p99 stayed between 15 and
96 ms. Their cause was not established: the server was run at log level Warning, so there are no
per-request server timings for that run, and k6, PostgreSQL and the 8 worker processes share the same
12 logical cores. In a diagnostic baseline run at log level Information the slowest server-side request
took 214 ms. This should be investigated on separate load-generator hardware before any capacity claim.

Resources and background processing (5-second samples over the run; CPU and memory summed over the 11
processes of the server: uv launcher, Uvicorn supervisor, its helper process and 8 workers):

| Metric | baseline | stress |
|---|---|---|
| Server CPU, average / peak (% of the whole machine) | 2.2 % / 4.7 % | 9.6 % / 30.2 % (≈ 3.6 cores) |
| Server working set, average / peak | 965 MB / 970 MB | 982 MB / 997 MB |
| PostgreSQL connections of `healthcare_app` (peak open / peak active) | 24 / 2 | 72 / 1 |
| Redis cache hit ratio | 99.7 % (6,247 hits, 17 misses) | 99.9 % (31,970 / 24) |
| Outbox events in the query window / left pending | 786 / 0 | 2,735 / 0 |
| Outbox lag (occurred → processed) mean / p95 / max | 0.59 / 1.55 / 1.72 s | 0.44 / 1.23 / 1.82 s |

The working set of about 120 MB per worker process (each imports the full application and keeps its own
pool and caches) is the main cost of the process model compared with single-process runtimes. The outbox
lag is bounded by the 2-second poll interval of the dispatcher; eight worker processes poll concurrently
and `SKIP LOCKED` keeps them from processing an event twice (0 events left pending).

Earlier runs on the same day, made while the Redis client was failing (RESP3 handshake rejected by
Redis 5, so every cache call fell back to the database), were discarded and are not reported here
([troubleshooting.md](troubleshooting.md), 1.5).
