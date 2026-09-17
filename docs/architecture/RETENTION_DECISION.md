# Retention decision record

**Status:** accepted for the contract-freeze stage

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
