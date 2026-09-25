# Production backup & durability — evidence report (task-164, Agent A)

Baseline: `main@2351f099e2f789b80de05c6b382554b5cdcec0f3` · branch `arena/01a0d9f4-nexus-ai-agent` · 2026-09-25

Mission principle: **produce → persist → verify → restore → report → alert**, each leg proven
by an executable check, never by prose. Anything not provable from this sandbox is marked so.

## 1. Live truth (before any mutation)

| # | Run | Date (UTC) | Event | Result | Failing step | Duration of failing step |
|---|---|---|---|---|---|---|
| 1 | 36115146739 | 2026-09-25 08:50 | schedule | failure | `Run nexus maintenance backup` | **1 s** (08:56:27→08:56:28) |
| 2 | 35975483186 | 2026-09-24 08:28 | schedule | failure | same | ~1 s |
| 3 | 35838057275 | 2026-09-23 08:36 | schedule | failure | same | ~1 s |
| 4 | 35705502209 | 2026-09-22 08:33 | schedule | failure | same | ~1 s |
| 5 | 35586584833 | 2026-09-21 10:02 | schedule | success | — (housekeeping only; `backup-db` never ran) | — |
| 6 | 35580701817 | 2026-09-21 08:58 | schedule | failure | same | ~1 s |

The log archive and the secrets API are unreachable from the sandbox (`gh run view --log`
→ EOF on `results-receiver…` / blob storage; `gh secret list` → 403), exactly as the
2026-09-23 forensics recorded. A one-second failure right after `pip install` rules out a
timeout, a network upload or a hung `pg_dump`; it is consistent only with the pre-dump
"not configured" branch — but that was still inference, not evidence.

**Definitive evidence** — forensic run [36181105635](https://github.com/bot523h/nexus-ai-agent/actions/runs/36181105635)
(this branch; presence booleans mirrored as steps so they are readable from the jobs API,
values never printed):

```
Config: R2_ACCOUNT_ID present         skipped   → ABSENT
Config: R2_ACCESS_KEY_ID present      skipped   → ABSENT
Config: R2_SECRET_ACCESS_KEY present  skipped   → ABSENT
Config: R2_BUCKET present             skipped   → ABSENT
Config: NEXUS_DATABASE_URL present    skipped   → ABSENT
Config: pg_dump on PATH               success
Run nexus maintenance backup          failure
```

### Failure classification

| Class | Verdict |
|---|---|
| credential / secret issue | **YES — primary.** None of the five secrets in `docs/ops/r2-storage.md §4` is set in the repository. |
| observability gap | **YES.** The by-design red was indistinguishable from a real dump/upload failure, named nothing, and alerted nobody — five consecutive red nights. |
| code defect | **YES — latent, found by the real-PostgreSQL drill.** `_verify_postgres_dump` required the dump to *end* with `-- PostgreSQL database dump complete`; a genuine `pg_dump` ends with `--\n-- PostgreSQL database dump complete\n--\n\n`, so **every real Postgres backup would have failed verification even after the secrets are set**. The task-167 tests only used hand-written fixtures. |
| design defect | **YES.** Without `NEXUS_DATABASE_URL` the CI job would "back up" the runner's own (non-existent/empty) SQLite file — a fake success path. Also `pg_dump` 16 on `ubuntu-latest` aborts against a Postgres 17 server (Neon's current default). |
| infra / storage provider / timeout / test defect | not implicated (no request ever left the runner). |

## 2. FILE → PROBLEM → EVIDENCE → OPTIONS → CHOSEN DESIGN → WHY

**`src/nexus_ai_agent/maintenance/backup.py`**
- PROBLEM: one untyped error for "owner never set secrets" and "backup broke"; message named all four R2 vars regardless; footer check wrong for real dumps; no restore proof for Postgres; no evidence output; SQLite fallback allowed on ephemeral hosts; URL credentials could leak into error text.
- EVIDENCE: run 36181105635; local drill against embedded PostgreSQL 16.2 (`pgserver`) reproduced the footer rejection on a real dump.
- OPTIONS: (a) message-only fix; (b) typed taxonomy + preflight + restore proof + evidence; (c) switch to `pg_dump -Fc` + `pg_restore`.
- CHOSEN: (b). Plain-SQL format kept: PostgreSQL docs define the restore recipe as `psql -X --set ON_ERROR_STOP=on` into a database created from `template0`, `--single-transaction` for all-or-nothing loads; the footer check stays meaningful; `--no-owner --no-privileges` mirrors Neon's documented `--no-owner --no-acl` so scratch clusters need not know source roles. Row-count drift is *recorded*, table-set mismatch *fails* (a live source can legitimately drift between inventory and snapshot; the quiescent drill requires exact equality).
- WHY: restore semantics first — the unit of recovery is "one plain dump loadable by stock psql into an empty DB"; everything else proves that unit is real.

