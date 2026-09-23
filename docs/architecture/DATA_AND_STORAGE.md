# Data, Storage & Lifecycle

**Status:** Living document
**Scope:** every store the runtime touches, what owns it, how it is migrated, and how data leaves
**Verified against:** `main` @ `7573249`

The system uses **one logical data model with two physical backends** (SQLite for local, PostgreSQL/Neon for scale-to-zero) plus three purpose-built stores. Nothing is shared by accident: each store has exactly one owner module.

---

## 1. Store map

```mermaid
flowchart LR
    subgraph appdb["Application DB (SQLModel + Alembic)"]
        tables["31 tables<br/>users · messages · memory · gamification · knowledge · approvals"]
    end
    subgraph sidecar["Queue sidecar (SQLite, own DDL)"]
        q["nexus_job_queue<br/>id · job_type · idempotency_key UNIQUE · payload · status"]
    end
    subgraph ckpt["Checkpoints (LangGraph runtime)"]
        cp["checkpoints · writes (+ blobs)"]
        life["nexus_checkpoint_lifecycle (PG) / sidecar table (SQLite)"]
    end
    subgraph blobs["Blob tier"]
        r2["Cloudflare R2 (s3-compatible)<br/>DB backups · model blobs"]
        cache["local model/image cache"]
    end
    graph["Orchestration graph"] --> ckpt
    queue["Bot + workers"] --> sidecar
    features["Feature engines"] --> appdb
    reconciler["Reconciler + CLI"] --> ckpt
    maintenance["nexus maintenance backup"] --> blobs
```

| Store | Owner module | Backend | Lifetime | Notes |
|---|---|---|---|---|
| Application DB | `storage/models.py` (+ 2 tables in `features/referral.py`) | SQLite (WAL) or PostgreSQL | durable | 31 tables; the product's source of truth for user-visible history |
| Job queue | `adapters/in_process_job_queue.py` | SQLite sidecar `<db>.jobs.sqlite3` | durable, disposable | owns its table and **no** Alembic migration; payloads must pass adapter validation |
| Effect outbox | `adapters/outbox_dispatcher.py` (+ pure policy `domain/policies/outbox_policy.py`) | SQLite sidecar (own DDL) | durable | `nexus_effect_outbox`: delivery intent + lease + outcome; `UNIQUE(operation_type, effect_key)` is the dedupe backstop; **no** Alembic revision (schema authority stays put) |
| Checkpoints | `storage/langgraph_checkpoint.py` (`get_checkpointer`) | SQLite or PostgreSQL | disposable | LangGraph runtime state only; deletion never touches messages |
| Lifecycle index | `storage/checkpoint_lifecycle*.py` | PG table `nexus_checkpoint_lifecycle` / SQLite sidecar table | durable | metadata about checkpoints: age, lineage, lock state, access |
| Vector store | `features/rag.py` (`vector_path`, `chroma_db_path`) | SQLite/Chroma on disk | rebuildable | derived from documents; safe to drop and re-ingest |
| Blob tier | `storage/providers/r2.py`, `storage/ai_storage_manager.py` | Cloudflare R2 | durable | idempotent `put`/`delete` behind `ObjectStoragePort` |
| Creative workspace | `settings.creative_temp_dir` | local temp dir | ephemeral | `slideshow_*` workspaces; stale ones pruned after 24 h |

## 2. Schema management

- **Alembic is the only schema authority.** The stopgap `create_all` path was retired (D7); startup either verifies head or runs the chain under a migration lock.
- Chain (3 revisions, current head `7c2f9d41e8a3`):

```text
47903d282ede  initial schema
   └── f4a9c2e71b08  nexus_checkpoint_lifecycle (PostgreSQL only)
          └── 7c2f9d41e8a3  ai-memory consent columns on usermemory  ← HEAD
```

- **URL normalisation** accepts `postgresql://`, `postgres://`, `postgresql+asyncpg://` and rejects everything else with a clear error (`storage/db.py::normalize_database_url`).
- **Legacy adoption instead of replay:** an existing, unstamped SQLite database or a pre-Alembic PostgreSQL database is *inspected, decided, then stamped* (`storage/adopt_pg.py`, `nexus adopt-pg`) — never re-created.
- **Drift is a failure, not a repair:** `storage/checkpoint_fingerprint.py` compares a live fingerprint to a golden manifest; updating goldens is human-only (`nexus golden update`, enforced by tests).
- **Concurrency:** migrations take a lock (`storage/migrations.py::migration_lock`); concurrent first-run migrations are covered by `tests/integration/test_migrate_race_condition.py` and the CI `migrate-postgres` job (real `pgvector/pgvector:pg16`), which also asserts the stamp equals the head revision and that a second `nexus migrate` is a no-op.

## 3. Entity ownership (naming discipline)

SQLModel classes are the schema; product names are the contract.

| Aggregate | Table(s) | Owner |
|---|---|---|
| User / chat history | `user`, `chat`, `message` | `features/ai_chat.py`, `storage/models.py` |
| Memory | `usermemory` (+ `ai_memory_consent*`), `useractiveagent`, `documentchunk`, `knowledgecache` | `features/ai_memory.py`, `knowledge/` |
| Community | `referral`, `referralcode`, `userreputation`, `userxp`, `quizzescore`, `anonsession` | `features/{referral,gamification,anonymous_chat}.py` |
| Governance | `adminlog`, `forcejoinconfig`, `moderationconfig`, `pendingapproval`, `personalityconfig`, `engagementconfig` | `features/{owner_control,force_join,moderation,personality,engagement}.py` |
| Growth | `viralpost`, `adcampaign`, `analyticsevent`, `channelschedule`, `welcomemessage` | `features/{viral_engine,ads,analytics,channel_manager}.py` |
| Ops | `task`, `toolrun`, `reminder`, `cloudfile`, `userlanguage` | `storage/models.py`, `worker.py`, `tools/` |

