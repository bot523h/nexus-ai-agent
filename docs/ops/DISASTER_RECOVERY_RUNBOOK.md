# Disaster Recovery Runbook — backup must mean recovery

**Scope:** the nightly DB backup chain and its restore path for
`bot` (SQLite on Koyeb disk or PostgreSQL on Neon → snapshots in Cloudflare R2).

**Golden rules (pinned by tests, enforced in code):**

1. An upload is a backup **only after** a byte-identical read-back.
2. A backup is recovery **only after** a restore into an isolated target.
3. Any failure message must say *what / where / why / retryable / operator action*.
4. Nothing fakes a missing dependency: no fake provider, no fake success —
   unverifiable legs are recorded as such, never greened.

Companion docs: `r2-storage.md` (R2 setup & secrets), the backup section of
`deployment-koyeb.md`, `DEPLOY_RUNBOOK.md` (deploy/rollback).

---

## 1. The chain, and what proves each link

| # | Stage | State in drill report | Proof |
|---|-------|-----------------------|-------|
| 1 | Dump source database (online sqlite backup / `pg_dump`) | — | non-empty artifact, page-level copy while writers run |
| 2 | Structural verification of the artifact | `INTEGRITY_VERIFIED` | sqlite `integrity_check` on an isolated copy + table inventory; pg: `pg_dump` completion footer |
| 3 | sha256 measured | `CHECKSUM_*` | exact 64-hex; recorded in summary + sidecar |
| 4 | Upload → R2 | — | `create_backup()` succeeds |
| 5 | Read-back = identical bytes | (verified roundtrip) | `RuntimeError` on any drift → loud red, never green |
| 6 | Sidecar manifest uploaded + read-back verified | — | `<key>.meta.json` self-describing (sha256/size/timestamp/source/verification) |
| 7 | Drill fetch via `--latest`/`--from-r2` | `FETCHED` | downloadable artifact from bucket alone |
| 8 | Drill-verify (default mode) | `RESTORE_PROVEN` | the artifact is provably restorable |
| 9 | `--apply` into isolated target | `SAFETY_COPY`, `POST_RESTORE_INTEGRITY` | atomic replace + target re-opened |
| 10 | App-readable state | `APP_SCHEMA_READABLE` | every ORM table present on the restored DB (`--expect-app-schema`) |

Backup objects live at `backups/db/<YYYYMMDD-HHMMSS>/<file>` (+ `.meta.json`
sidecar). Housekeeping retention (weekly) prunes by stamp; a *stamped* object
at rest is never deleted by the drill.

## 2. Operator procedure: prove it tonight

```bash
# A) Create-and-verify a backup (same entry point the nightly job runs):
python - <<'EOF'
from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.maintenance.backup import create_backup
print(create_backup(settings=get_settings()))
EOF
#    Watch for: sha256 + size_bytes + timestamp + verification.roundtrip =
#    "byte-identical" + sidecar.verified. Anything red is ACTIONABLE (§4).

# B) Drill the recovery (read-only: refuses to touch anything live):
python -m nexus_ai_agent.maintenance.restore --latest --json
#    Expect chain RESTORE_PROVEN and a sha256 that matches the sidecar.

# C) Full recovery rehearsal into a sandbox file (sqlite leg):
python -m nexus_ai_agent.maintenance.restore --latest \
  --apply --target /tmp/nexus-rehearsal.sqlite3 \
  --expect-app-schema
#    Expect the rehearsal DB to open AND match the app schema.

# D) PostgreSQL leg (structural proof is automatic; a LIVE pg restore is
#    owner-run by design — the drill will name the external dependency):
python -m nexus_ai_agent.maintenance.restore --from-local path/to/dump.sql \
  --apply --target-url postgresql://... --force
```

## 3. CI evidence (no secrets involved)

`.github/workflows/maintenance.yml`:

- `backup-db` (nightly 03:17 UTC; manual dispatch too) — writes the backup
  evidence line (`key/sha256/size_bytes/timestamp`) to the Step Summary and
  uploads the full log artifact on failure. Red is a **signal**, not noise:
  it means "no verified backup exists — investigate the same day".
