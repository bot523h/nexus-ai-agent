# Application Port Contracts

**Status:** Living document (supersedes the short v1 list; content unchanged, surface completed)
**Scope:** the seven hexagonal ports, their invariants, adapters, and the tests that lock them
**Verified against:** `main` @ `7573249`

Ports are framework-free contracts in `src/nexus_ai_agent/application/ports/`. **Ports never import adapters**; adapters implement ports; only a composition root (`bot/app.py`, `api/app.py`, `cli.py`) wires an adapter to a port. This is the seam that lets the runtime swap SQLite for PostgreSQL, in-process for Whisper, or local disk for R2 without touching product code.

---

## Port list

| Port | Module | Methods | Adapters in this repository |
|---|---|---|---|
| `LLMPort` | `ports/llm.py` | `complete(prompt, *, idempotency_key=None) -> str` | `llm/` router chain (litellm), `llm/local_server_provider.py`, `llm/fake_llm.py` (tests) |
| `ConversationStorePort` | `ports/conversation_store.py` | `append_message(thread_id, role, content) -> str`, `list_messages(thread_id) -> Sequence[dict]` | `features/conversation_store.py`, `adapters/conversation_store_sqlite.py` (PR#33) |
| `CheckpointLifecyclePort` | `ports/checkpoint_lifecycle.py` | `record_checkpoint`, `touch_thread`, `inspect`, `delete_thread(*, idempotency_key)`, `schema_fingerprint` | `storage/checkpoint_lifecycle_adapter.py` (SQLite), `storage/checkpoint_lifecycle_pg_store.py` (PostgreSQL) |
| `JobQueuePort` | `ports/job_queue.py` | `enqueue(...) -> str`, `get_status(job_id) -> JobStatus`, `get_result(job_id)` | `adapters/in_process_job_queue.py` |
| `ObjectStoragePort` | `ports/object_storage.py` | `put(*, key, content, idempotency_key) -> str`, `delete(*, key, idempotency_key)` | `storage/providers/r2.py`, `storage/ai_storage_manager.py` |
| `CaptionEnginePort` | `ports/caption_engine.py` | `transcribe(audio_path, *, language=None) -> TranscriptRef`, `is_available() -> bool` | `adapters/whisper_local.py` (extra `[speech]`), `creative/caption/unavailable_adapter.py` (fail-closed default) |
| `OutboxPort` (P3/P4) | `ports/outbox_port.py` | `append_intent(*)` (durable delivery intent with the owning transaction), `inspect_intent(effect_key)`; sibling contracts `EffectAdapter.deliver(*, effect_key, payload, destination)` + `DeliveryError(retryable)` | `adapters/outbox_dispatcher.py` (reference: sidecar store + claim/lease/outcome) |

## Safety invariants

- `schema_fingerprint()` is checked before any destructive operation.
- `delete_thread()` is the v1 deletion unit and **requires** an explicit idempotency key.
- Per-checkpoint deletion is `POST_V1`: absent from the port by design, because delta-chain surgery is unsafe.
- `inspect()` is read-only and never updates access timestamps.
- Unknown lineage, metadata, schema, or lock state blocks deletion (fail closed).
- Idempotent writes reuse the same idempotency key and cannot create a second effect (`put`, `enqueue`, `delete_thread`).
- `append_intent` is durable **with** the business change (the owning transaction or an explicit `EffectConsistencyError`); a committed intent is a dispatch contract, never a "best effort" fire-and-forget.
- `DeliveryError(retryable=False)` means the destination definitively refused — the effect goes terminal, no noisy retries.
- `JobQueue` is the SQLite-backed in-process implementation; **Redis/Celery are forbidden** in the modular monolith ([`MODULE_MAP.md`](MODULE_MAP.md) §3 R4).
- `CaptionEnginePort` fails closed: an unconfigured installation raises `CaptionProfileUnavailableError` (`code = "caption_profile_unavailable"`). Silent fallbacks to mock or cloud providers are prohibited.

## Enforcement

| Rule | Test |
|---|---|
| required methods present and fully typed | `tests/architecture/test_port_signatures.py::test_required_port_methods_are_present_and_typed` |
| deletion demands an idempotency key | `::test_delete_requires_explicit_idempotency_key` |
| ports never import adapters | `::test_ports_do_not_import_adapters`, `tests/architecture/test_import_boundaries.py` |
| the raw LangGraph saver stays behind the lifecycle wrapper | `tests/architecture/test_saver_boundary.py` |
| caption port stays adapter-free and fail-closed | `tests/architecture/test_caption_substrate_boundary.py`, `tests/unit/test_caption_engine_adapters.py` |
| the queue is the only background path | `tests/integration/test_in_process_job_queue.py`, `tests/unit/test_jobs_cli.py` |

## Adding a port

Follow [`MODULE_MAP.md`](MODULE_MAP.md) §5 (“Add a port”): protocol first, signature test second, adapter third, composition-root wiring last. A port without a signature test is a convention, not a contract.
