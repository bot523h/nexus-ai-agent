# NEXUS-OMNI — Runtime, Data & Recovery Audit (2026-11-03)

> **Status:** dated record. Mission `NEXUS-OMNI-FORENSIC-RECONSTRUCTION-2026-11-03`,
> branch `arena/omni-audit-reports`. Base `main` @ `5ce9f7d3`. Covers the durable
> job queue, SQLite/Postgres, storage providers, the asset library, backup and
> recovery. No production database was touched.

## 1. Job queue & attempt lifecycle

The durable queue lives in `adapters/in_process_job_queue.py` (3,303 LOC — the
largest file) with the render path in `creative/render_jobs.py`, the lifecycle
state machine in `jobs/lifecycle.py`, and the passport/evidence layer in
`jobs/creative_passport.py`.

**Ownership & fencing (verified by the queue-ownership campaign):**

- `jobs/lifecycle.py` documents the ownership model explicitly: state
  transitions are `owner="queue (fenced: attempt = claim)"` — the attempt number
  **is** the claim, so a stale attempt cannot mutate a row a newer attempt owns.
- Failure conversions are `fail-closed`.
- `jobs/creative_passport.py` binds every artifact to
  `attempt_id(job_id, attempt_number)`; `verify_attempt` (line ~256) rejects any
  passport whose `attempt_id` does not equal the expected one — a forged or
  stale attempt cannot produce accepted evidence.

**The mutation campaign** (`scripts/queue_ownership_mutations.py`, 10 mutants
M1–M10) targets `adapters/in_process_job_queue.py` and
`creative/render_jobs.py` and asserts a `worktree_clean` provenance check. This
is the campaign that was failing in CI before the disk fix (see the CI report).

**Targeted tests** (56, run locally against the minimal install, all green in
16.9 s):

- `tests/integration/test_queue_ownership_lifecycle.py`
- `tests/integration/test_queue_ownership_processes.py`
- `tests/integration/test_artifact_publication_safety.py`
- `tests/integration/test_queue_ownership_publication_barrier.py`

## 2. Execution-core invariants

`docs/architecture/EXECUTION_CORE.md` restates the I1–I10 invariant table, and
`tests/architecture/test_execution_contract_boundary.py` holds the canonical
table; `tests/unit/test_docs_integrity.py` gates the two row-for-row. This is a
strong pattern: the architecture document cannot drift from the contract test.

Execution properties observed in code: idempotency via the `(operation_type,
effect_key)` unique constraint in the effect outbox; fencing via attempt =
claim; retry via typed retryable classification; terminal-state discipline in
`jobs/lifecycle.py`.

## 3. Effect outbox

`adapters/effect/outbox_dispatcher.py` implements the transactional outbox
pattern: an insert collides on the unique `(operation_type, effect_key)` index
and the dispatcher reads the existing row instead of double-applying — *"the
re-insert was a no-op BY THE UNIQUE CONSTRAINT"*. `pending_rows()` seeds
reconciliation; `all_rows()` is the audit view. The B608 bandit hits here are
constant column lists, not injection (security report §3).

## 4. Database & storage

- **Engines:** SQLModel 0.0.42 / SQLAlchemy 2.0.54, SQLite (aiosqlite) and
  Postgres (asyncpg, psycopg[binary,pool]).
- **Migrations:** 7 Alembic revisions (`migrations/versions/`): initial schema,
  ai-memory consent, telegram-ids bigint, principal membership, knowledge
  retrieval, nagar asset library, checkpoint lifecycle. Two historical reverts
  removed a pgvector migration and kept the chain single-headed
  (`beb2a678 test: assert the reverted migration chain has one head`).
