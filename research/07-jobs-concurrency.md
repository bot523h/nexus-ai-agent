# 07 — Jobs / Concurrency / Distributed Systems Research

> **Agent 4 — Independent Research**
> **Date:** 2026-09-22
> **Scope:** Celery / RQ / Dramatiq / Temporal / Redis Streams / Kafka / RabbitMQ / Postgres queues / SQLite job patterns + exactly-once reality check
> **Status:** VERIFIED where docs explicitly state semantics, CONFIDENCE H/M/L

---

## Preamble — The One Question That Matters

"Will my job run exactly once, survive crashes, and recover without duplication or loss?" The industry answer in 2026 is *nuanced by layer*. Message delivery, queue acknowledgement, processing, side effects, and persistence each have different guarantees. Conflating them is the #1 source of exactly-once marketing confusion. [VERIFIED, HIGH — cross-validated across Temporal, Celery, Kafka docs.]

---

## 1) Systems Surveyed (9)

### J1 — Celery (broker + result backend + beat)

- **Architecture:** Python tasks decor `@app.task` → broker (Redis/RabbitMQ/SQS) → workers → result backend (Redis/RPC). Beat scheduler for cron/ETA. Canvas primitives: chain, group, chord.
- **Semantics:** Celery docs: "guarantees delivery of the *message* to the broker, not durability of application-level task state" [VERIFIED, HIGH]. At-least-once delivery via `acks_late` + `reject_on_worker_lost`. Without idempotent tasks + deduplication, duplicates occur on retry/crash.
- **Durability:** Broker-durable if `task_acks_late=True` + persistent broker. In-flight task state not checkpointed; crash loses in-progress progress.
- **Retry:** Task-level `self.retry` + `max_retries` + exponential backoff (`60 * 2**retries`). Idempotency remains app concern.
- **Scheduling:** Celery Beat (cron-like) with persistence drift risks; ETA tasks.
- **Scaling:** Horizontal workers + autoscale via `celery multi` / K8s. Canvas migrates poorly — chaining 100s subtasks is Celery-specific lock-in.
- **Maturity:** 15+ years, 5.6.3 stable, largest Python queue ecosystem. [VERIFIED]

### J2 — RQ (Redis Queue)

- **Architecture:** Redis-only, lightweight. Job = function + args pickled to Redis list. Workers `BRPOP`. No broker abstraction.
- **Semantics:** At-least-once (Redis persistence config-dependent: RDB/AOF). Simpler than Celery, fewer durability knobs.
- **Durability:** Redis durability = persistence config. No built-in exactly-once.
- **Retry:** Manual. Enqueue failed → retry queue. No native exponential with cap (must code).
- **Scheduling:** `RQ-Scheduler` / `rq cron`. Simpler but less rich than Beat.
- **Scaling:** Redis bottleneck. Good to <~1k tps; Dramatiq outperforms above.
- **Maturity:** Mature, Python-only. Ideal <1k tasks/s. [VERIFIED]

### J3 — Dramatiq

- **Architecture:** RabbitMQ-first (also Redis). Auto-ack handling correctly (fixes Celery ack race). Typed messages, dead-letter, middleware.
- **Semantics:** At-least-once with correct ack sequencing; else no stronger guarantee than Celery, but defaults safer.
- **Retry:** Auto retries with exponential backoff (default 7-day retention), Prometheus-native.
- **Durability:** RabbitMQ quorum queues durable; Redis depends on config.
- **Scheduling:** No built-in beat (use APScheduler). Intentionally minimal.
- **Scaling:** 1,200 tps/worker vs Celery 950 (modest but 30-40% lower memory). Best for reliable modern Python without Canvas lock-in. [PARTIALLY — bench vendor-reported, consistent across 2 sources.]
- **Maturity:** 6 years, production-ready, recommended default for greenfield per DevProPortal 2025.

### J4 — Temporal (Durable Execution, not a queue)

- **Architecture:** Temporal Server (+ DB: Postgres/Cassandra/MySQL) + Workers (activities). Workflow defined as code, event-sourced history persisted per step. Activities have heartbeats, timeouts, retry policies, versioning.
- **Semantics:** **Durable execution**: every state transition persisted; worker crash resumes from last recorded event via replay. Activity processing is *effectively exactly-once per history* if activity is idempotent + workflow handles deduplication via workflow ID. The durable guarantee is real for *orchestration*, not for raw side-effect exactly-once.
- **Durability:** Event-sourced — strongest of this set.
- **Retry:** Promise-based retry + timers + compensating transactions (Saga).
- **Scheduling:** Workflow timers + cron.
- **Scaling:** Horizontally via Temporal cluster; bottleneck is persistence DB + visibility (Elasticsearch optional).
- **Maturity:** Server 1.30.x Aug 2026, multi-language SDKs, enterprise-proven.

