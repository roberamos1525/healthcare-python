# Capacity Model

This document sizes the platform for its first scale milestone: **100,000 registered users**. It
separates registered users, daily active users, concurrent users and request rates, shows the
arithmetic, and states every assumption so the model can be corrected when real traffic data
exists.

> **Status of the numbers in this document.** Everything here is an estimate derived from stated
> assumptions. Nothing in this document is a measurement. Performance figures are engineering
> targets. Measured results belong in a test report that names the environment, the data set and the
> load profile.

---

## 1. Performance targets

| Target | Value | Notes |
|---|---|---|
| Common API reads | p95 < 300 ms | Measured at the load balancer, steady state |
| Appointment booking | p95 < 500 ms | Excludes external dependencies (email, SMS, payment) |
| API availability | 99.9 % per month | About 43 minutes of allowed downtime per 30-day month |
| Conflicting confirmed appointments | 0 | A correctness invariant enforced by the database, not a statistical target |

These are targets to design and test against. They are **not** claims of measured performance.

---

## 2. Population assumptions

| Quantity | Assumption | Value |
|---|---|---|
| Registered users | Milestone | 100,000 |
| Patients (portal accounts) | 94 % of users | 94,000 |
| Staff (receptionists, administrators, other staff) | 4 % | 4,000 |
| Providers | 2 % | 2,000 |
| Organizations | Average 4 providers each | 500 |
| Locations | Average 1.5 per organization | 750 |
| Patient records | Includes patients registered by staff who have no portal account; 2.5 × portal patients | 235,000 |

The split reflects a product sold to small and medium practices: a few thousand professionals, each
serving many patients. Patient records exceed patient accounts because front-desk staff register
patients who never sign in.

---

## 3. Activity model

### 3.1 Daily active users

Patients use the product occasionally; staff and providers use it all day on working days.

| Group | Population | Daily active share | DAU |
|---|---:|---:|---:|
| Patients | 94,000 | 4 % | 3,760 |
| Staff | 4,000 | 70 % | 2,800 |
| Providers | 2,000 | 65 % | 1,300 |
| **Total** | 100,000 | | **7,860** |

Assumption behind the patient share: a patient interacts with a clinic a few times a year (book,
confirm, check details, reschedule), which gives roughly 10–15 active days per year, or 3–4 % on a
given day.

### 3.2 Peak concurrent users

US business hours across four mainland time zones overlap for about five hours. Staff and providers
keep the application open; patients have short sessions.

| Group | DAU | Concurrent at peak | Peak concurrent |
|---|---:|---:|---:|
| Patients | 3,760 | 5 % (sessions of 5–10 minutes, clustered in morning and evening) | 190 |
| Staff | 2,800 | 60 % | 1,680 |
| Providers | 1,300 | 50 % | 650 |
| **Total** | | | **≈ 2,500** |

### 3.3 Requests per second

Requests per active minute, by group (API calls, excluding static assets served by the CDN):

| Group | Peak concurrent | Requests per user per minute | Requests per minute |
|---|---:|---:|---:|
| Patients | 190 | 6 (active browsing during a short session) | 1,140 |
| Staff | 1,680 | 3 (calendar refreshes, searches, check-ins) | 5,040 |
| Providers | 650 | 1.5 (schedule and patient lookups between visits) | 975 |
| **Total** | | | **7,155** |

- Peak sustained rate: 7,155 / 60 ≈ **120 requests per second**.
- Burst allowance: 3 × sustained for top-of-the-hour spikes and client retries ≈ **360 requests per
  second**.
- Daily volume: assume the peak rate holds for 5 hours and half of it for another 5 hours:
  120 × 18,000 s + 60 × 18,000 s ≈ **3.2 million requests per day**.
- Average over 24 hours: 3.2 M / 86,400 ≈ **37 requests per second**.

Design capacity is set at **400 requests per second** so that the first milestone runs at about
30 % of capacity at sustained peak and survives a burst without queueing.

### 3.4 Request mix

| Category | Share of requests | Examples |
|---|---:|---|
| Reads | 88 % | Appointment lists, availability, patient lookup, provider directory, profile |
| Writes | 10 % | Booking, status changes, patient updates, schedule edits |
| Authentication | 2 % | Sign-in, token refresh, sign-out |

At sustained peak: about 106 reads, 12 writes and 2–3 authentication requests per second.

