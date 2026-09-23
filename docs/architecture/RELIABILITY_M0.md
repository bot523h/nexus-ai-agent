# Reliability M0 — Claim+Lease, Inbox/Receipt, Effect-Key Preparation

**Status:** Living document — M0 preparation
**Scope:** P1 Claim+Lease, P2 Inbox/Receipt, P4 Effect-Key contracts, fake impls, invariants
**Owner zone:** observability-reliability

This document complements `OBSERVABILITY_M0.md` with reliability substrate.

---

## 1. Why Reliability Substrate?

Research v2 defines four primitives:

- P1 Claim+Lease
- P2 Inbox/Receipt
- P3 Outbox
- P4 Effect-Key

M0 does NOT implement them fully in production (would require touching `worker.py`, `bot/app.py`). Instead, M0 provides domain contracts, protocol types, pure helpers, invariant tests, SQLite/Postgres-neutral interfaces, fake implementations, and concurrency property tests that are compatible with existing architecture.

When delivery engine later implements P1/P2, system will be observable, measurable, provable.

---

## 2. P1 Claim+Lease

**Problem:** Two workers claiming same job concurrently => exactly one winner must be guaranteed. Expired lease must be recoverable.

**Interface:** `ClaimLeasePort` in `reliability.py`

- `try_claim(resource_id, owner_id, ttl) -> (ClaimResult, Lease|None)`
- `renew(resource_id, owner_id, ttl) -> bool`
- `release(resource_id, owner_id) -> bool`
- `get_lease(resource_id) -> Lease|None`
- `recover_expired(now) -> list[str]`

**Lease:** `Lease` dataclass with `resource_id`, `owner_id`, `claimed_at`, `expires_at`, `version`, `is_expired()`, `is_owned_by()`

**Fake:** `InMemoryClaimLease` — thread-safe, in-memory, invariant: at most one owner per resource_id.

**Invariants:**

- A: two claims concurrent => exactly one winner — `invariant_single_winner_claim`
- C: expired lease => recoverable — `invariant_lease_recoverable`

**Tests:**

- `test_claim_single_winner_concurrent` — two threads, one winner
- `test_claim_same_owner_idempotent` — same owner idempotent
- `test_claim_expired_recoverable` — expired => new owner can claim
- `test_recover_expired_lists_recovered`
- `test_lease_renew`, `test_lease_release`
- Property: 5 owners concurrent, still one winner

**Future Postgres:** `SELECT ... FOR UPDATE` or `INSERT ... ON CONFLICT` with `expires_at` check. SQLite: `BEGIN IMMEDIATE`.

---

## 3. P2 Inbox/Receipt

**Problem:** Same Telegram `update_id` delivered twice (Telegram retry on 503) => one logical receipt.

**Interface:** `InboxPort`

- `try_receive(message) -> (is_new, receipt|None)`
- `store_receipt(receipt)`
- `get_receipt(message_id)`

**Types:**

- `InboxMessage`: `message_id` (e.g., update_id), `payload`, `received_at`
- `Receipt`: `message_id`, `receipt_id`, `processed_at`, `result`

**Fake:** `InMemoryInbox` — deduplicates by `message_id`, one logical receipt.

**Invariant:**

- B: same update_id twice => one logical receipt — `invariant_inbox_idempotent`

**Tests:**

- `test_inbox_same_update_id_one_receipt`
- `test_inbox_concurrent_processing`

**Future table:** `inbox (message_id PK, receipt_json, processed_at)` with UNIQUE.

---

## 4. P4 Effect-Key

**Problem:** Duplicate logical effect (e.g., send_message with same chat_id+text) should have stable effect-key, so second execution can return cached result.

**Function:** `compute_effect_key(operation, params, namespace="nexus")`

- Canonical JSON: sorted keys, separators (",", ":")
- SHA-256, truncated to 32 hex chars
- Stable: same operation+params => same key
- No secret leakage: hash is one-way

**Types:**

- `EffectRecord`: `effect_key`, `operation`, `result`, `created_at`
- `EffectStorePort`: `try_record_effect(record) -> (is_new, existing)`, `get_effect(key)`

**Fake:** `InMemoryEffectStore` — thread-safe.

**Invariant:**

- D: duplicate logical effect => effect-key stable — `invariant_effect_key_stable`

**Tests:**

- `test_effect_key_stable`
- `test_effect_key_different_params_different_key`
- `test_effect_key_sorted_params`
- `test_effect_store_deduplication`
- `test_effect_key_no_secret_leakage`

---

## 5. No Big Infra

M0 uses only:

- threading.Lock (std lib)
- hashlib, uuid, datetime (std lib)
- No Redis, Kafka, Celery, microservices

---

## 6. Integration Points (Future)

- `bot/handlers.py`: before processing update, check inbox; if duplicate, return existing receipt
- `InProcessJobQueue`: before enqueue, check inbox? Actually job queue already has idempotency_key UNIQUE — similar to inbox
- `worker.py`: before executing handler, try_claim job_id; on success, execute; on expired lease, recover
- Job handlers: before side effect (send_message), compute effect_key, check effect store, if exists return cached

These wirings are NOT done in M0 to avoid stealing delivery engine. M0 provides contracts and tests.

---

## 7. Files

- `src/nexus_ai_agent/observability/reliability.py` — contracts, fakes, invariants
- `tests/unit/test_reliability_m0.py` — 14 tests
- This doc — living view

---

## 8. References

- `OBSERVABILITY_M0.md` — M0 observability
- `research/v2/13-target-architecture.md` — target architecture (P1-P4)
- `docs/architecture/RUNTIME_FLOWS.md` — job queue flow
- `docs/architecture/OBSERVABILITY.md` — O1 observability

---

## 9. Runtime Integration Touchpoint (task-172, 2026-09-23)

`InstrumentedJobQueue` (`adapters/instrumentation/`) is the runtime carrier
for M0 observability events. Reliability contracts in this document remain
CONTRACT-ONLY (P1/P2/P4 wiring deferred per §6); what changed is the event
substrate they will observe: the five queue events now fire at both
composition roots (`bot/app.py`, `cli.py jobs resume`) with correlation,
bounded labels, histogram, and saturation gauges — proven by
`tests/integration/test_m0_queue_runtime.py` (Q1–Q5 + M1–M4) and
`tests/architecture/test_m0_wiring.py` (static import graph). Correlation
chain, effect-key, and update_id remain three distinct identifiers: only the
first is wired today.
