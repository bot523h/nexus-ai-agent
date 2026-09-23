# Research V2 — P1 CLAIM+LEASE and P2 INBOX/RECEIPT

**Status:** Living document (implemented substrate)
**Owner claim:** `task-160-research-v2-p1-p2` / `arena/01a0cf30-nexus-ai-agent`
**Verified against:** this branch after P1/P2 landing
**Scope:** the first two Research V2 primitives only — not P3/P4

This page records the evidence trail, the options considered, and the
contracts the code must keep. Claims are tagged:

| Tag | Meaning |
|---|---|
| **VERIFIED** | Observed in this tree or proven by a test in this PR |
| **INFERENCE** | Reasonable conclusion from verified facts + prior art |
| **RECOMMENDATION** | Chosen design for Nexus |
| **UNKNOWN** | Not resolved; do not pretend otherwise |

---

## 0. What the live tree had before this work

**VERIFIED**

| Fact | Evidence |
|---|---|
| Durable jobs live in SQLite sidecar `nexus_job_queue` | `adapters/in_process_job_queue.py` |
| Statuses: `pending` / `processing` / `completed` / `failed` | `application/ports/job_queue.py::JobStatus` |
| Enqueue is idempotent on `idempotency_key` | UNIQUE column + `_insert_or_get` |
| No `owner_id`, no lease, no fencing token | `PRAGMA`/CREATE TABLE before this PR |
| `_mark_processing` is not a multi-worker claim (no rowcount winner) | same adapter |
| `resume_pending` reset *all* pending+processing | pre-P1 `_reset_unfinished` |
| Webhook path checks `update_id` presence, does **not** durable-dedup | `api/app.py`, `bot/app.py` |
| No `research/v2/` tree and no prior task-160 on the board | `git ls-remote`, board show 2026-09-23 |
| Redis/Celery are forbidden | `tests/architecture/test_modular_monolith.py` |

**INFERENCE:** the modular-monolith constraint means P1/P2 must stay on
SQLite (same sidecar family), not introduce a broker.

**UNKNOWN:** when/whether a second OS process will drain the same sidecar
in production today (composition root is still single-process). The
primitive is still required so that day does not invent ownership ad hoc.

---

## 1. Five independent searches — P1 CLAIM strategy

| # | Trail | Core finding |
|---|---|---|
| 1 | Postgres `SELECT … FOR UPDATE SKIP LOCKED` job queues | Atomic claim = lock + status flip in one statement; losers skip, never block |
| 2 | SQLite `UPDATE … WHERE status='pending'` + `rowcount==1` / `BEGIN IMMEDIATE` | Writer lock serialises; CAS on status is the winner signal without SKIP LOCKED |
| 3 | Lease + version fencing (Kleppmann / production worker fences) | Time-bounded lease ≠ exclusivity; resource must reject stale token/generation |
| 4 | Queue visibility timeout (SQS-class) | Expiry restores liveness; without fencing, expired holders still complete |
| 5 | In-tree prior art | `idempotency_key` UNIQUE, creative bus idempotency map, board lease TTL — patterns already valued here |

### Options

| Option | Correctness | Crash safety | Concurrency | Ops simplicity | Migration | Fit with Nexus |
|---|---|---|---|---|---|---|
| A. SKIP LOCKED (Postgres-only) | high | high | high | medium | needs PG queue | **poor** — queue is SQLite by decision |
| B. Broker (Redis/Celery) | high | high | high | low | high | **forbidden** |
| C. In-process `asyncio.Lock` only | low multi-proc | low | low | high | none | insufficient |
| D. **SQLite BEGIN IMMEDIATE + CAS UPDATE + lease_token/version** | high | high | high (process-local + multi-conn) | high | additive ALTER | **best fit** |

**RECOMMENDATION (chosen): D.**

- Claim: `BEGIN IMMEDIATE` + `UPDATE … WHERE pending OR (processing AND expired)` + `rowcount==1`.
- Fence: `lease_token` (opaque per claim) + monotonic `lease_version`.
- Heartbeat / complete / fail / release all require matching owner+token+version.
- Stale / unfenced (`lease_expires_at IS NULL`) processing is reclaimable (preserves historical resume semantics).

**UNKNOWN:** multi-host SQLite over a network filesystem — out of scope; Nexus already assumes local/durable disk for the sidecar.

---

## 2. Five independent searches — P2 INBOX

