# Application port contracts

Ports are framework-free contracts. Adapters implement them; ports never import
adapters. The LangGraph runtime saver remains the runtime owner. The lifecycle
port owns only lifecycle metadata and safe, whole-thread deletion.

## Safety invariants

- `schema_fingerprint()` is checked before any destructive operation.
- `delete_thread()` is the v1 deletion unit and requires an idempotency key.
- Per-checkpoint deletion is `POST_V1` because delta-chain surgery is unsafe.
- `inspect()` is read-only and never updates access timestamps.
- Unknown lineage, metadata, schema, or lock state blocks deletion.
- JobQueue starts with an in-process implementation; Redis/Celery are not part of Stage 0.
  The adapter is `adapters/in_process_job_queue.py` (`InProcessJobQueue`): jobs run as
  asyncio tasks inside the bot process; state is durable in the `<db_path>.jobs` SQLite
  sidecar (never in the Alembic-managed schema); `UNIQUE (job_type, idempotency_key)`
  makes a resubmission return the existing job with no second effect; every persisted
  status change is an `ALLOWED_TRANSITIONS` edge (`domain/policies/retention.py`);
  failures are stored as `failed` + error; recovery after a restart is the explicit
  `resume_pending()` — nothing is re-dispatched implicitly at boot. Guarded by
  `tests/architecture/test_no_distributed_queue.py`.

## Port list

- `LLMPort`: provider-neutral completion.
- `ConversationStorePort`: durable message history.
- `CheckpointLifecyclePort`: lifecycle metadata, inspection, fingerprint and thread deletion.
- `ObjectStoragePort`: idempotent attachment put/delete.
- `JobQueuePort`: idempotent background work submission and status.