### J5 — Redis Streams

- **Architecture:** `XADD` → `XREADGROUP` → `XACK`. Consumer groups, pending entries list (PEL), `XPENDING`/`XCLAIM` for stuck messages.
- **Semantics:** At-least-once via PEL/claim. No exactly-once without idempotent consumer.
- **Durability:** Redis persistence-dependent (same caveat as RQ). Entries capped via `MAXLEN` / `TRIM`.
- **Retry:** PEL claim + consumer reprocess.
- **Scheduling:** Not native (use delayed stream or external scheduler).
- **Scaling:** Low latency, high throughput within Redis cluster.

### J6 — Kafka

- **Architecture:** Partitioned log, consumer groups, offset commits (auto/manual), broker replication (ISR).
- **Semantics:** **Partitioned at-least-once by default**. *Exactly-once* exists only narrowly: **Idempotent Producer (EOS idempotent) + Transactional API (read_committed isolation) for producer-consumer-DB transactional boundary** — not for arbitrary agent side-effects. Marketing often omits this narrow scope.
- **Durability:** Broker-durable (replicated log, `acks=all`). Strong.
- **Retry:** Not queue-retry but replay from offset; DLQ via Dead Letter Topic pattern must be hand-coded.
- **Scheduling:** Not native (use Kafka Streams / external scheduler).
- **Scaling:** Highest throughput (linear with partitions), replay capability. Ops heavy.

### J7 — RabbitMQ

- **Architecture:** Exchanges → queues, `ack`/`nack`/`reject`, quorum queues (Raft), TTL, DLX.
- **Semantics:** At-least-once with ack; exactly-once impossible without idempotent consumer + transactional publish (which is weaker than Kafka transactions).
- **Durability:** Quorum queues durable; transient queues not.
- **Retry:** `x-dead-letter-exchange` pattern with retry count headers + TTL.
- **Scaling:** Routing-rich but broker SPOF risk (mitigated by clustering/quorum).

### J8 — Postgres Queues (SKIP LOCKED)

- **Architecture:** Table `jobs` + `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` claim + advisory locks. `LISTEN/NOTIFY` for wake. Alembic-managed schema.
- **Semantics:** **Transactional exactly-once claim** (row-level lock race-free with ACID) but *processing* is still at-least-once if crash after claim before commit of side effect. Needs idempotent handler.
- **Durability:** ACID (strongest single-DB guarantee, no extra infra).
- **Retry:** Column `attempts` + `next_run_at` + exponential backoff computed in SQL.
- **Scheduling:** Native (cron stored in DB, `pg_cron` optional).
- **Scaling:** Vertical-bound: `SKIP LOCKED` polling overhead + vacuum at >~1-5k jobs/s depending on PG tuning. No broker to scale beyond DB.
- **Use when:** Zero extra infra desired (NEXUS constraint: modular monolith — one process + one DB sidecar).

### J9 — SQLite Job Patterns (Embedded)

- **Architecture:** Embedded DB (no server). Same `SKIP LOCKED` analogy via `BEGIN IMMEDIATE` + file lock. Sidecar file (e.g., `.jobs.sqlite3`) separate from main DB for isolation. WAL mode for concurrent readers.
- **Semantics:** Same ACID claim semantics as Postgres but single-writer (serialized writes). Suitable for single-process / small fleet.
- **Durability:** File durability (fsync via WAL checkpoint). Survives process crash; not cross-host durable without replication.
- **Retry:** Same table pattern, serialized.
- **Scaling:** Single-writer — not horizontal. Perfect for modular monolith / edge / single host.
- **Maturity:** Production for local-first apps. NEXUS `adapters/in_process_job_queue.py` implements this.

---

## 2) Comparison Matrix