**`src/nexus_ai_agent/cli.py` (only the `maintenance backup` command + new `restore-drill`)**
- PROBLEM: exit 1 for everything; no preflight; no evidence file.
- CHOSEN: exit **2 = not_configured** (owner action, nothing attempted) vs **1 = failure**; `--preflight`, `--require-postgres`, `--restore-target-url`, `--evidence-json` (written on success *and* failure).

**`.github/workflows/maintenance.yml`**
- PROBLEM: no preflight, no restore target, no artifact, no alert, PG16 client, SQLite fallback reachable.
- CHOSEN: PGDG PostgreSQL 17 client (documented `apt.postgresql.org.sh`), `pgvector/pgvector:pg17` scratch service (same image family ci.yml already pulls), presence steps, typed preflight, evidence artifact (`if: always()`, 30 d), failure → one idempotent GitHub issue via `GITHUB_TOKEN` (`issues: write`, no new secret). New `restore-drill` job proves the mechanism nightly **with zero secrets**.

**`tests/unit/test_maintenance_backup.py`** (+14 tests) and **`tests/integration/test_backup_restore_postgres.py`** (new, real PostgreSQL, skip-with-reason otherwise).

**`docs/ops/r2-storage.md`**: contract, exit codes, workflow chain, Neon notes.

Sources consulted (non-trivial decisions): PostgreSQL manual §25.1 "SQL Dump / Restoring the Dump" (psql `-X`, `ON_ERROR_STOP`, `template0`, dumps load into newer servers); Neon "Backups with pg_dump" (client major must match, unpooled URL, `--no-owner --no-acl`); GitHub Docs workflow syntax (`workflow_dispatch` file-on-default-branch rule, `permissions`); actions/runner-images Ubuntu 24.04 README (PostgreSQL 16.15 preinstalled) and issue #14651 (16 on 24.04); Stack Overflow / DigitalOcean / pgadmin4#8046 on "aborting because of server version mismatch".

## 3. Test evidence (exact commands, observed)

```
$ ruff check . && ruff format --check .          → All checks passed! / 497 files already formatted
$ mypy src                                       → 1 error (pre-existing environmental chromadb import; unchanged from baseline)
$ pytest -q tests/unit/test_maintenance_backup.py                     → 20 passed
$ NEXUS_DATABASE_URL=postgresql://nexus:nexus@localhost:5433/nexus \
  pytest -q -rs tests/integration/test_backup_restore_postgres.py     → 2 passed   (embedded PostgreSQL 16.2, real pg_dump/psql)
$ pytest -q -m "not slow"                                             → 2326 passed, 32 skipped
$ nexus maintenance restore-drill --source-url … --target-url … --evidence-json /tmp/drill.json
  → ✅ restore drill ok: 31 table(s), row counts identical   (sha256 897b1e39…, 53 736 bytes)
```

Before/after on the real-dump footer: before → `BackupVerificationError: pg_dump artifact is truncated…` on a healthy 62 475-byte dump; after → `restore_proven=True`.

## 4. Backup / restore evidence in CI (run [36182675224](https://github.com/bot523h/nexus-ai-agent/actions/runs/36182675224), this branch)

| Job | Result | What it proved |
|---|---|---|
| `restore-drill` | **success** | PG17 client install → `nexus migrate` → dump → `CREATE DATABASE … TEMPLATE template0` → `psql --single-transaction` restore → inventory identical → integration test green → artifact `restore-drill-evidence-36182675224` (703 B) |
| `backup-db` | failure, **classification `not_configured`** at step "Preflight (named, typed)" (exit 2); the backup step never ran | artifact `backup-evidence-36182675224` (497 B); **alert issue [#85](https://github.com/bot523h/nexus-ai-agent/issues/85) opened automatically** naming each missing secret |

So the chain is proven end to end for everything that does not require the owner's
credentials: produce ✓ · verify ✓ · restore ✓ · report ✓ · alert ✓ · persist (R2) — **blocked
on secrets**, but the code path is exercised against an in-memory provider with byte-identical
round-trip in `test_create_backup_reports_restore_proven_end_to_end`.

## 5. Remaining gaps (honest)

1. **Owner action:** set `NEXUS_DATABASE_URL` (unpooled Neon URL), `R2_ACCOUNT_ID`,
   `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` (repo secrets), then run the
   workflow manually; expected: `backup-db` green, `backup-evidence-<id>` shows
   `"status": "success", "restore_proven": true`, issue #85 stops receiving comments.
2. If the Neon database has extensions unavailable in the scratch image (e.g. `neon`), the
   restore step will fail with `restore_failed` naming the extension — that is the correct,
   visible outcome; the fix would be `--exclude-extension` (pg_dump ≥ 17) after the first real run.
3. Alert de-duplication is by issue title; closing the issue re-arms it.
4. Artifact download is blocked from this sandbox (blob storage); sizes and expiry were read
   from the API. Neither logs nor artifacts are needed to read the outcome: step names + issue body carry it.