## 4. Checkpoint lifecycle and deletion

| Concept | Rule |
|---|---|
| Source of truth for history | **messages** — never deleted by checkpoint cleanup |
| Workflow state | checkpoints — disposable, used only to resume |
| Deletion unit (v1) | the **whole thread**, through the native checkpointer operation, with an idempotency key |
| Per-checkpoint deletion | `POST_V1` — delta-chain surgery is unsafe; blocked by a marker constant + liveness test |
| Unknown anything | unknown lineage, schema, metadata, or lock state ⇒ **no delete** |
| Read paths | `inspect` is read-only and never updates access timestamps |
| Retention | 30-day resumability window; expired resume forks a new thread from message history |
| Circuit breaker | a cleanup run stops at **≥1 %** failed/blocked operations **or ≥500** such operations, records a redacted reason, and requires a reviewed retry |

Full policy text: [`DATA_LIFECYCLE.md`](DATA_LIFECYCLE.md), [`RETENTION_DECISION.md`](RETENTION_DECISION.md); implementation: `storage/checkpoint_reconciler.py`, `domain/policies/{retention,reconciler_policy,lifecycle_health}.py`.

## 5. Portability and backup

- **Local → Neon**: point `NEXUS_DATABASE_URL` at Neon and run `nexus migrate`; the same chain, the same head, the same fingerprint checks. `docs/ops/NEON_LIFECYCLE_RUNBOOK.md` covers keep-alive and lifecycle specifics.
- **Backups**: `nexus maintenance backup` writes to the R2 tier behind `ObjectStoragePort` (idempotency keys make repeated runs safe). Restore procedure and rotation: [`../ops/DEPLOY_RUNBOOK.md`](../ops/DEPLOY_RUNBOOK.md).
- **Ephemeral-disk rule**: in webhook/scale-to-zero mode, anything that must survive a redeploy lives in Postgres or R2 — never on local disk. The queue survives restarts because it is a SQLite sidecar on durable storage, and `nexus jobs resume` re-schedules *pending-only* work.

## 5b. Effect consistency (P3/P4) — semantic ladder per effect

The outbox delivers **at-least-once**; the effect key collapses repeated attempts
into **at-most-one logical effect**. The two are different claims and are told
apart everywhere. Enforced by `tests/unit/test_effect_dedup.py` and
`tests/integration/test_outbox_crash_matrix.py`.

| Effect class | Delivery semantics | Logical-effect semantics | Why |
|---|---|---|---|
| `telegram.send` | at-least-once | at-most-once intent (duplicate attempt visible, not silently "solved") | Telegram has no server-side idempotency token; a connect-lost-after-send then retry *is* a second message. The outbox cannot stop the network — it must not claim to. |
| `telegram.edit` | at-least-once | effectively-once (idempotent by value: same text, same message) | editing the same message to the same text is observationally one effect, but delivery is still at-least-once. |
| `r2.upload` (PUT) | at-least-once | effectively-once | the object key names the destination; a second PUT of identical content is a no-op overwrite. |
| `http.callback` | at-least-once | at-least-once unless the receiver honours `Idempotency-Key` (adapter adds it per attempt) | external receiver policy is outside this process; the key makes receiver-side dedupe *possible*. |
| DB insert | exactly-once *by the UNIQUE(operation_type, effect_key) constraint* | effectively-once | the database serialises writes; the constraint is the atomic dedupe, not an in-memory set. |

- **Effect key** (`domain/policies/effect_key.py`) is a SHA-256 over the
  canonical JSON of `(operation_type, logical_entity, logical_revision,
  destination, logical_slot)` — deterministic, retry-stable, distinct per
  intent, and *independent of the attempt id* (P4). Mutation A (random key per
  retry) turns the suite red.
- **Outbox state machine** (`domain/policies/outbox_policy.py`):
  `PENDING → CLAIMED → SUCCEEDED | FAILED_RETRYABLE | FAILED_PERMANENT`, with
  `FAILED_RETRYABLE → PENDING` on bounded backoff. No dead-letter state without
  the explicit evidence-gated toggle (`would_dead_letter`) — permanent failures
  stay in the table, visible, awaiting the reconciliation contract or an
  operator command.
- **Reconciliation contract** (`classify_row` / `recovered_rows`): stale
  `CLAIMED` past the lease, old `PENDING`, due `FAILED_RETRYABLE`, and terminal
  rows carrying a ghost lease owner (C4 residue) are each classified
  deterministically — recovered, waited on, or surfaced as `unknown`, never
  guessed into `SUCCEEDED`.

## 6. What never enters a store

- No credentials, tokens, or raw prompts in the operation journal or lifecycle metadata (redacted error codes only).
- No effect-key token, `telegram_id`, or payload field ever becomes an observability label; effect events carry a truncated digest only (`adapters/observability_backend.py::effect_key_prefix`).
- No PII in dashboard responses (`api/dashboard.py`, `tests/unit/test_dashboard_privacy.py`).
- No user prompt egress to a training-capable free endpoint unless the strict-privacy flag is off and the user consented (`features/ai_memory.py` consent gate + `LLM_PROVIDERS.md`).
- No manifest, tone template, or pack resource may contain an executable key (§[`CREATIVE_STUDIO.md`](CREATIVE_STUDIO.md) §3).
