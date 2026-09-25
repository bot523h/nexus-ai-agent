# Cloudflare R2 — technical blob tier (v3.9.0, Phase 5)

R2 stores **maintenance blobs only**: nightly database backups and heavy RAG
documents. It is **not** part of the user-file cloud round-robin
(`storage/unified_cloud.py`) — user files keep flowing through the existing
providers.

Routing rule (one line, `storage/ai_storage_manager.py`):

```python
BLOB_KEY_PREFIXES = ("backups/", "rag-docs/")  # → R2 only
```

## 1) Create the bucket

1. Cloudflare Dash → **R2** → **Create bucket** (default settings are fine).
2. Note the **Account ID** on the right side of the R2 overview page.

## 2) Create an API token (single-bucket scope)

1. Dash → **R2** → **Manage R2 API Tokens** → **Create API Token**.
2. Permissions: **Object Read & Write**.
3. Scope: **Apply to specific buckets only** → select **only** your bucket.
4. Copy the generated **Access Key ID** and **Secret Access Key**
   (the secret is shown once).

## 3) Fill `.env`

```env
R2_ACCOUNT_ID=<account id>
R2_ACCESS_KEY_ID=<access key id>
R2_SECRET_ACCESS_KEY=<secret access key>
R2_BUCKET=<bucket name>
```

Leave them empty to disable the blob tier entirely.

## 4) GitHub Actions secrets

`.github/workflows/maintenance.yml` runs two scheduled jobs
(**never** on push/PR): `backup-db` nightly at **03:17 UTC**, `housekeeping`
weekly Mondays **04:23 UTC**. Set these repo secrets
(*Settings → Secrets and variables → Actions*):

| Secret | Used by | Notes |
|---|---|---|
| `NEXUS_DATABASE_URL` | `backup-db` | Neon/Postgres URL; without it the job backs up the local SQLite file |
| `R2_ACCOUNT_ID` | both | |
| `R2_ACCESS_KEY_ID` | both | |
| `R2_SECRET_ACCESS_KEY` | both | |
| `R2_BUCKET` | both | |

No bot token is used by this workflow. Both jobs share one
`concurrency` group (`cancel-in-progress: false`) so runs never overlap.
Test manually via the **Run workflow** button (`workflow_dispatch`).

## CLI

```bash
nexus maintenance backup --preflight    # names what is (not) configured; exit 0 ok / 2 missing
nexus maintenance backup --dry-run      # show what would be dumped/uploaded
nexus maintenance backup                # dump + verify + upload + round-trip (fails loudly without R2)
nexus maintenance backup --require-postgres --restore-target-url postgresql://… \
                         --evidence-json out.json   # + restore proof into a scratch PostgreSQL
nexus maintenance restore-drill --source-url … --target-url …   # dump→restore proof, no R2 needed
nexus maintenance housekeeping --dry-run
nexus maintenance housekeeping          # stale temp files + R2 retention prune
```

All commands are stateless and idempotent. `backup` **fails** when R2 is not
configured (a silently-skipped backup is worse than a red job); `housekeeping`
stays green and only does the local temp cleanup.

### Backup success contract and failure classification (task-164)

A run prints `✅` only after **all** of: artifact exists and is non-empty →
sha256 measured → local integrity (SQLite: `PRAGMA integrity_check` + table
inventory in a temp DB; PostgreSQL: pg_dump completion footer) → uploaded →
**re-downloaded byte-identical** → (when `--restore-target-url` is given) the
artifact is restored into a fresh scratch database (`CREATE DATABASE …
TEMPLATE template0`, `psql -X -v ON_ERROR_STOP=1 --single-transaction`) and the
restored table inventory equals the source inventory (row-count drift is
recorded per table, never hidden). The scratch database is always dropped.

| Exit | `classification` | Meaning | Who acts |
|---|---|---|---|
| 2 | `not_configured` | Nothing attempted; each missing variable is named individually | owner (secrets) |
| 1 | `dump_failed` | DB missing / `pg_dump` failed (e.g. *server version mismatch*) | operator |
| 1 | `verification_failed` | empty/corrupt artifact, footer missing, or round-trip bytes differ | operator |
| 1 | `upload_failed` | provider refused the upload | operator |
| 1 | `restore_failed` | scratch restore errored, table set differs, or no tables came back | operator |

`--evidence-json` writes the full summary for success **and** failure
(status, classification, key, sha256, size, verification, preflight
booleans). It never contains secret values; URL credentials in error text are
redacted (`://***@`).

### The scheduled workflow, end to end

`backup-db` (nightly 03:17 UTC): PGDG PostgreSQL **17** client (Neon projects
default to 17; a 16 `pg_dump` aborts on a 17 server) → presence preflight
(booleans, mirrored as steps) → `backup --preflight --require-postgres` →
`backup --require-postgres --evidence-json` with a `pgvector/pgvector:pg17`
service container as the restore target → evidence uploaded as the
`backup-evidence-<run id>` artifact (30 days) → on failure, one GitHub issue
titled **"[backup] nightly database backup is failing"** is opened or
commented with the classification and the named missing pieces.

`restore-drill` (same schedule, **no secrets**): `nexus migrate` on a fresh
PostgreSQL 17 → `nexus maintenance restore-drill` (dump → restore → identical
inventory, row counts equal) → `tests/integration/test_backup_restore_postgres.py`
→ `restore-drill-evidence-<run id>` artifact. A red drill means the mechanism
is broken regardless of configuration and opens its own issue.

`--require-postgres` is deliberate: the runner's local SQLite file is never the
production database, so backing it up would be a fake success.

Neon notes: use the **unpooled** connection string for `NEXUS_DATABASE_URL`
(Neon documents that `pg_dump` must not go through the pooler), and keep the
project's Postgres major ≤ 17 or bump `PG_CLIENT_MAJOR` in the workflow.

## Notes

- Backup keys: `backups/db/<YYYYMMDD-HHMMSS>/nexus-pg-<stamp>.sql`
  (Postgres via `pg_dump`) or `nexus-sqlite-<stamp>.sqlite3` (online-backup
  API, safe while the app runs).
- Housekeeping deletes only keys matching `backups/db/<stamp>/<file>` with a
  parseable stamp older than the retention (default 30 days) — anything else
  under the prefix is never pruned.
- Presigned URLs (`R2Provider.generate_presigned_url`) are capped at **7
  days** (604800 s) — R2 rejects longer TTLs.
- The boto3 client is built with `request_checksum_calculation="when_required"`
  to avoid the known R2 checksum incompatibility.