Password hashing deserves separate attention because it is deliberately expensive
(PBKDF2-HMAC-SHA256, 600,000 iterations — on the order of a few hundred milliseconds of one CPU
core per attempt, to be confirmed by measurement). Sign-ins are estimated at 7,860 DAU × 1.2
sign-ins = 9,400 per day; with half arriving in a two-hour morning window the rate is about
0.65 per second, with bursts of a few per second. Token refreshes do not hash passwords. Sign-in
rate limiting bounds the worst case.

---

## 4. Appointment volume

| Quantity | Calculation | Value |
|---|---|---:|
| Appointments per provider per working day | Assumption | 16 |
| Appointments per working day | 2,000 × 16 | 32,000 |
| Working days per year | Assumption | 250 |
| Appointments per year | 32,000 × 250 | 8,000,000 |
| Bookings created per day | Appointments + 15 % later cancelled or no-show | ≈ 37,000 |
| Peak booking rate | 30 % of bookings in the busiest hour: 11,100 / 3,600 | ≈ 3 per second |
| Status changes per appointment | Created, confirmed, checked in, started, completed | ≈ 4.5 |
| Status-history rows per year | 8 M × 4.5 | 36,000,000 |
| Reminders per appointment | Assumption (for example 24 h and 2 h) | 2 |
| Reminder rows per year | 8 M × 1.15 × 2 | ≈ 18,400,000 |

Booking contention is local, not global: writes for one provider are serialized by a per-provider
advisory lock and the exclusion constraint, so the theoretical write throughput scales with the
number of providers being booked at the same moment. A booking rate of 3 per second across 2,000
providers produces negligible lock waiting; the contention case that matters is many clients
competing for one popular slot, which is covered by the concurrent-booking test.

Availability queries are the most frequent scheduling read. Each one loads a provider's schedule
blocks, exceptions, holidays and appointments for a bounded date range (at most 31 days), which the
`(provider_id, starts_at)` index satisfies with a short range scan.

---

## 5. Data volume and storage growth

Row sizes are planning estimates including tuple overhead; index size is assumed at 60–100 % of
table size depending on the number of indexes.

| Table family | Rows after year 1 | Avg row | Table | With indexes |
|---|---:|---:|---:|---:|
| Users, memberships, links | 100,000 users + 110,000 related | 400 B | 0.1 GB | 0.2 GB |
| Sessions and one-time tokens | ≈ 60,000 live (expired rows purged) | 300 B | < 0.1 GB | < 0.1 GB |
| Organizations, clinics, locations, settings | ≈ 3,000 | 500 B | negligible | negligible |
| Providers, credentials, schedules, exceptions | ≈ 60,000 | 300 B | < 0.1 GB | < 0.1 GB |
| Patients, emergency contacts, consents, relationships | 235,000 patients + 900,000 related | 450 B | 0.5 GB | 1.0 GB |
| Appointments | 9,200,000 (including cancelled) | 350 B | 3.2 GB | 6.5 GB |
| Appointment status history | 36,000,000 | 180 B | 6.5 GB | 10 GB |
| Appointment reminders | 18,400,000 | 150 B | 2.8 GB | 4.5 GB |
| Waiting list | ≈ 200,000 | 250 B | < 0.1 GB | 0.1 GB |
| Audit events | ≈ 400,000,000 (see below) | 350 B | 140 GB | 190 GB |
| Outbox events | ≈ 60,000,000 per year, purged after 14 days → ≈ 2,300,000 live | 400 B | 0.9 GB | 1.3 GB |
| Idempotency records | 37,000 per day, purged after 48 hours → ≈ 75,000 live | 1 KB | < 0.1 GB | 0.1 GB |

Audit estimate: all writes (≈ 320,000 per day) plus reads of patient data. If 40 % of the
≈ 2.8 million daily reads touch patient data, that is ≈ 1.1 million read events per day; the total
of ≈ 1.45 million events per working day × 250 days ≈ 360 million, rounded up to 400 million to
include authentication events.

Conclusions:

- Transactional data is small: about **25 GB** after the first year, growing roughly linearly with
  appointment volume.
- The audit trail dominates storage: about **190 GB per year**. It must be retained for at least
  six years (see the retention policy), so it is the table that needs a growth strategy.
- Strategy for large append-only tables (`audit_events`, `appointment_status_history`,
  `appointment_reminders`): monthly range partitioning on the timestamp column once a table passes
  roughly 100 million rows, and export of closed audit partitions to encrypted object storage with
  lifecycle rules. Partitioning is not applied in the initial schema; the tables are designed so it
  can be introduced without changing application code.
