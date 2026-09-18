# Nexus data lifecycle contract

## Vocabulary and source of truth

- **Conversation**: product-level aggregate.
- **Thread**: one resumable workflow branch; a conversation can fork into multiple threads.
- **Message**: durable, user-visible history. It is the source of truth for conversation history.
- **Checkpoint**: disposable LangGraph workflow state used only to resume execution.
- **Tool state**: state owned by the tool integration that created it.
- **Attachment**: object-storage content plus durable metadata.

Checkpoint deletion must never delete messages. After the resumability window,
visible history remains available and a continuation starts a new thread.

## v1 safety contract

- Destructive unit: whole thread, through the native checkpointer operation.
- Mid-chain checkpoint deletion is `POST_V1`; delta-channel lineage makes it unsafe.
- Unknown schema, unknown lineage, missing metadata, or uncertain locking means **no delete**.
- `inspect` is read-only and never updates access timestamps.
- Reconciliation backfills an unknown checkpoint into protected metadata first; it never treats unknown age as old.

## Operation journal

All externally observable operations use `nexus_operation_journal` with a unique
`(op_type, idempotency_key)`. The record contains `aggregate_type`,
`aggregate_id`, `status`, `attempts`, `blocked_since`, timestamps,
`payload_hash`, and a redacted error code/message. Payloads have a size limit
and never contain credentials, tokens, or raw conversation content.

Allowed state transitions are:

```text
pending -> running -> succeeded
pending -> running -> failed
failed -> retrying -> running
running -> blocked
```

Retries reuse the same idempotency key and cannot create a second effect.
