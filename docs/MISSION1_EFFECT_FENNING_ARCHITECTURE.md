# MISSION 1: External Side-Effect Fencing (P0)

**Status:** DEFERRED (blocked by PR #283 merge + task-256 lease release)
**Owner:** arena/1f7a74db-nexus-ai-agent
**Extends:** task-160 effect-consistency infrastructure (P3 outbox + P4 effect-key)

## Problem

When the heartbeat stops, the running handler does NOT stop. Recovery creates a
new attempt (attempt 2), and both attempts may execute external side effects.
This violates the core invariant: **one durable queue, one execution authority,
one fencing contract**.

Concretely:
1. **Handler constructs its own authority from payload** — the handler decides
   what external effect to perform based on the job payload, without any
   queue-issued authorization.
2. **No attempt-bound effect authorization** — attempt 1 and attempt 2 both
   have access to perform the same external effect.
3. **No attempt-specific fencing** — there is no mechanism to prevent an older
   attempt from creating a duplicate logical operation.
4. **No ambiguous-outcome handling at the effect level** — the outbox handles
   delivery ambiguity (C1-C5), but not the case where attempt 1 may have
   already executed the effect before attempt 2 starts.

## How Task-160 Is Extended (Not Duplicated)

Task-160 (merged via PR #59) provides:

| Component | File | What it provides |
|---|---|---|
| Effect key | `domain/policies/effect_key.py` | Deterministic logical effect key (sha256 of canonical JSON) |
| Outbox policy | `domain/policies/outbox_policy.py` | Pure state machine + crash matrix C1-C5 |
| Outbox port | `application/ports/outbox_port.py` | Durable delivery-intent contract |
| Outbox dispatcher | `adapters/effect/outbox_dispatcher.py` | Reference dispatcher (claim→deliver→record) |
| Effect delivery | `adapters/effect/effect_delivery.py` | Effect handler helpers |
| Observability | `adapters/effect/observability_backend.py` | Delivery metrics |

**MISSION 1 extends these by adding:**

| New Component | What it adds |
|---|---|
| Typed execution context | Queue-issued context with attempt identity, fencing token, effect authorization |
| Attempt-bound effect authorization | The handler receives authorized effect operations, never constructs its own |
| Effect gateway | Intercepts all external effects, enforces attempt fencing |
| Effect ledger | Prepared/In progress/Confirmed successful/Confirmed failed/Unknown |
| Attempt-specific fencing | Older attempt cannot create duplicate logical operation |

### What stays in task-160

- Effect key computation (deterministic, retry-stable)
- Outbox state machine (PENDING→CLAIMED→SUCCEEDED|FAILED_RETRYABLE|FAILED_PERMANENT)
- Dispatcher claiming and delivery (at-least-once, dedupe by effect key)
- Crash matrix C1-C5 (classify_row, recovered_rows)

### What MISSION 1 adds on top

- **Execution context** — wraps the handler's `payload` with queue-issued
  `attempt_id`, `fencing_token`, and `effect_authorization`
- **Effect gateway** — the handler calls `gateway.perform(effect_intent)` instead
  of directly calling external APIs. The gateway checks:
  1. Is this attempt still authorized? (fencing token matches current)
  2. Has this logical effect already been confirmed? (effect ledger)
  3. Is the effect key stable across retries? (deterministic key from task-160)
- **Effect ledger** — a durable record of effect-operation states:
  - `PREPARED` — intent recorded, not yet executed
  - `IN_PROGRESS` — execution started, outcome unknown
  - `CONFIRMED_SUCCESS` — execution confirmed successful
  - `CONFIRMED_FAILED` — execution confirmed failed (permanent)
  - `UNKNOWN` — outcome uncertain, requires reconciliation

## Design

### Typed Execution Context

```python
@dataclass(frozen=True)
class ExecutionContext:
    attempt_id: str  # Queue-minted attempt identifier
    fencing_token: int  # Monotonically increasing fencing token
    effect_authorization: EffectAuthorization
    # ... existing payload fields ...
```

The `EffectAuthorization` lists the effect operations this attempt is allowed
to perform. The handler NEVER constructs its own authority from the payload.

### Effect Gateway

```python
class EffectGateway:
    async def perform(
        self,
        *,
        context: ExecutionContext,
        effect_intent: EffectIntent,
    ) -> EffectOutcome:
        """Perform one external effect, with attempt fencing."""
        # 1. Check fencing: is this attempt still authorized?
        if not self._is_authorized(context):
            raise StaleAttemptError(context.attempt_id)

        # 2. Check ledger: has this logical effect already been confirmed?
        ledger_entry = self._ledger.get(effect_intent.effect_key)
        if ledger_entry is CONFIRMED_SUCCESS:
            return EffectOutcome(deduped=True)

        # 3. Record intent in ledger (PREPARED)
        self._ledger.mark_prepared(effect_intent)

        # 4. Perform the effect
        try:
            result = await self._adapter.deliver(
                effect_key=effect_intent.effect_key,
                payload=effect_intent.payload,
                destination=effect_intent.destination,
            )
            self._ledger.mark_confirmed_success(effect_intent)
            return EffectOutcome(result=result)
        except DeliveryError as exc:
            if not exc.retryable:
                self._ledger.mark_confirmed_failed(effect_intent)
                raise
            self._ledger.mark_unknown(effect_intent)
            raise
```

### Effect Ledger

The ledger extends the outbox's terminal states with the ambiguous-outcome
handling required by MISSION 1:

```
PREPARED → IN_PROGRESS → CONFIRMED_SUCCESS (terminal)
                      → CONFIRMED_FAILED (terminal, permanent)
                      → UNKNOWN (requires reconciliation)
```

The `UNKNOWN` state is the key addition: when a crash occurs between
`IN_PROGRESS` and the outcome write, the ledger entry is `UNKNOWN`. The
reconciler must NOT retry the effect until it has determined the outcome:
- If the provider supports idempotency keys: retry with the same key (safe)
- If the provider does NOT: reconcile by querying the destination (if possible)
- If reconciliation is impossible: surface to operator, never guess

### Attempt-Specific Fencing

The fencing token is the queue's existing mechanism. MISSION 1 extends it to
the effect level:

1. The queue mints a fencing token per reservation (already implemented).
2. The execution context carries the token.
3. The effect gateway checks the token against the queue's current token before
   every effect operation.
4. If the token is stale (attempt 1 after attempt 2 has been minted), the gateway
   raises `StaleAttemptError` — the effect is NOT performed.

This proves: **older attempt cannot create duplicate logical operation**.

## Adversarial Matrix (10 Cases)

| # | Scenario | Expected Behavior |
|---|---|---|
| 1 | Attempt 1 starts, performs effect A, heartbeat stops | Attempt 2 starts, gateway refuses effect A (already confirmed) |
| 2 | Attempt 1 starts, performs effect A, crash before outcome write | Ledger: UNKNOWN. Attempt 2 reconciles before retry |
| 3 | Attempt 1 and attempt 2 both try to perform effect A simultaneously | Only one wins the fencing check; the other gets StaleAttemptError |
| 4 | Attempt 1 performs effect A (confirmed), attempt 2 tries effect A again | Gateway returns deduped (CONFIRMED_SUCCESS already in ledger) |
| 5 | Attempt 1 performs effect A (confirmed), attempt 2 tries effect B (different key) | Gateway allows effect B (different logical effect) |
| 6 | Attempt 1 crashes after PREPARED, before IN_PROGRESS | Ledger: PREPARED. Attempt 2 sees PREPARED, reconciles |
| 7 | Attempt 1 crashes after IN_PROGRESS, provider has no idempotency | Ledger: UNKNOWN. Reconciliation required before retry |
| 8 | Attempt 1 crashes after IN_PROGRESS, provider has idempotency | Ledger: UNKNOWN. Retry with same key is safe |
| 9 | Handler tries to construct effect authorization from payload | Type system prevents this (context is queue-issued, not payload) |
| 10 | Handler tries to perform effect not in authorization list | Gateway raises UnauthorizedEffectError |

## Limitations by Provider

| Provider | Idempotency Support | MISSION 1 Behavior |
|---|---|---|
| Telegram (sendMessage) | No native idempotency | At-least-once delivery; duplicate messages possible if UNKNOWN. Reconciliation by message dedup (text+timestamp) |
| R2 (upload) | Effectively-once (key-based) | Safe to retry with same key; duplicate uploads overwrite |
| HTTP callback | Depends on receiver | If receiver honours Idempotency-Key: safe. Otherwise: at-least-once, duplicate callbacks possible |
| DB insert | Exactly-once (constraint) | UNIQUE constraint prevents duplicate; safe to retry |

## Implementation Plan

1. **Phase 1: Execution context** — Add `ExecutionContext` with attempt_id,
   fencing_token, effect_authorization. Wire into handler invocation.
2. **Phase 2: Effect gateway** — Build `EffectGateway` that wraps
   `EffectAdapter.deliver` with fencing checks.
3. **Phase 3: Effect ledger** — Extend outbox schema with ledger states
   (PREPARED, IN_PROGRESS, UNKNOWN). Add reconciliation logic.
4. **Phase 4: Handler migration** — Migrate handlers to use `gateway.perform()`
   instead of direct external calls.
5. **Phase 5: Adversarial tests** — Implement the 10-case adversarial matrix.
6. **Phase 6: Mutation probes** — Verify fencing rejects mutation.
7. **Phase 7: Docs + evidence** — Document limitations by provider, evidence.

## Dependencies

- **Blocked by:** PR #283 merge + task-256 lease release
- **Requires:** task-160 effect-consistency infrastructure (on main, NOT in PR branch)
- **Coordinates with:** MISSION 2 (publication hardening), MISSION 3 (queue ownership)

## Evidence

- Task-160 infrastructure read from `origin/main` (7 files, 1184 insertions)
- `effect_key.py`: deterministic sha256(canonical_json)[:60]
- `outbox_policy.py`: state machine with C1-C5 crash matrix
- `outbox_port.py`: port contract (OutboxPort, EffectAdapter, DeliveryError)
- `outbox_dispatcher.py`: SQLite dispatcher with UNIQUE(operation_type, effect_key) dedupe