- Initial database storage allocation: **100 GB** with storage autoscaling enabled, reviewed
  quarterly.

Document files (Release 3) live in object storage and are not part of this estimate.

---

## 6. Application tier sizing

Assumptions: a mid-size container (2 vCPU, 4 GB) running the API; average CPU cost per request
≈ 5 ms for reads and ≈ 15 ms for writes; password verification handled separately.

- CPU demand at sustained peak: 106 × 5 ms + 12 × 15 ms ≈ 0.71 core-seconds per second.
- Sign-in hashing at 3 per second burst × ≈ 0.3 s ≈ 0.9 core-seconds per second.
- Total ≈ 1.6 cores at peak, before garbage collection and background workers.

| Environment | Instances | Reasoning |
|---|---:|---|
| Production, initial | 3 × (2 vCPU, 4 GB) across three availability zones | No single point of failure; ≈ 30 % CPU at sustained peak; one instance can be lost during a burst |
| Production, autoscaling | 3–8 | Scale out on CPU > 55 % or request count per target |
| Staging | 2 | Exercises multi-instance behaviour (outbox contention, cache invalidation) |
| Development | 1 | |

The per-request CPU costs are assumptions and are the first figures to replace with measurements
from the performance tests.

---

## 7. Database connection budget

The API holds a connection only for the duration of a query or transaction, so the pool needs to
cover concurrent in-flight database work, not concurrent users.

- In-flight requests at sustained peak ≈ rate × mean latency = 120 × 0.08 s ≈ 10.
- At burst (360 requests per second) with degraded latency (0.25 s) ≈ 90.

| Consumer | Connections |
|---|---:|
| API instances: 8 (autoscaling maximum) × pool maximum 30 | 240 |
| Background workers (outbox dispatcher, reminder scheduler) — included in the API pool | 0 |
| Migration job | 2 |
| Administration and monitoring | 10 |
| Headroom | 48 |
| **`max_connections` required** | **300** |

Guidance:

- Pool maximum per instance: 30; minimum: 5. With three instances the steady-state footprint is
  15–90 connections. The pool is set in the connection string (`Maximum Pool Size=30;Minimum Pool
  Size=5`). In this backend the pool size applies **per worker process**: an instance with four
  Uvicorn workers and `Maximum Pool Size=10` opens up to 40 connections. The default without a
  setting is 100 per process; the local Compose file uses 2 workers × 25.
- If the instance count must exceed eight, introduce a connection proxy (RDS Proxy or PgBouncer)
  instead of raising `max_connections`. This backend already applies the tenant settings
  transaction-locally (`set_config(..., true)` at the start of every transaction), so a proxy in
  transaction pooling mode is compatible with its tenant isolation; prepared statements must then be
  disabled or the proxy must support them (asyncpg prepares statements per connection).
- A database instance with 4 vCPU and 32 GB memory comfortably holds the first-year working set
  (transactional data plus hot indexes ≈ 25 GB) in memory. Start at 4 vCPU / 16 GB and resize on
  observed buffer-cache hit ratio.

Read scaling: reporting and analytics (Release 4) read from a replica. The transactional API does
not need a replica at this milestone; the option is kept open by routing read-only reporting queries
through a separate connection string.

---

## 8. Cache sizing

Redis holds short-lived lookups that would otherwise hit the database on every request.

| Entry | Key cardinality at peak | Size per entry | TTL | Memory |
|---|---:|---:|---|---:|
| Session validity (`session:{id}`) | ≈ 2,500 concurrent sessions, 10,000 within TTL churn | 100 B | 30 s | 1 MB |
| Organization access snapshot (`access:{organization}:{user}` → roles, provider id, patient ids) | ≈ 10,000 | 300 B | 60 s | 3 MB |
| Rate-limit counters (if centralized later; Release 1 keeps them in process memory per instance) | ≈ 20,000 | 100 B | 1 min | 2 MB |
| **Total working set** | | | | **< 10 MB** |

Organization settings are read from PostgreSQL on each booking and availability request in
Release 1; caching them is a possible optimization if measurements show it matters.

Even with a tenfold margin and Redis per-key overhead the cache needs well under 1 GB. The smallest
production-grade node with a replica in a second availability zone is sufficient; the choice is
driven by availability, not memory.

Expected effect: session and membership lookups occur on every authenticated request (≈ 120 per
second at peak). With a 60-second lifetime and a few requests per user per minute, the expected hit
ratio is roughly 65–85 % for staff traffic. This is an expectation to verify; cache hit ratio is one
of the metrics the performance tests record. The cache is an optimization only: on a miss or a Redis
outage the application reads from PostgreSQL.