- **WAL init race** (`storage/db.py`) was a fixed defect (task-190, PR #132 →
  #174) with a dedicated test `test_storage_wal_init_race.py`.
- **Providers:** `storage/providers/` — `r2.py`, `github_releases.py`,
  `huggingface.py`, `mega.py`, `rclone.py`, `local_cache.py`. R2 is a separate
  "technical blob" tier (backups + heavy RAG docs), not part of the user-file
  round-robin.

## 5. Asset library

`creative/assets/` implements content-addressed storage with SHA-256 identity,
an explicit authorization seam (`authz.py`, security report §2), and
filesystem-security tests (`test_asset_library_filesystem_security.py`).
Lineage and provenance are recorded in `provenance/` and the passport layer.

**Cleanup safety:** the retention contract (`RETENTION_DECISION.md`) requires
that a cleanup run be circuit-broken at 1% / 500 anomalies, that unknown
timestamps are *protected* rather than treated as old, and that the destructive
unit is the whole thread. This is the correct fail-safe posture for asset
cleanup.

## 6. Backup & restore

`maintenance/` implements the backup/restore/housekeeping trio:

- `backup.py` — `create_backup()` writes `backups/db/<UTC-stamp>/…` to R2 and
  writes machine-verifiable evidence (key, sha256, size, timestamp) to the job
  log and step summary.
- `restore.py` — lists `backups/db/`, restores, and has a **restore drill**
  (`restore-drill` job, weekly) that proves a real restore in an isolated path.
- `housekeeping.py` — prunes backups older than the retention window and cleans
  the temp dir; a dry run cannot be misreported as a successful deletion
  (`*_planned` vs `*_removed`/`*_deleted` are separate keys).

**The maintenance workflow's preflight is fail-closed by design:** `backup-db`
refuses to run when the R2/Postgres secrets are missing or when
`NEXUS_DATABASE_URL` is not a Postgres URL — *"Refusing a CI-local SQLite
fallback for the production backup."* This is exactly right: an unmonitored
missing backup is worse than a red job.

## 7. Findings

### RT-1 (P2) — housekeeping red every schedule, retention not actually running

The scheduled `maintenance` run `41446266076` (`main` @ `d52a2434`) shows
`housekeeping` **failure**, `backup-db` **skipped**, `restore-drill`
**skipped**. The housekeeping job log (job `125280000337`) ends with:

```
StorageError: R2 list failed (prefix='backups/db/'):
ParamValidationError: Parameter validation failed: Invalid bucket name
```

Root cause: `R2Provider.is_configured()` accepts any non-empty `R2_BUCKET`, so a
malformed bucket passes the guard and boto3 fails deep inside with an opaque
error. Consequence: **backup retention was not actually running** — the
`housekeeping` job is the only thing that prunes old backups, and it was red.

**Fix:** PR #305 validates the bucket against the documented R2 rule and refuses
at the boundary with an actionable `ProviderUnavailable`. The malformed
`R2_BUCKET` repository secret itself must be corrected by the owner; the code
fix makes the failure explicit instead of opaque.

### RT-2 (P2, documented) — restore is proven only for the R2 path

The `restore-drill` job proves the R2 backup/restore round-trip weekly, but it
is `skipped` whenever `backup-db` fails or secrets are absent. There is no
independent, always-on restore proof. Disposition: owner decision; the drill
exists and is the right shape.

### RT-3 (P3, documented) — startup guard for the one-queue-owning-process assumption

The board's forward work (`next_work`) already records *"Startup guard for the
one-queue-owning-process assumption"*. The in-process queue assumes a single
owning process; there is no startup assertion that refuses a second owner. This
is an honest, known gap with a planned guard.

## 8. Disposition

| Item | Severity | Status |
|---|---|---|
| RT-1 housekeeping red / retention not running | P2 | **FIXED (code)** PR #305; owner corrects the secret |
| RT-2 restore proven only for R2 path | P2 | documented; owner decision |
| RT-3 single-owner startup guard | P3 | in `next_work`, planned |
| RT-4 bounded retention for `_quarantine` trees | P3 | in `next_work`, planned |

The queue's ownership/fencing/passport design is strong and test-backed. The
recovery story is well-shaped (fail-closed preflight, weekly restore drill,
verified backup evidence); its weakness is operational (a red scheduled job that
nobody actioned) rather than architectural — and that is now visible.
