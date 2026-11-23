# Troubleshooting

Problems that were actually hit while building and testing this implementation, with their causes and
fixes: toolchain and workstation issues first, then defects found in this code by its own tests, then
behaviour differences against the reference implementation found by black-box comparison, and finally
one defect of the reference that is not reproduced.

---

## 1. Toolchain and workstation

### 1.1 `uv` is not recognized right after installing it

**Where:** a PowerShell session opened before `winget install astral-sh.uv`.
**Cause:** winget adds `%LOCALAPPDATA%\Microsoft\WinGet\Links` to the user PATH, which running shells do
not reload.
**Fix:** open a new shell, or call `"$env:LOCALAPPDATA\Microsoft\WinGet\Links\uv.exe"` directly.

### 1.2 Python 3.14 instead of the newest release

`uv python list` offered CPython 3.15.2 as well. 3.14.10 was chosen because every dependency with
compiled parts (asyncpg, pydantic-core, cryptography, httptools, greenlet) publishes Windows wheels for
it; 3.14 also brings `uuid.uuid7()` in the standard library.

### 1.3 Multi-statement migration files fail with "cannot insert multiple commands into a prepared statement"

**Cause:** SQLAlchemy's asyncpg dialect prepares every statement, and PostgreSQL cannot prepare several
commands at once; the migration files are complete scripts with `DO $$ ... $$` blocks, so splitting them
is not safe.
**Fix:** `execute_sql_file` (`migrations/history.py`) takes the asyncpg connection behind Alembic's
connection and runs the file with `await_only(connection.execute(sql))` (simple query protocol). It runs
inside the migration transaction, after `SELECT 1` has made sure the driver-level transaction started.

### 1.4 Every request through `localhost` took two seconds; the readiness probe never succeeded

**Where:** the first k6 runs (`BASE_URL=http://localhost:8080`, `ConnectionStrings__Redis=localhost:6379`).
**Cause:** on Windows `localhost` resolves to `::1` first. Uvicorn was bound to `0.0.0.0` (IPv4 only) and
the local Redis service listens on `127.0.0.1` only, and Windows needs about two seconds to give up on a
refused connection before trying the IPv4 address. `/health/ready` probes Redis, so it took just over two
seconds and the runner's two-second probe always timed out.
**Fix:** the perf runner addresses the API, Mailpit and Redis as `127.0.0.1`. In containers and on Linux
the problem does not occur (the names resolve to the address the service listens on).

### 1.5 Redis showed no hits and no misses; some requests took 20 seconds

**Where:** the first baseline load run (Redis 5.0 Windows service).
**Symptoms:** `keyspace_hits:0`, `keyspace_misses:0` after the run, `Cache read failed ... ResponseError` warnings in
the API log, and a few requests with ~20 s latency while p99 stayed below 160 ms.
**Cause:** redis-py 8 negotiates RESP3 with `HELLO 3` on every new connection, and Redis 5 rejects the command.
Each cache call failed (the application correctly fell back to the database, so all responses were still
correct), and the client's retry-with-backoff on failed connection set-up produced the occasional long waits.
The integration tests had not caught it because they use the in-memory cache unless `HEALTHCARE_TEST_REDIS` is set.
**Fix:** the client is created with `protocol=2` (RESP2 works with every Redis version and with ElastiCache).
A diagnostic run afterwards showed a maximum latency of 214 ms; the integration suite was also run against the
real Redis (`HEALTHCARE_TEST_REDIS=127.0.0.1:6379`). The first, invalid measurements were discarded; all numbers
in [testing.md](testing.md) are from runs after the fix.

### 1.6 The resource sampler measured almost nothing

**Cause:** `.venv\Scripts\python.exe` created by uv is a small launcher that starts the real interpreter
as a child process; with `HEALTHCARE_WORKERS=8` the interpreter starts eight spawned worker processes.
The process that owns the listening socket is the supervisor, not a worker.
**Fix:** `perf/sample-resources.ps1` walks up from the listening process to the root of the Python process
tree and sums CPU time and working sets over the root and all descendants (11 processes in the measured
runs: launcher, supervisor, eight workers, and the multiprocessing helper).

### 1.7 k6 output files look binary

**Cause:** Windows PowerShell 5.1 writes `*>` redirections as UTF-16.
**Fix:** read them with `Get-Content`, or strip NUL bytes (`tr -d '\000'`) in Git Bash. The numbers in
[testing.md](testing.md) come from the JSON summaries (`--summary-export`).

### 1.8 Building the reference with its fix without touching it

