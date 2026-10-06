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
| `NEXUS_DATABASE_URL` | `backup-db` | Neon/Postgres URL. **Required**: the production job refuses to fall back to a CI-local SQLite file (a dump of an empty checkout is not a backup). |
| `R2_ACCOUNT_ID` | both | |
| `R2_ACCESS_KEY_ID` | both | |
| `R2_SECRET_ACCESS_KEY` | both | |
| `R2_BUCKET` | both | |

`backup-db` runs a deterministic **preflight** as its first step: it fails in
seconds naming the exact missing secret(s), refuses a non-PostgreSQL
`NEXUS_DATABASE_URL`, and refuses when `pg_dump` is not on `PATH`. Secret
*names* are printed; values never are. The preflight is behavioural — extracted
from the YAML and executed — in `tests/unit/test_maintenance_workflow_contract.py`.

No bot token is used by this workflow. Both jobs share one
`concurrency` group (`cancel-in-progress: false`) so runs never overlap.
Test manually via the **Run workflow** button (`workflow_dispatch`).

### Verification status — `VERIFIED_WITH_LIMITATIONS`

- **Repository-side (verified):** preflight ordering, fail-closed configuration
  handling, least privilege (`contents: read`, `persist-credentials: false`),
  no failure masking, and scheduled-only triggers — all enforced by executable
  contract tests.
- **Owner-side / production (NOT verified here):** the real R2 upload, object
  identity, retention, and a measured RPO/RTO require the five repository
  secrets and an actual scheduled run against production. No restore proof is
  fabricated. This leg stays `VERIFIED_WITH_LIMITATIONS` until a real
  `backup-db` run succeeds with secrets set.

## CLI

```bash
nexus maintenance backup --dry-run      # show what would be dumped/uploaded
nexus maintenance backup                # dump + upload (fails loudly without R2)
nexus maintenance housekeeping --dry-run
nexus maintenance housekeeping          # stale temp files + R2 retention prune
```

Both commands are stateless and idempotent. `backup` **fails** when R2 is not
configured (a silently-skipped backup is worse than a red job); `housekeeping`
stays green and only does the local temp cleanup.

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
