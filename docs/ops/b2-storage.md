# Backblaze B2 — maintenance blob tier

B2 is the **preferred** store for maintenance blobs: the nightly database
backup under `backups/db/` and the weekly housekeeping that prunes it. It is an
S3-compatible alternative to [Cloudflare R2](r2-storage.md); the two are
interchangeable for this one lane and nothing else in the application is
affected. B2 is **not** part of the user-file cloud round-robin
(`storage/unified_cloud.py`), it never serves the Asset Library, and it never
stores RAG documents.

Which tier is used is decided at runtime by `build_blob_provider()` in
`src/nexus_ai_agent/maintenance/backup.py`:

1. a **complete and valid** B2 configuration wins;
2. otherwise a **complete** R2 tier is used (the fallback);
3. a **partial** B2 configuration (some, not all, of the five names) with no
   complete R2 tier raises `ProviderUnavailable` naming the missing setting — a
   half-finished migration is loud, never a silent write to the wrong bucket.
   A partial B2 alongside a *complete* R2 tier is accepted and falls back to R2,
   matching the workflow preflight (which accepts either complete tier).

## 1) Create the bucket and a scoped application key

1. Backblaze → **B2 Cloud Storage** → **Buckets** → **Create a Bucket**.
2. Note the bucket's **Endpoint** (e.g. `s3.us-east-005.backblazeb2.com`) and
   its **Region** (e.g. `us-east-005`). A B2 account lives in a single region,
   and the endpoint host must match that region.
3. Backblaze → **Application Keys** → **Add a New Application Key**.
   * Allow access to **only this bucket**.
   * Grant **Read and Write** (and **List** so housekeeping can enumerate).
   * **Never** use the master application key.
4. Copy the **keyID** and the **applicationKey** (the secret is shown once).

## 2) Fill `.env`

```env
NEXUS_B2_KEY_ID=<keyID>
NEXUS_B2_APPLICATION_KEY=<applicationKey>
NEXUS_B2_BUCKET=<bucket name>
NEXUS_B2_ENDPOINT=https://s3.us-east-005.backblazeb2.com
NEXUS_B2_REGION=us-east-005
```

Leave them empty to disable B2 (R2 remains the fallback). The bare `B2_KEY_ID`
/ `B2_APPLICATION_KEY` / `B2_BUCKET` / `B2_ENDPOINT` / `B2_REGION` names are
accepted as legacy aliases, but the five `NEXUS_B2_*` names are canonical.

## 3) GitHub Actions secrets

`.github/workflows/maintenance.yml` maps exactly these five names into the
`backup-db` and `housekeeping` jobs:

| Secret | Value |
|---|---|
| `NEXUS_B2_KEY_ID` | the application key id |
| `NEXUS_B2_APPLICATION_KEY` | the application key (secret) |
| `NEXUS_B2_BUCKET` | the bucket name |
| `NEXUS_B2_ENDPOINT` | `https://s3.<region>.backblazeb2.com` |
| `NEXUS_B2_REGION` | `<region>` (must match the endpoint) |

The `backup-db` preflight requires **one complete blob tier** (B2 or R2) plus a
PostgreSQL `NEXUS_DATABASE_URL`. When neither tier is complete it fails in
seconds, names the missing B2 secrets, and uploads nothing — a silently skipped
backup is worse than a red job.

## 4) Fail-closed rules

Every one of these is validated *before* any network call and raises a typed
`ProviderUnavailable` that names the setting (never the value):

* all five fields are present;
* `NEXUS_B2_ENDPOINT` is `https://` and its host ends in `.backblazeb2.com`
  (pointing the key at a non-B2 host is refused — it would leak the key);
* the endpoint host and `NEXUS_B2_REGION` agree (`s3.<region>.backblazeb2.com`);
* the bucket name is valid (6–63 chars, lowercase letters/digits/hyphens,
  starts and ends alphanumeric);
* every remote key is confined to the `backups/db/` prefix, so a caller bug can
  never read or delete the RAG or user-file namespaces.

## 5) Rotation

Rotate by adding a new application key in the Backblaze console, updating the
five repository secrets, and only then deleting the old key. Because the
provider validates the endpoint and region, a copy-paste error in rotation is
refused at the boundary rather than surfacing as an opaque `boto3` error.

## 6) Restore

The nightly backup is only a claim until it is restored. The weekly
`restore-drill` job proves the **local** artifact→recovery chain without
secrets; the **remote** leg (`--from-r2` / `--latest`) fetches the newest object
through whichever blob tier is configured and is documented in
[DISASTER_RECOVERY_RUNBOOK.md](DISASTER_RECOVERY_RUNBOOK.md).
