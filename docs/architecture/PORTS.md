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
- JobQueue is the SQLite-backed in-process implementation; Redis/Celery are forbidden in the Modular Monolith.

## Port list

- `LLMPort`: provider-neutral completion.
- `ConversationStorePort`: durable message history.
- `CheckpointLifecyclePort`: lifecycle metadata, inspection, fingerprint and thread deletion.
- `ObjectStoragePort`: idempotent attachment put/delete.
- `JobQueuePort`: idempotent background work submission and status.
