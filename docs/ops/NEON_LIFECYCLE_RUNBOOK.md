# Neon Lifecycle Operations Runbook

Operating procedures for the PostgreSQL checkpoint backend (Neon) with the
SQLite sidecar lifecycle store (PR3, option B).

No hidden migration. No hidden mutation. No implicit repair.

## What exists where

| Data | Store | Why |
|---|---|---|
| Checkpoints, blobs, writes | PostgreSQL (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`) | LangGraph official `PostgresCheckpointer`; the only durable source of conversation state |
| Alembic head | PostgreSQL (`alembic_version`) | Schema version for the app schema (the 4 `checkpoint_*` tables are LangGraph-owned, not Alembic-owned) |
| Lifecycle metadata (`last_accessed_at`) | Local SQLite sidecar next to `settings.checkpoint_path` | PR3 option B: disposable metadata, zero migrations, zero PG tables. Losing it is non-fatal (rebuilds on next access) |
| Schema goldens | `src/nexus_ai_agent/storage/golden/*.json` | Drift detection; `postgres.langgraph.json` covers the LangGraph schema scope only |

Kill switch: `NEXUS_LIFECYCLE_KILL_SWITCH=false` (or the settings field)
disables all lifecycle recording; the checkpointer stays a bare
`PostgresCheckpointer`. Recording is never active while the kill switch is
off, and no lifecycle write (including the sidecar) is ever performed.

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
   - `purge_eligible` stays `false` on the PG backend (SQLite-only by
     design). The lifecycle on PG is observability, not reclamation.

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

## Lifecycle sidecar maintenance

- The sidecar is a local file. Deleting it is safe (worst case: the next
  `reconcile` reports `missing_lifecycle: true` until the next user
  access repopulates rows; the graph is unaffected).
- It must not be committed, backed up, or shipped. It is per-host
  metadata (the owner's laptop, the worker's scratch disk).

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
| `anomaly_rate > 1%` | `--apply` blocked (SQLite purge path) |
| Kill switch off | bare checkpointer, zero lifecycle writes |
| Sidecar write failure | logged, never raised into the graph |
| Golden fingerprint changed, same head | warning with `old:` / `new:`; human updates golden via CLI |
| `missing_lifecycle: true` | informational; no data is lost |
