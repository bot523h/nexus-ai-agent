# Effect Consistency — P3 (Transactional Outbox) + P4 (Effect-Key / Idempotent Effect)

**Status:** dated audit record (not a source of truth; the living views are `docs/architecture/*` and the enforcement tests)
**Owner session:** `arena/01a0cf31-nexus-ai-agent` · **Board task:** `task-160` (zone `effect-consistency`) · **Branch head:** see git
**Date:** 2026-09-23

The mission was **not** to build "magic exactly-once". It was to build a layer
that (a) tolerates failure, (b) recognises duplicates, (c) keeps every effect
recoverable and (d) proves every claim with an invariant and a test. The four
repository hazards this closes:

> DB says "done" but the external effect did not happen.
> The external effect happened but the DB does not know.
> A job retry double-sends / double-uploads.

---

## 1. What was delivered (P3)

- **Pure outbox state machine** — `src/nexus_ai_agent/domain/policies/outbox_policy.py`
  (`PENDING → CLAIMED → SUCCEEDED | FAILED_RETRYABLE | FAILED_PERMANENT`,
  `FAILED_RETRYABLE → PENDING`). Allowed/invalid transitions are enumerated;
  retry is bounded (`MAX_ATTEMPTS=5`, backoff `min(2**(attempt-1), 60)`); a lease
  is 60s; a permanent failure is terminal on first sight; **no dead-letter state
  without the evidence-gated toggle** (`would_dead_letter`).
- **Delivery-intent contract** — `src/nexus_ai_agent/application/ports/outbox_port.py`
  (`OutboxPort.append_intent` / `inspect_intent`, `EffectAdapter.deliver`,
  `DeliveryError(retryable=…)`, `EffectConsistencyError` for a non-atomic
  environment). Framework-free, frozen-baseline (R1/R13).
- **Reference dispatcher** — `src/nexus_ai_agent/adapters/effect/outbox_dispatcher.py`.
  One SQLite sidecar owns `nexus_effect_outbox`; atomic intent insert via
  `ON CONFLICT (operation_type, effect_key) DO NOTHING`; single-statement claim
  via a guarded UPDATE (SQLite's serialised-writer analogue of
  `FOR UPDATE SKIP LOCKED`); `attempt_id`-fenced outcome writes; no payloads in
  logs; `last_error` truncated at 300 chars.
- **Effect delivery adapter registry** — `src/nexus_ai_agent/adapters/effect/effect_delivery.py`
  (fail-closed missing-adapter, never a silent SUCCEEDED).
- **Observability backend** — `src/nexus_ai_agent/adapters/effect/observability_backend.py`
  (`effect_created/claimed/succeeded/failed/recovered/duplicate` events +
  `nexus_effect_<state>_total` counters on the allow-listed registry; keys travel
  only as a truncated digest; no `telegram_id`/payload/secrets/high-cardinality labels).
- **Reconciliation contract** — `classify_row` / `recovered_rows` in the policy:
  stale `CLAIMED`, old `PENDING`, due `FAILED_RETRYABLE`, and terminal rows with a
  ghost lease owner (C4 residue) are each classified deterministically — recovered,
  waited on, or surfaced as `unknown`, never guessed into `SUCCEEDED`.

## 2. What was delivered (P4)

- **Deterministic effect key** — `src/nexus_ai_agent/domain/policies/effect_key.py`.
  `sha256(canonical_json(operation_type, logical_entity, logical_revision,
  destination, logical_slot))` — deterministic, retry-stable, distinct per
  intent, and **independent of the attempt id** (the forbidden
  `random_uuid_every_retry()` pattern is muted by construction and by tests).
- **Dedupe at the database** — the `UNIQUE(operation_type, effect_key)`
  constraint, not an in-memory set; cross-process backstop tested.
- **Told-apart identity** — the attempt id (`attempt_id`) and the logical key
  are distinct columns; a re-claim reuses the key but mints a new attempt.

## 3. Effect semantics (the honest ladder)

Never "exactly-once" merely because a unique index exists.

| Effect class | Delivery | Logical effect | Why |
|---|---|---|---|
| `telegram.send` | at-least-once | at-most-once intent | no provider idempotency token; connect-lost-after-send + retry *is* a second message — the system must not claim to stop the network |
| `telegram.edit` | at-least-once | effectively-once | same text to the same message is observationally one effect |
| `r2.upload` (PUT) | at-least-once | effectively-once | object key names the destination; a second PUT of identical content is a no-op |
| `http.callback` | at-least-once | at-least-once unless the receiver honours `Idempotency-Key` | receiver policy is out of process; the key makes receiver-side dedupe *possible* |
| DB insert | exactly-once by constraint | effectively-once | the database serialises writes |

## 4. Crash matrix (each row is a deterministic test, no sleeps)