- `restore-drill` (weekly, Sundays 05:41 UTC; dispatch too) — builds a REAL
  application-schema database, runs it through the same dump function the
  nightly job uses, then applies the drill into an isolated target with
  `--expect-app-schema`. This is the honest CI leg of link 9+10 (local, no
  secrets, no fake provider). The R2 fetch legs (7–8 remotely) need owner
  credentials and stay owner-side per §2 — they are never faked in CI.

## 4. Failure taxonomy — how red jobs talk

Every remote failure raises `MaintenanceOperationError` with an
`ActionableFailure`: `what`, `where`, `why`, `category`, `retryable`,
`operator_action`. Classes (see `maintenance/failures.py`):

| category | sees | retryable? | operator action |
|---|---|---|---|
| `credentials` | InvalidAccessKeyId / SignatureDoesNotMatch / TokenRefreshRequired | no (config fix, then re-run) | fix the R2 API token pair in repo secrets |
| `bucket` | NoSuchBucket | no | `R2_BUCKET` name/account of the bucket |
| `permission` | AccessDenied | no | grant Object Read & Write on the bucket |
| `key` | NoSuchKey | no | the object does not exist (pruned?): pick an older stamp |
| `network` | endpoint/DNS/connect timeouts | yes | verify `R2_ACCOUNT_ID` (endpoint host) + connectivity, re-run |
| `transient` | SlowDown / InternalError / ServiceUnavailable | yes | re-run with backoff |
| `unknown` | anything else | yes | capture the log artifact, classify manually |

A missing configuration (no R2 env at all) raises `ProviderUnavailable`
naming `R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET`.
That is **by design**: a silently skipped backup is a silent-failure
blindspot; a red job is a working alarm.

## 5. Chaos rehearsal matrix (owner drills, quarterly)

| Chaos | Expected behaviour |
|---|---|
| Rotate the API token but old value in secrets | `credentials` class, actionable, non-retryable flag |
| Point `R2_BUCKET` at a wrong name | `bucket` class |
| Delete the newest object, run `--latest` | drill picks the previous stamp; still proven |
| Tamper 1 byte in the artifact between upload and download | `checksum MISMATCH` (sidecar or `--expected-sha256`), drill REFUSES |
| Drop the sidecar object | drill warns honestly and reports *structural-only* proof, never an upgrade |
| Truncate the sqlite artifact | `INTEGRITY` failure before any write to a target |
| Point `--target` at an existing DB without `--force` | hard refusal, existing DB untouched |
| Point `--target` at a directory | plan error naming the mistake |
| Drive the download over the timeout | `MaintenanceOperationError`, bounded (no infinite hang) |
| Run `--apply --expect-app-schema` on an app-foreign database | `missing application tables: …` — never silently "restored" |

Each of these has a regression test in `tests/unit/test_restore_drill.py`
(a recorded mutation: turning any `raise` into a `warn` turns these tests red).

## 6. Owner dependency register (who must do what, and where it is proven)

| Dependency | Side | Status / evidence |
|---|---|---|
| R2 secrets in GitHub Actions (4 vars) | owner-side | without them `backup-db` is by-design red (actionable) |
| `NEXUS_DATABASE_URL` (Neon) for pg backups | owner-side | missing → sqlite leg is backed up |
| First green dispatch producing an object under `backups/db/` | owner-side once | recorded in the Actions log artifact |
| Weekly local restore drill | repo-side (CI) | `restore-drill` job, no secrets, pinned by this file |
| Verification of the REMOTE leg end-to-end | owner-side quarterly | §2 B–C commands against the real bucket |
| Live PostgreSQL restore into a cluster | owner-side | structural leg proven in repo; live leg deliberately requires `--target-url --force` and is never faked |
| Retry/backoff policy on transient R2 failures | repo-side, task-137 (queued) | transient classes already flagged `retryable=True` |

## 7. What you should know before the DB dies

- **Where the backup is**: `backups/db/<stamp>/` in R2 + `.meta.json` sidecar
  with sha256/timestamp (self-describing).
- **How its health is proven**: structural check at upload time + byte-
  identical read-back + sidecar; weekly drill into an isolated target in CI.
- **How restore runs**: `python -m nexus_ai_agent.maintenance.restore` (§2),
  non-destructive by default (`--force` is opt-in, safety copy is automatic).
- **What is still owner-dependent**: see the register above — nothing on the
  list is hidden behind a fake green.