| Dimension | Celery | RQ | Dramatiq | Temporal | Redis Streams | Kafka | RabbitMQ | Postgres Q | SQLite Q |
|-----------|--------|----|----------|----------|---------------|-------|----------|------------|----------|
| **Durability guarantee (processing)** | at-least-once | at-least-once | at-least-once (safer ack) | **durable execution (event-sourced)** | at-least-once | at-least-once (narrow EOS) | at-least-once | at-least-once (claim is exactly-once) | at-least-once (single writer) |
| **Exactly-once claim reality** | ❌ marketing myth | ❌ | ❌ | ✅ narrow (orchestration + idempotent activities) | ❌ | ⚠️ producer+transactional only | ❌ | ⚠️ claim, not end-to-end | ⚠️ claim, not end-to-end |
| **Retry semantics** | task-level + backoff | manual | auto exp backoff (7d) | promise + timers + compensation | PEL claim | offset replay / DLT | DLX + TTL | SQL columns + backoff | SQL columns |
| **Idempotency responsibility** | **app** | **app** | **app** | **app for side effects** | **app** | **app** | **app** | **app** | **app** |
| **Scheduling** | Beat (rich, drift) | simple | APScheduler ext | Timers + cron (strong) | none | none | DLX-based hack | native SQL + pg_cron | native SQL |
| **Locking** | broker lock | Redis lock | broker lock | workflow ID + activity lease | PEL | partition ownership | queue lock | `FOR UPDATE SKIP LOCKED` | file lock WAL |
| **Horizontal scaling** | good (workers) | weak | good | strong (cluster) | good | **best** (partitions) | good | **weak** (DB-bound) | **none** (single writer) |
| **Failure recovery** | requeue dup risk | requeue | safer requeue | **replay from event log** | claim takeover | rebalance + replay | requeue / DLQ | `next_run_at` + reconciler | sidecar + reconciler |
| **Exactly-once marketing?** | often implied, **not true** | not claimed | not claimed | sometimes overstated, **narrow** | rarely claimed | **most overstated** | not claimed | rarely claimed | not claimed |
| **Operational footprint** | Medium (broker+backend+beat) | Low (Redis only) | Low-Med | **High** (server+DB+ES) | Low (Redis) | **High** (ZK/Kraft cluster) | Medium (cluster) | **Zero** extra (if DB exists) | **Zero** extra |

---

## 3) Exactly-Once Deconstruction — Where Reality Lives

> **Quote to remember:** Celery's own doc: "guarantees delivery of the *message* to the broker, not durability of application-level task state." Kafka docs: EOS = idempotent producer + transactional consume-transform-produce within Kafka; it does not cover DB writes or HTTP side effects. Temporal docs: durable *execution history*, not automatic side-effect exactly-once.

**Five layers, one truth per layer:**

| Layer | Claim type | Truth | Achieving "once" requires |
|-------|------------|-------|---------------------------|
| **1. Produce (enqueue)** | At-least-once | Duplicate enqueue possible on client retry | De-dupe key (idempotency key / workflow ID) + DB unique constraint |
| **2. Deliver (broker→consumer)** | At-least-once (default) | Ack before process = loss; ack after = duplicate on crash | `acks_late` + PEL/claim pattern |
| **3. Process (handler run)** | At-least-once | Handler may run 2× on crash mid-effect | **Idempotent handler**: `IF NOT EXISTS` / UPSERT / deduplication table keyed by job ID |
| **4. Side effect (HTTP/DB/file)** | At-most-once without tooling | External system may see side effect 1× while queue sees failure → duplicate side effect on retry | Transactional outbox (write local DB + outbox in same txn → relay), or idempotent external API (Idempotency-Key header) |
| **5. Observe (result/report)** | At-least-once | Result may be written 0, 1, or 2× | Result dedup key, atomic `.part` → publish (as NEXUS render lane does) |

**Therefore:** True end-to-end exactly-once = **de-dupe key + idempotent handler + transactional outbox (or idempotent external service) + de-duplicated result publish**. No queue/broker achieves this alone. Temporal gets closest by persisting execution history, but even Temporal requires idempotent activities for side effects.

**Marketing verdict:**

| Technology | Marketing claim found | Verdict |
|------------|----------------------|---------|
| Celery | sometimes implies "reliable" = once | **Contradicted** — at-least-once |
| Dramatiq | "reliable" | Correctly described as at-least-once with safer ack [VERIFIED] |
| Kafka "exactly once" | conflates producer EOS with processing | **Partially contradicted** — narrow transactional scope only |
| Temporal "durable / guaranteed completion" | strong but scoping nuance needed | **Partially verified** — durable execution is real, side-effect once requires idempotence |

---

## 4) Idempotency & Dedup Patterns (Inventory)

| Pattern | How | Cost | Example |
|---------|-----|------|---------|
| **Natural idempotency** | Handler operation is set/put (`DELETE` → nop) | Free | `DELETE /file/:id` twice |
| **Idempotency key table** | `INSERT INTO dedup(job_id PK)` guards second run | One row/write | NEXUS image lane SHA256 cache + cost event dedup |
| **Conditional write** | `INSERT ... ON CONFLICT DO NOTHING` | Zero extra read | Postgres/RQ Postgres queue claim |
| **Transactional outbox** | `BEGIN; INSERT jobs; INSERT outbox; COMMIT` + relay | One extra table + pusher | Robust event publishing |
| **External Idempotency-Key** | Pass `Idempotency-Key: job_id` to Stripe etc. | Header only | HTTP tool pattern |
| **Reconciler / sweeper** | Periodic job scans `PENDING` → requeue stale claims | Timer (5m) | NEXUS checkpoint lifecycle reconciler + `nexus jobs resume` |

