# Neon Lifecycle Operations Runbook

Operating procedures for the PostgreSQL checkpoint backend (Neon) with the
in-database lifecycle index (PR3, option A — the index lives in the same
database; see "Lifecycle index maintenance" for why option B's sidecar was
retired on this path).

No hidden migration. No hidden mutation. No implicit repair.

## What exists where

| Data | Store | Why |
|---|---|---|
| Checkpoints, blobs, writes | PostgreSQL (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`) | LangGraph official `PostgresCheckpointer`; the only durable source of conversation state |
| Alembic head | PostgreSQL (`alembic_version`) | Schema version for the app schema (the 4 `checkpoint_*` tables are LangGraph-owned, not Alembic-owned) |
| Lifecycle metadata (`created_at`, `last_accessed_at`, `active_until`) | **PG path:** PostgreSQL, table `nexus_checkpoint_lifecycle` (Alembic revision `f4a9c2e71b08`). **SQLite path:** local sidecar file next to `settings.checkpoint_path`, owned by the store | Durable on serverless (Neon) — the reason option A replaced the interim sidecar (option B). Rows are disposable metadata: losing them is non-fatal (reconcile backfills) |
| Schema goldens | `src/nexus_ai_agent/storage/golden/*.json` | Drift detection; `postgres.langgraph.json` covers the LangGraph schema scope only |

Kill switch: `NEXUS_LIFECYCLE_KILL_SWITCH=false` (or the settings field)
disables all lifecycle recording; the checkpointer stays a bare
`PostgresCheckpointer`. Recording is never active while the kill switch is
off, and no lifecycle write of any kind is ever performed.

## Daily

1. **Inspect** (read-only, safe anytime):

   ```
   nexus checkpoints inspect --json
   ```

   `would_free_bytes_estimate` is informational only — no purge is ever
   performed by this command.

2. **Reconcile dry-run** (read-only; recommended before any destructive
   operation, including Neon's own branch delete/detach):

   ```
   nexus checkpoints reconcile --json
   ```

   Read the report:
   - `langgraph_schema: "match"` / `"drift"` vs `postgres.langgraph.json`
     (`error` = connection failure — NOT drift; check connectivity first)
   - `core_schema: "match"` / `"drift"` vs `alembic_version` head
   - `health_gate`: `anomaly_rate` and counts. `--apply` is blocked when
     `anomaly_rate > 0.01` or anomalies > 500 — that is a guard, not a bug.
   - `--apply` (both backends) may backfill missing lifecycle rows and
     purge orphaned *lifecycle rows* under the full guard stack (kill
     switch, golden match, health gate, 24h block per orphan, anomaly
     caps). It never deletes LangGraph rows on any backend.

## Drift response (any `drift` in the report)

1. Do not "fix" the schema manually. No implicit repair.
2. `git log -p src/nexus_ai_agent/storage/golden/` — was a golden update
   committed without the code change (or vice versa)?
3. If a legitimate schema change is pending: the golden update is a
   **human-only** operation (see below), then re-run reconcile.
4. If the PG instance is not the expected one (wrong URL, restored branch
   from an older commit): fix the URL / re-apply the expected alembic head
   via `nexus migrate` before touching anything.

## Golden update (human-in-the-loop only)

```
nexus checkpoints golden update --backend postgres --yes
```

- Never automated, never in CI, never from a script. A human reads the
  diff (`git diff src/nexus_ai_agent/storage/golden/`) and commits it
  together with the code change that caused it.
- `--output` writes elsewhere (tests only); canonical path is default.
- Fingerprint change on the *same* alembic head means the LangGraph
  library version changed its DDL — review the `old:` / `new:` warning
  and the upstream changelog before committing.

## Lifecycle index maintenance

**PG path — the table is migration-owned (option A).**

- Never hand-DDL the table. If it is missing (database predates revision
  `f4a9c2e71b08`), the fix is `nexus migrate`; reconcile contains the
  resulting read failure as a health-gate error, never as drift.
- Rows are disposable metadata. Losing them (branch restore, manual
  truncate) is non-fatal: the next `reconcile --apply` backfills missing
  rows with a protected estimated age, and the graph is unaffected.
- Why the sidecar was retired here: the interim design (option B) kept
  the index in a local SQLite file next to the checkpoint path. On
  serverless deployments (Neon) the local file is ephemeral — it is lost
  on every compute restart, so lifecycle history would vanish
  repeatedly and silently. Option A (owner decision) puts the index in
  the database, where it is as durable as the checkpoints themselves.

**SQLite path — the sidecar stays.**

- On the SQLite backend the "database" is already a local file, so the
  sidecar is the store's own local index file (created by the store via
  `CREATE TABLE IF NOT EXISTS`; no migration machinery for a disposable
  index). It is not ephemeral in the Neon sense — it lives with the
  checkpoint file — and it must not be committed, backed up, or shipped
  (per-host metadata).
- Deleting it is safe: the next reconcile reports `missing_lifecycle:
  true` until rows are repopulated; the graph is unaffected.

## Neon-specific notes

- Branches: reconcile dry-run before **delete** or **detach** of a branch;
  a branch delete is the only destructive Neon op this repo has a guard
  against, and it works with zero changes to the graph path.
- Compute pause: the PG path is read-heavy only when the CLI runs; a
  paused compute simply means "connection error" (health gate, not drift).
- URL injection: the URL always comes from the owner's environment
  (`NEXUS_DATABASE_URL`); it is never committed and never logged with
  credentials.

## Failure table (observed behavior, by design)

| Situation | Behavior |
|---|---|
| PG connection error during inspect/reconcile | `health_gate: "error"`, exit 2, no drift verdict |
| `anomaly_rate > 1%` | `--apply` blocked (both backends; lifecycle rows only) |
| Kill switch off | bare checkpointer, zero lifecycle writes |
| Lifecycle store write failure (either backend) | logged, never raised into the graph |
| PG lifecycle table missing (migration not run) | reconcile health-gate error — never a crash, never drift; fix with `nexus migrate` |
| Golden fingerprint changed, same head | warning with `old:` / `new:`; human updates golden via CLI |
| `missing_lifecycle: true` | informational; no data is lost |