The black-box comparison needed a build of the reference that contains its latest commit (the provider
specialties fix). Building inside its folder would have written `bin/` and `obj/` there, so the source was
copied to a scratch folder (without `bin`, `obj` and `.git`) and published from the copy;
`git status` in the reference stayed clean.

---

## 2. Defects found in this implementation by its own checks (fixed)

| Symptom | Cause | Fix |
|---|---|---|
| A unit test expected `base64url_decode("***")` to fail, but it returned bytes | `base64.urlsafe_b64decode` silently discards characters outside the alphabet | The decoder checks the alphabet (`[A-Za-z0-9_-]`) and rejects impossible lengths first |
| `mypy --strict` reported `Function "...list" is not valid as a type` | Use-case methods named `list` shadow the builtin inside the class body, so later `list[...]` annotations resolve to the method | The methods are named after what they list (`list_providers`, `list_patients`, ...) |
| `mypy` reported the field `date` of `HolidayRequest` as "not valid as a type" | A Pydantic field named `date` shadows `datetime.date` inside the class body | Fields named `date` are annotated as `dt.date` (module-qualified) |
| `Row[Any]` index 1 "out of range" in a test | SQLAlchemy 2.1's `Row` is variadic (`Row[*Ts]`), so `Row[Any]` means a one-column row | The `Tx` port returns rows as the `Row` protocol / `Any` |
| JSONB double-encoding risk for idempotent responses and outbox payloads | SQLAlchemy's JSONB bind processor would `json.dumps` an already serialized string | Values are bound as text and cast in SQL (`CAST(CAST(:payload AS text) AS jsonb)` / `cast(literal(..., Text), JSONB)`); payloads are read back as `::text` |

---

## 3. Behaviour differences found against the reference (fixed)

A black-box script (`blackbox.py`, kept outside the repository) sends the same requests to the reference
API (a release build on port 8091 with its own database) and to this API (port 8092) and compares status
codes, selected headers (content type, `Allow`, `WWW-Authenticate`, cache and CORS headers, `Location`,
`Idempotent-Replayed`, the security headers) and normalized bodies (identifiers, timestamps, tokens and
trace ids replaced; member order compared too). The final run covered 223 exchanges: anonymous and
routing edge cases, 22 malformed or unusual bodies, CORS, a complete practice set-up, providers with
specialties, schedules with `HH:mm`/`HH:mm:ss`/`null`/numeric times, patients, booking, replay, key reuse,
conflicts, the appointment life cycle, the waiting list, the audit trail, a portal patient, invitations,
MFA, refresh rotation and replay, logout, and query-string binding errors.

| Difference | Cause | Fix |
|---|---|---|
| A body of only spaces answered `A non-empty request body is required.` | The reference treats only a body without any bytes as empty; whitespace is unreadable JSON at the root | Only an empty body is "empty"; whitespace yields `body: ["The value is missing or not valid."]` |
| `GET /api/v1/patients/self-registration` answered an empty 404; the reference answers 405 with `Allow: POST` | In the reference's routing tree a literal segment of a template excludes GUID-constrained identifier routes at that position, while any other value goes to the identifier branch without checking the constraint | `Router.candidates` walks the templates segment by segment with that rule; 405/`Allow` is decided on the candidates, the GUID constraint only afterwards. Unit test `test_routes_resolve_like_the_reference_routing_tree` |

Afterwards 222 of 223 exchanges were identical; the remaining one is section 4.

Two behaviours were confirmed identical although they differ from the Go and Rust ports' router
libraries: literal path segments are matched case-insensitively (`/API/V1/AUTH/ME`), and a trailing slash
is ignored (`/api/v1/auth/me/`).

---

## 4. Defect in the reference implementation (not reproduced)

`POST /api/v1/providers` with `"specialties": [null]` answers **500 internal_error** in the reference: its
validator dereferences the null list element. This backend reports the element as unreadable
(`400`, `errors: {"specialties[0]": ["The value is missing or not valid."], "request": [...]}`), which is
how the reference treats every other wrong value inside the list. This is the only difference in the
final comparison run.

**Update, 23 November 2026:** the reference has since been fixed (HealthCare-C# commit `b9eed75`) and now
answers this request with the same `400` and `errors` body as this backend, so the difference is gone.
The comparison figures were measured against the earlier reference build.

The earlier reference defect for `PUT /api/v1/providers/{id}` with specialties (always 409) is fixed in the
reference (commit `239973c`); the comparison run against a build containing that commit shows identical
behaviour, so there is no deviation for it.