---

## 5) NEXUS Job System — READ-ONLY Technical Notes

- **Stack:** `adapters/in_process_job_queue.py` = SQLite sidecar `.jobs.sqlite3` + WAL + `InProcessJobQueue` implementing `application/ports/job_queue.py` (`JobQueuePort`). Queue calls = `SKIP LOCKED`-like via `BEGIN IMMEDIATE` (single-writer semantics). Tests: `tests/architecture/test_port_signatures.py` asserts port never imports adapters.
- **Constraints alignment:** The modular monolith constraint (no Celery/Redis) explicitly routes to SQLite sidecar. This is **intentional durability trade**: durability is file-level (survives process crash/restart), not cross-host; horizontal scaling is deliberately not a goal (frozen constraint). 15 architecture gates enforce no broker leakage. [VERIFIED — `tests/architecture/test_modular_monolith.py`.]
- **Durability vs Dramatiq/Celery:** At NEXUS scale (Telegram bot, Nagar renders ≤5 images/30s, 1280×720), SQLite sidecar throughput is sufficient; polling overhead is negligible. Temporal/Kafka would add operational surface without throughput justification — this is cost rationale for monolith, not a deficiency.
- **Failure coverage observed:** JobQueue payload re-validation inside adapter (B2), atomic `.part` publish + `probe_video` + `sha256` (Q3), `nexus jobs resume` reconciler for crash recovery. [READ-ONLY]
- **No recommendation to migrate** — observation only.

---

## 6) Choosing Without Marketing — Decision Framework

> Non-prescriptive; select per dimension.

| If you need… | Fails with | Pick | Why |
|--------------|-----------|------|-----|
| Zero extra infra, single host, offline-first | Kafka / Celery (broker) | **SQLite/Postgres queue** | ACID claim + reconciler, no SPOF |
| 1–5k jobs/s, low ops, Python-only | Celery (complex) | **Dramatiq** | Safer ack, 30-40% mem cut |
| Canvas chains/chords deeply embedded | Anything else | **Stay Celery** | Migration cost > saving |
| Long workflows (hours/days), human approvals, compensations | All queues | **Temporal** | Durable history + replay is architecturally unique |
| >10k events/s, fleet, replay, stream processing | Queues | **Kafka / Redis Streams** | Partition scale + replay |
| Rich routing, per-message DLX, quorum | Kafka (log) | **RabbitMQ** | Exchange-level control |

---

## Sources Cited

1. **Primary:** docs.celeryproject.org (delivery vs durability distinction, Canvas, Beat), dramatiq.io, python-rq.org, docs.temporal.io (event sourcing, activity retry, signals), kafka.apache.org (idempotent producer, transactions, ISR), redis.io docs streams (XADD/XREADGROUP/XPENDING/XCLAIM), rabbitmq.com docs (quorum, DLX), postgresql.org docs FOR UPDATE SKIP LOCKED
2. **Primary (NEXUS READ-ONLY):** `adapters/in_process_job_queue.py`, `application/ports/job_queue.py`, `creative/rendering/executor.py` (one subprocess, timeout), `storage/checkpoint_lifecycle*.py`, `tests/architecture/test_modular_monolith.py`
3. **Independent:** DevProPortal 2025 "Celery vs RQ vs Dramatiq" + MujtabaAlmas "Background Tasks & Workers" (maturity, throughput, broker table)
4. **Independent (core for verdict):** Markaicode "Temporal vs Celery: Durable Workflows or Simple Queues" (durability as architectural property, per-step overhead), SuhasBhairav "Celery vs Temporal for AI Agent Tasks" (Celery for stateless short, Temporal for long stateful), Markaicode "Celery Alternatives 2026" (Canvas lock-in, memory numbers)
5. **Independent:** Confluent/Kafka EOS docs cross-ref (via Temporal/Kafka docs — narrow transactional scope)
6. **Independent:** Starlite/ARQ Postgres queue patterns (SKIP LOCKED implementation guide)

> **Open gap:** Benchmark of Postgres SKIP LOCKED throughput vs temporal persistence for identical agent workload — no head-to-head found [UNKNOWN]. NEXUS sqlite-vec + SQLite sidecar has not been benchmarked above ~2k jobs/s in this repo — extrapolating from Postgres literature [PARTIALLY VERIFIED].