---

## 9. Messaging volume

| Event source | Events per day |
|---|---:|
| Appointment life-cycle events (≈ 4.5 per appointment) | 166,000 |
| Reminder-due events (2 per appointment) | 74,000 |
| Patient, provider and organization changes | ≈ 10,000 |
| **Total** | **≈ 250,000** |

Average ≈ 3 events per second, peak ≈ 15 per second. The outbox dispatcher claims batches of 50
every 2 seconds and immediately takes the next batch while a full batch was found (defaults
`Workers:BatchSize` and `Workers:OutboxPollSeconds`), which is about 25 events per second per
instance even before back-to-back batches; several instances run concurrently because rows are
claimed with `FOR UPDATE SKIP LOCKED`. Target queue processing time: events published within 5
seconds of commit at p95 (a target, to be measured).

---

## 10. Growth scenarios

| Milestone | Registered users | Providers | Peak concurrent | Sustained peak req/s | Appointments per year |
|---|---:|---:|---:|---:|---:|
| Launch | 10,000 | 200 | 250 | 12 | 0.8 M |
| **First milestone** | **100,000** | **2,000** | **2,500** | **120** | **8 M** |
| Growth | 500,000 | 10,000 | 12,500 | 600 | 40 M |
| Large organizations | 1,000,000 | 20,000 | 25,000 | 1,200 | 80 M |

Linear scaling is assumed. What changes at each step:

- **500,000 users.** Application tier scales horizontally (≈ 10–15 instances) and therefore needs a
  connection proxy. Partition the large append-only tables. Add a read replica for reporting and
  patient-portal reads. Move audit history older than one year to object storage.
- **1,000,000 users.** Evaluate separating high-volume modules (notifications, audit ingestion) into
  their own deployable services using the existing module boundaries and outbox events. Evaluate
  dedicated databases for very large tenants.

---

## 11. Assumptions to validate

The model is only as good as these inputs. Each should be replaced with observed data as soon as it
is available.

| Assumption | Used in | How to validate |
|---|---|---|
| 4 % of patients active per day | DAU, request rate | Product analytics after launch |
| 16 appointments per provider per day | Appointment volume, storage | Scheduling data by specialty |
| Requests per user per minute | Request rate | Load balancer logs |
| CPU cost per request (5 ms / 15 ms) | Instance count | k6 runs with CPU profiling |
| Password hash cost | Sign-in capacity | Benchmark on the production instance type |
| 40 % of reads touch patient data | Audit volume | Audit table growth in staging |
| Cache hit ratio 65–85 % | Database load | Redis and application metrics |
| Row sizes | Storage | `pg_total_relation_size` on a synthetic data set of realistic size |

---

## 12. Measurements of this implementation

The Python backend was measured with the shared k6 scripts on a single developer workstation (full
details and caveats in [testing.md](testing.md#51-results)); these are regression figures, not a
statement of production capacity.

| Profile | Throughput | Bookings p95 | Read endpoints p95 | Server CPU peak | Server memory | DB connections peak |
|---|---:|---:|---:|---:|---:|---:|
| baseline (20 VUs) | 19.7 req/s | 17.3 ms | 5.3–7.8 ms | 4.7 % of 12 cores | 970 MB (8 workers) | 24 |
| stress (100 VUs) | 97.5 req/s | 28.4 ms | 9.4–12.3 ms | 30.2 % (≈ 3.6 cores) | 997 MB (8 workers) | 72 |

Implications for sizing this backend:

- **CPU.** Python executes request handling on one core per process, so a task needs one Uvicorn worker
  per vCPU to use its CPU. Sign-in and registration cost the same PBKDF2 work as in the reference; it
  runs on worker threads, which OpenSSL lets run in parallel with request handling.
- **Memory.** Each worker process costs roughly 110–120 MB of working set (measured: about 970 MB for the
  whole tree with 8 workers). A 1 vCPU / 2 GB task with 2 workers, or a 2 vCPU / 4 GB task with 4 workers,
  leaves headroom.
- **Connections.** The pool size applies per worker process: `instances × workers × Maximum Pool Size`
  must fit the `max_connections` budget of section 7. With 8 workers and a pool of 15, the stress run
  opened at most 72 connections, mostly idle (peak 1–2 active); a pool of 5–10 per worker is sufficient at
  these rates.