| Id | Scenario | Invariant → expected state → recovery path | Test |
|---|---|---|---|
| C1 | commit → crash before dispatch | row stays PENDING → reclaim → deliver | `test_C1_commit_then_crash_before_dispatch_stays_pending` |
| C2 | effect → crash before success record | retry reuses the same effect key; row retryable | `test_T4_crash_after_remote_effect_leaves_ambiguity_then_same_key` |
| C3 | two dispatchers race | one logical effect (`attempt_id` + guarded UPDATE) | `test_T3_two_dispatchers_race_one_effect` |
| C4 | timeout / unknown remote result | ambiguity explicit → retryable, never laundered to SUCCEEDED | `test_T4…`, policy `classify_row → unknown` |
| C5 | stale lease | another worker reclaims; stale claimant fenced | `test_T7_stale_lease_recovery_is_safe`, `test_stale_claimant_cannot_overwrite_a_newer_attempt` |

## 5. Tests — exact commands and results

```bash
# targeted (the new proof)
pytest tests/unit/test_effect_key.py tests/unit/test_outbox_policy.py \
       tests/unit/test_effect_dedup.py tests/unit/test_outbox_dispatcher.py \
       tests/integration/test_outbox_crash_matrix.py \
       tests/architecture/test_import_boundaries.py \
       tests/architecture/test_modular_monolith.py \
       tests/architecture/test_port_signatures.py -q
# → 54 passed

# full suite, zero regression (dev extras installed; the 20 skips are pre-existing env-gated optional paths)
pytest -q -m "not slow"
# → 1885 passed, 20 skipped

ruff check src/nexus_ai_agent/adapters/effect/ src/nexus_ai_agent/domain/policies/effect_key.py \
           src/nexus_ai_agent/domain/policies/outbox_policy.py src/nexus_ai_agent/application/ports/outbox_port.py \
           tests/unit/test_effect_key.py tests/unit/test_outbox_policy.py tests/unit/test_effect_dedup.py \
           tests/unit/test_outbox_dispatcher.py tests/integration/test_outbox_crash_matrix.py
# → All checks passed!

mypy src/nexus_ai_agent/adapters/effect/ src/nexus_ai_agent/domain/policies/effect_key.py \
     src/nexus_ai_agent/domain/policies/outbox_policy.py src/nexus_ai_agent/application/ports/outbox_port.py
# → Success: no issues found in 7 source files
```

## 6. Mutation proof

| Mutation | Method | Result |
|---|---|---|
| **A — effect key randomised per retry** (`+ uuid.uuid4().hex` appended in `effect_key()`) | `pytest tests/unit/test_effect_key.py tests/unit/test_effect_dedup.py tests/integration/test_outbox_crash_matrix.py -q` | **FAILED — 5 tests red** (determinism, stability, attempt-id independence, T4) |
| **B — remove `UNIQUE(operation_type, effect_key)`** | `pytest tests/unit/test_effect_dedup.py -q` | **FAILED — 6 tests red** (the whole dedupe surface, incl. the cross-process backstop) |
| restore A then B | re-run | **GREEN** (44 in the new suite) |

Mutation B *first* ran green on the unit layer; that was the honest signal the
guard was incomplete (dedupe only as an in-process SELECT→INSERT under one
lock). The store was made race-correct (`ON CONFLICT … DO NOTHING` +
cross-connection backstop test) and the suite now turns red when the constraint
disappears.

## 7. Five-search decisions (each choice is documented, not just asserted)

### A — Transactional outbox (pattern + mechanics)

1. *Official transactional outbox* (microservices.io / Richardson) — the rule: business state + "please perform this effect" in one local transaction; publish out-of-band.
2. *PostgreSQL outbox production pattern* — `FOR UPDATE SKIP LOCKED`, claim-before-publish, don't hold a transaction across the network call.
3. *Failure modes of outbox dispatchers* — at-least-once only; exactly-once is a consumer/idempotency problem.
4. *Polling vs CDC* — polling = simple, explicit, dependency-free; CDC = lower latency at the cost of connector infra.
5. *Real production prior art* — lease columns (`lease_owner`, `lease_until`, `attempt_count`) as the claim token; short claim txn, fault-tolerance, idempotent consumers.

**Chosen:** database-first polling dispatcher with a lease+claim protocol and
`ON CONFLICT` dedupe, executed on an owned SQLite sidecar.
**Rejected:** CDC/Debezium (infra, violates the no-new-infra guardrail),
broker-based outbox (Redis/Kafka forbidden by R4), in-memory dispatch (dies with
the process, the exact hazard being closed).

### B — Idempotency / effect identity

1. Stripe idempotency: key names *one intent*, cache the outcome, reject key reuse with different parameters.
2. `Idempotency-Key` HTTP semantics (IETF draft): client-minted, unique per operation, never reused for a different payload.
3. Database unique-key idempotency: `ON CONFLICT DO NOTHING` is the atomic dedupe; treat the conflict as the success path.
4. Distributed retry dedupe: key derives from intent, never a fresh attempt UUID.
5. Object-storage/messaging idempotent writes: PUT-by-key is idempotent; POST/messaging needs explicity dedupe identity.

