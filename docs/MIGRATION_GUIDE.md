# Migration Guide

End-user guide for taking any NEXUS database to the current Alembic-managed
head. No prior Alembic knowledge required.

## I have SQLite

1. Point NEXUS at your file and run the migrator:

   ```bash
   NEXUS_DB_PATH=/path/to/my.sqlite nexus migrate
   ```

2. What happens:

   - **Fresh file** → Alembic builds the full schema (`upgrade head`).
   - **Legacy file** (pre-Alembic, tables but no `alembic_version`) → adopted:
     idempotent `create_all` then `alembic stamp head`. Your data is preserved.
   - **Already migrated** → no-op.

## I have PostgreSQL (no Alembic / Neon from the C1 era)

1. Point NEXUS at it:

   ```bash
   export NEXUS_DATABASE_URL=postgresql://user:pass@host:5432/db
   ```

2. Inspect first (safe, read-only):

   ```bash
   nexus adopt-pg --dry-run
   ```

   - `would ADOPT (stamp head) … zero drift` → your schema matches the app
     exactly; adopt it:
     `nexus adopt-pg --yes`
   - `already Alembic-managed` → you are done.
   - `empty database` → run `nexus migrate` instead.
   - `REFUSED — schema drift (missing=…, extra=…)` → your tables differ from
     the application model. **Do not force it**: reconcile the schema (see
     below), then adopt.

## Errors and what to do

| Message | Meaning | Fix |
|---------|---------|-----|
| `Un-stamped PostgreSQL database with schema drift …` | Legacy DB, tables ≠ model | `nexus adopt-pg --dry-run`, reconcile the listed tables, re-run |
| `relation "…" already exists` (raw) | Alembic replayed initial revision on a legacy DB | use `nexus adopt-pg --yes` (or for SQLite: `nexus migrate`) |
| `NEXUS_DATABASE_URL is not set` | `adopt-pg` targets Postgres/Neon only | export the URL, or use `nexus migrate` for SQLite |

## Concurrency

`nexus migrate` is idempotent; running it twice — even concurrently — is safe.
The per-URL prepare cache means schema preparation runs once per database per
process.