| # | Trail | Core finding |
|---|---|---|
| 1 | Telegram `update_id` dedup patterns | At-least-once delivery; high-water mark drops out-of-order lower ids (known bug class) |
| 2 | Inbox / outbox messaging | Inbox = consumer-side idempotency table; outbox = producer dual-write (P2 is inbox only) |
| 3 | Exactly-once vs at-least-once | True exactly-once is rare; practical target is at-least-once + idempotent consumer |
| 4 | Idempotent consumer DB patterns | UNIQUE insert arbitrates races; claim+effect should share a transaction when possible |
| 5 | Receipt / uniqueness table designs | `message_receipts(pk=idempotency_key)` + status machine for poison/retry |

### Options

| Option | Dedup correctness | Out-of-order safe | Crash safety | Ops | Fit |
|---|---|---|---|---|---|
| A. In-memory set | low across restart | yes | low | high | insufficient |
| B. High-water `last_update_id` | false under concurrent/out-of-order | **no** | medium | high | reject |
| C. Redis SETNX TTL | high | yes | medium | adds Redis | **forbidden** |
| D. **SQLite inbox table PK(update_id) + receipt state machine** | high | yes | high | high | **best fit** |

**RECOMMENDATION (chosen): D.**

State machine:

```text
received  → processing | dead
processing → processed | dead | received   (received = reclaim after crash)
processed / dead are terminal
```

Accept semantics: first insert → `ACCEPTED`; unique conflict → `DUPLICATE` with the existing receipt (payload of the replay is ignored).

**UNKNOWN:** multi-bot / multi-tenant `update_id` namespaces if one process hosts several bot tokens — not required today; extend PK to `(bot_id, update_id)` only when that composition exists.

---

## 3. Public surface (this PR)

| Piece | Path | Role |
|---|---|---|
| Domain lease vocabulary | `domain/lease.py` | outcomes, `JobLease`, expiry helpers |
| Domain inbox vocabulary | `domain/inbox.py` | `ReceiptStatus`, transitions, `UpdateReceipt` |
| Claim store | `adapters/job_claim_lease.py` | `ClaimLeaseStore` |
| Inbox store | `adapters/update_inbox.py` | `UpdateInboxStore` |
| Package re-export | `research/__init__.py` | single import root |
| Queue DDL | `adapters/in_process_job_queue.py` | additive lease columns + lease-aware `_reset_unfinished` |
| Port | `application/ports/job_queue.py` | **unchanged** method surface (architecture gate) |

Hard exclusions respected: no edits to `bot/handlers.py`, `bot/app.py`,
`worker.py`, security middleware, surfaces, ads/onboarding, workflows,
`AGENTS.md`, or `docs/README.md`.

**INFERENCE:** wiring the inbox into the webhook path is a later composition
task (touches excluded files / PR#33 surface). P2 delivers the durable
primitive and tests; call-site integration is intentionally separate.

---

## 4. Failure contracts

| Situation | Observable result |
|---|---|
| Two workers claim one pending job | Exactly one `CLAIMED`, one `NOT_AVAILABLE` |
| Holder pauses past TTL | Another worker may reclaim; version bumps |
| Stale holder completes after reclaim | `STALE_TOKEN`; row unchanged by stale write |
| `resume_pending` while live lease exists | Live row **not** reset to pending |
| Same `update_id` accepted 12× concurrently | 1 `ACCEPTED`, 11 `DUPLICATE` |
| Replay after `processed` | `DUPLICATE` + original receipt; no second effect |
| Illegal receipt transition | `ReceiptTransitionError` |
| Receipt token mismatch | `PermissionError` |

---

## 5. Test evidence map

| Criterion | Test |
|---|---|
| Atomic single winner | `tests/integration/test_claim_lease_concurrency.py::test_two_workers_one_job_exactly_one_winner` |
| N workers / N jobs, no dup | `…::test_n_workers_n_jobs_no_double_claim` |
| Stale complete rejected | unit + integration fencing tests |
| Live lease survives resume | `…::test_resume_pending_does_not_steal_live_lease` |
| Inbox concurrent accept | `tests/integration/test_inbox_receipt_dedup.py::test_concurrent_accept_same_update_id_one_accepted` |
| Replay noop | `…::test_replay_after_processed_is_noop` |
| Out-of-order ids | `tests/unit/test_update_inbox_receipt.py::test_out_of_order_ids_are_independent` |
| Pre-P1 sidecar upgrade | `tests/unit/test_job_claim_lease.py::test_pre_p1_sidecar_upgrades_in_place` |
| Existing queue green | `tests/integration/test_in_process_job_queue.py` (regression) |

---

## 6. Compatibility with P3/P4

**RECOMMENDATION:** keep P1/P2 free of scheduler policy, rate limits, and
research-plan graphs. P3/P4 may *consume* `ClaimLeaseStore` and
`UpdateInboxStore` but must not widen their schemas without a new decision
record.

**UNKNOWN:** P3/P4 concrete shapes — owned by the next agent; not speculated here.