**Chosen:** deterministic key from canonicalised logical intent; UNIQUE
constraint as the atomic dedupe; attempt-id kept as a separate column.
**Rejected:** random-UUID keys per attempt (the failure mode P4 forbids),
in-memory dedupe sets (forgotten across the very crash that causes the retry).

### C — Recovery

1. Outbox crash recovery: rows survive as PENDING; orphaned CLAIMED needs a recovery rule.
2. Reconciliation patterns: periodic self-healing sweep; explicit state decisions over "some streaming protocol fixes it".
3. Visibility timeout + retry: the lease is ad-vice, not an acknowledgement; a stale acquired row must be fenced.
4. Dead-letter semantics: quarantine only for permanently-failing poison, after an explicit retry budget; alert at depth > 0.
5. RTO/RPO drills: recoverable-by-construction beats a backup you've never restored.

**Chosen:** lease + attempt-id fencing; deterministic `classify_row` reconciler
core; permanent failures stay *visible* in the outbox (no silent DLQ without
evidence).
**Rejected:** dead-letter-by-default, crash-then-forget, "unknown result →
SUCCEEDED" laundering.

### Schema decision (five investigations)

1. Expand/contract: additive columns/tables, never destructive changes; separate expand/contract revisions.
2. Same-DB outbox (atomicity for free) vs separate-DB sidecar (no atomicity).
3. Dual-write / compatibility-view: migrate reads and writes in phases when the schema is someone else's.
4. Alembic ownership/conflicts: migrations are deployable product changes, not per-feature artifacts.
5. Single-writer SQLite queue: guarded UPDATE claim; store idempotency with the domain change.

**Chosen:** an additive sidecar store created by the dispatcher's own `CREATE
TABLE IF NOT EXISTS` — **no new Alembic revision** — because the canonical
transaction that would justify a shared migration lives in Agent 3's
`core/async_db.py` (out of this session's zone). The OutboxPort contract
demands (via `EffectConsistencyError`) that a real integration share the owning
transaction.

## 8. Recovery / DR (RTO/RPO movement)

From *undefined* toward a **measurable contract**: the deterministic crash matrix
(C1–C5) is the RTO/RPO probe for the effect outbox — every row is recoverable
from the store alone, so RPO for *effect intent* is the commit point (zero lost
commits), and RTO is `next dispatcher sweep + lease remainder`. Storage
replication itself is **out of scope** (recorded, not built). The recovery drill
is exactly `test_C1…` + `test_T7…`: insert intent → simulate crash (no dispatch) →
restart (new store instance) → reclaim → complete → verify no duplicate logical
effect.

## 9. Files

New:
```
src/nexus_ai_agent/domain/policies/effect_key.py
src/nexus_ai_agent/domain/policies/outbox_policy.py
src/nexus_ai_agent/application/ports/outbox_port.py
src/nexus_ai_agent/adapters/effect/__init__.py
src/nexus_ai_agent/adapters/effect/effect_delivery.py
src/nexus_ai_agent/adapters/effect/observability_backend.py
src/nexus_ai_agent/adapters/effect/outbox_dispatcher.py
tests/unit/test_effect_key.py
tests/unit/test_outbox_policy.py
tests/unit/test_effect_dedup.py
tests/unit/test_outbox_dispatcher.py
tests/integration/test_outbox_crash_matrix.py
EFFECT_CONSISTENCY_P3P4_2026-09-23.md   (this report; root-level, matching the repo's dated-report convention)
```
Updated (living views, in place — no new doc index entries needed, so the fenced docs/README.md stayed untouched):
```
docs/architecture/DATA_AND_STORAGE.md   (effect outbox store + semantic ladder)
docs/architecture/MODULE_MAP.md          (R13 law → enforcing tests; seven ports)
docs/architecture/OBSERVABILITY.md       (effect events + counters + banned labels)
docs/architecture/PORTS.md               (OutboxPort contract)
.agents/board.json                        (claim + zone + 10-task forward network)
```

## 10. Blockers / follow-ups (queued, with owners)

- `docs/DECISION_LOG.md` append (D-0010) — fenced by PR#33/D-0009 ownership → queued `task-161`.
- Wiring `append_intent` into a real shared-transaction write — needs `core/async_db.py` Phase 1 (Agent 3's zone) → queued `task-162`, prerequisite `task-128`.
- Full own-zone reconciliation CLI → `task-166`; multi-process claim test → `task-165`; RTO/RPO drill doc → `task-169`.

The ten-task forward network is in `.agents/board.json → next_work`.

## 11. Honest limits

The outbox removes the *lost-intent* and *silent-success* hazards. It does **not**
make a Telegram message reach a phone exactly once; it makes the *logical effect*
at-most-once, the *attempt* visible and bounded, and the *ambiguity* explicit.
That is the contract, and it is enforced by tests, not by prose.
