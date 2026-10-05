---
name: nexus-job-lifecycle
description: This skill should be used when adding or changing durable background jobs, worker handlers, artifact verification, or job failure semantics in nexus-ai-agent, when asked to "add a job type", "enqueue a job", "verify an artifact", "why did a job stay pending", "add a verifier", or when the verification-registry ratchet fails. It encodes the canonical Command→Job→Execution→Verification→Result chain, the fencing-token state machine, and the rule that execution success is not job success.
---

# NEXUS Job Lifecycle

## Purpose

Long work (media renders, LLM batches, PDF extraction) never runs inside a Telegram handler. It
becomes a **durable job** with a fencing token, an independent verification phase, and an atomic
publication step. The contract is `docs/architecture/JOB_LIFECYCLE.md`, enforced by
`tests/unit/test_job_lifecycle.py`, `tests/unit/test_failure_semantics.py`,
`tests/integration/test_job_lifecycle_queue.py`, and
`tests/architecture/test_verification_registry_ratchet.py`.

## The one rule that prevents the worst bugs

**Execution success ≠ Job success.** A job reaches `completed` only after its artifact has been
re-measured independently by a verifier and published atomically. The verifier reads the filesystem;
it never trusts the handler's word. A crashing verifier fails the job **closed**.

## The canonical chain

```
Command (surface/API/CLI) → Job (durable row, idempotency-keyed)
  → Runtime Execution (packs registry → CommandBus → lane)
  → Artifact Verification (independent re-measurement)
  → Publication (atomic rename + re-probe)
  → Result (verified facts / typed failure)
```

## State machine (six states)

`pending → processing → verifying → completed`, with failures always ending in
`failed_retryable` or `failed_terminal` (never `completed`). Aliases: `RUNNING ≡ PROCESSING`,
`SUCCEEDED ≡ COMPLETED`; legacy rows written as `"failed"` read back as `failed_terminal`
(`jobs.lifecycle.parse_job_status`).

Fencing, in order:

1. `pending → processing` is a CAS that mints the fencing token (`attempt += 1`).
2. Every worker transition is a **fenced** CAS (`attempt = claim`); a stale zombie writes zero rows.
3. The side-effect fencing point (`_owns_execution`) is checked immediately before the rename.
4. The **only** licence to notify success is `rowcount == 1` of the completion UPDATE.
5. Takeover is `resume_pending` (startup or stale `started_at`), never a reservation.

## Side-effect boundary (exactly one)

No execution side-effect before **all** of: authorization → capability (operation in the registry)
→ references (media contained + readable) → idempotency (durable UNIQUE key) → revision/precondition
→ job reservation. Then the first lawful side effect is a write to a **staging** path; the final name
appears only via atomic rename (media `.part`, documents temp → `os.replace`). A half-written
destination is never visible under a final name.

## Adding a job type

1. **Handler** — register in `worker.default_job_handlers`. It returns a dict (success) or raises a
   typed failure. It stages bytes; it does not publish the final name.
2. **Verifier** — register a verifier for the new job type. `test_verification_registry_ratchet.py`
   **fails** if a handler is ever registered without one. The verifier re-measures: exists →
   `size > 0` → expected path per operation → workspace containment → sha recompute → probe (media)
   or structural decode (SubRip / OTIO JSON).
3. **Enqueue** — through `JobQueuePort.enqueue(job_type, idempotency_key, payload)`; never await the
   work in a handler.
4. **Result** — the durable row is the sole authority for job state and outcome; a passport is a
   queue-row checkpoint, not a second source of truth.

## Failure semantics

`jobs/failure_semantics.FailureClass` splits failures: `FAILED_RETRYABLE` (the world can change;
retry-eligible) vs `FAILED_TERMINAL` (the identical request must fail again). There is **no retry
scheduler** in this repository — both are terminal as implemented; the reserved
`failed_retryable → pending` edge is deliberately outside the transition matrix (fail-closed).

## Additional Resources

- **`references/job-contract.md`** — the full state diagram, the verifier registry, the three
  artifact identities (logical/spec/physical), the publication order, the negative-test matrix
  M1–M10, and the notification/trace contracts.

## Common mistakes

- Notifying success before the completion CAS returns `rowcount == 1` — a stale execution can announce.
- Publishing the final artifact name before verification — a refusal then leaves a visible artifact.
- Registering a handler without a verifier — the ratchet test goes red.
- Awaiting long work inside a Telegram handler instead of enqueuing a job.
- Treating `failed_retryable` as auto-retried — it is terminal here.
