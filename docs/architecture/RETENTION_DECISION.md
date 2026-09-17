# Retention decision record

**Status:** accepted for the contract-freeze stage

**Repository anchor:** `4a06ff262a9597cb1a4628eca91eff5941fbff69`

A checkpoint is workflow state, not a transcript. Message persistence is the
source of truth for user-visible history. The product contract therefore
separates the **resumability window** from message retention:

- resumability window: 30 days (initial product value; configurable later)
- message history: retained independently of checkpoint cleanup
- expired resume: fork a new thread from message history
- v1 deletion unit: whole thread, never an intermediate checkpoint

The 90-day inactive-thread and 24-hour orphan-grace values are not enabled by
this record. They require observed usage, a lifecycle adapter, and a reviewed
cleanup release. Unknown timestamps are protected, not interpreted as old.

## Circuit-breaker policy (H1)

A cleanup run is automatically blocked when either threshold is reached:

- failed or blocked operations reach **1%** of the run's attempted operations;
- or the run reaches **500** failed/blocked operations, whichever comes first.

The circuit breaker is fail-safe: it stops further mutation, records a redacted
reason in the operation journal, and requires an explicit reviewed retry.
Every reconciler anomaly is one journal entry with a specific `error_code`.
The 1% or 500 threshold is evaluated over those anomaly entries; crossing
**either** threshold stops the entire purge path and emits a structured warning.

Journal state transitions are binding: `blocked -> pending` is allowed only
after grace-period re-evaluation; `running -> cancelled` is terminal. Only
`succeeded` and `cancelled` are terminal. The `failed -> retrying` edge
increments `attempts` and applies exponential backoff before the retry.
