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

## LangGraph SQLite checkpoint schema (2.x) — read-only reference

Empirically captured from `SqliteSaver.setup()` on
`langgraph=1.2.11` / `langgraph-checkpoint=4.2.0` /
`langgraph-checkpoint-sqlite=3.1.1` (2026-09-18).  This is the schema the
read-only adapter inspects and the `schema-v1` golden fingerprints.  The
lifecycle layer only ever **reads** these tables; all mutation stays behind
the reconciler's `--apply` gate and touches the sidecar lifecycle index,
never the tables below.

```sql
CREATE TABLE checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type TEXT,
    checkpoint BLOB,
    metadata BLOB,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

CREATE TABLE writes (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    channel TEXT NOT NULL,
    type TEXT,
    value BLOB,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);
```

Notes:

- The 2.x/3.x layout stores payloads directly in `checkpoints.checkpoint` /
  `checkpoints.metadata` and `writes.value`; there is **no**
  `checkpoint_blobs` table in this version.  The adapter's byte estimator
  nevertheless stays tolerant of the legacy 1.x layout
  (`checkpoint_blobs` / `checkpoint_writes`) by probing table existence.
- `parent_checkpoint_id` is the delta-chain edge used by
  `verify_lineage`; per-checkpoint surgery on that chain remains POST_V1.
- The Postgres path (`langgraph-checkpoint-postgres`) uses different table
  names and types (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`,
  `checkpoint_migrations`; JSONB + BYTEA); its schema is fingerprinted under
  a separate algorithm and golden (see `storage/golden/`).
