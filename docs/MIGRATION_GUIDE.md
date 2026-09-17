# Migration Guide

How to move an existing NEXUS installation onto the Alembic-managed schema
(Phase D, released as 3.5.0), for both backends.

If you are starting fresh, you do not need this document: `nexus migrate`
creates everything and stamps it at head.

---

## 1. Which backend am I on?

| Condition | Backend |
|---|---|
| `NEXUS_DATABASE_URL` set to a `postgresql://` / `postgres://` / `postgresql+asyncpg://` URL | PostgreSQL (Neon, RDS, self-hosted) |
| `NEXUS_DATABASE_URL` unset or blank | SQLite at `NEXUS_DB_PATH` (default `data/app.sqlite`) |

The same rule is used by the runtime and by migrations, so they cannot
disagree. `DATABASE_URL` is accepted as a legacy alias; `NEXUS_DATABASE_URL`
wins if both are set.

---

## 2. I have SQLite

### 2.1 Upgrade

```bash
nexus migrate
```

That is the whole procedure. Three cases are handled automatically:

| Your file | What happens |
|---|---|
| does not exist / is empty | `alembic upgrade head` builds the schema from scratch |
| has tables **and** an `alembic_version` table | pending revisions are applied |
| has tables but **no** `alembic_version` (a pre-3.5 install) | **adopted**: missing tables are created, then the file is stamped at head |

Adoption never drops a table and never deletes a row. The initial revision is
*stamped*, not replayed — replaying it over existing tables is exactly what
would fail.

There is deliberately **no** `nexus adopt` command for SQLite: a local
single-user file carries no risk that justifies an extra manual step.

### 2.2 Verify

```bash
sqlite3 data/app.sqlite "SELECT version_num FROM alembic_version;"
# → 2a1c4b6d8e9f
sqlite3 data/app.sqlite "SELECT count(*) FROM sqlite_master WHERE type='table';"
```

Re-running `nexus migrate` is always safe; the second run is a no-op.

---

## 3. I have PostgreSQL without Alembic

This is the case where a database already holds NEXUS tables — typically one
created before the `create_all` stopgap was retired, or restored from an older
dump — but has no `alembic_version` table.

### 3.1 What happens if you just run `nexus migrate`

It refuses, before writing anything:

```
Detected an un-stamped PostgreSQL database at postgresql://user:***@host:5432/db:
it already contains 30 NEXUS table(s) (e.g. adcampaign, adminlog, …) but no
`alembic_version` table, so `alembic upgrade head` would fail with a
DuplicateTable error (relation already exists).

Run:
  nexus adopt-pg --dry-run    # inspect, change nothing
  nexus adopt-pg --yes        # create missing tables + stamp at head

No data was modified.
```

The password is always redacted.

### 3.2 Adopt it

```bash
nexus adopt-pg --dry-run     # 1. look first
nexus adopt-pg               # 2. review the plan, confirm at the prompt
# or, unattended:
nexus adopt-pg --yes
```

The dry run reports the database state (`managed` / `fresh` / `adoptable` /
`incompatible`) and the planned action without touching anything.

### 3.3 Then migrate normally

```bash
nexus migrate
```

From here on the database is Alembic-managed and behaves exactly like a fresh
one.

---

## 4. Errors and what they mean

### `Detected an un-stamped PostgreSQL database …`

Tables exist, `alembic_version` does not. Run `nexus adopt-pg --dry-run`, then
`nexus adopt-pg --yes`. See §3.

### `Refusing to adopt …: the database is missing N table(s) …`

The database has *some* NEXUS-looking tables but not the full schema. Adoption
refuses — including under `--dry-run` — because stamping head would record a
schema the database does not have, and every later revision would then be
applied to a database that silently disagrees with it.

Usual causes and fixes:

| Cause | Fix |
|---|---|
| `NEXUS_DATABASE_URL` points at the wrong database | point it at the right one |
| a partial restore | restore completely, then re-run `--dry-run` |
| a different application's tables | use a dedicated database |

If the tables really are meant to be there, create the missing ones by hand and
re-run `nexus adopt-pg --dry-run`.

### `Unsupported database URL scheme 'sqlite'`

`NEXUS_DATABASE_URL` is PostgreSQL-only. For a SQLite file use `NEXUS_DB_PATH`.

### `Invalid PostgreSQL URL: missing host`

`NEXUS_DATABASE_URL` needs the full form:
`postgresql://user:password@host:port/database`.

### `NEXUS_DATABASE_URL is not set, so there is no PostgreSQL database to adopt`

`nexus adopt-pg` only applies to PostgreSQL. For SQLite just run
`nexus migrate` (§2).

### `database is locked` (SQLite, concurrent starts)

Two processes bootstrapped the same file at once. The retry in
`create_all_metadata` absorbs the usual form of this race; if you still see it,
re-run `nexus migrate` — the schema converges.

---

## 5. Downgrading

```bash
alembic downgrade -1     # one revision back
alembic downgrade base   # empty the schema (destroys data)
```

The chain is `base → 47903d282ede (initial schema) → 2a1c4b6d8e9f (pgvector)`.
`downgrade base` empties the schema and drops the `vector` extension; take a
backup first.

---

## 6. After upgrading

```bash
nexus continuum verify   # is the recorded project state consistent with this checkout?
nexus continuum show     # what does the snapshot say?
```

`.nexus/continuum.json` is committed on purpose: it records which phase is
closed, which commit was last verified green, the expected test count and what
is still blocked. A new session reads it instead of rediscovering all of that.

---

## 7. Secrets

Tokens and API keys stored after 3.5.0 are encrypted at rest with Fernet. The
master key comes **only** from `NEXUS_SECRET_KEY` in the environment — never
from the database — so a database dump alone cannot decrypt them. Set it before
first start and keep it backed up separately: losing it makes existing
encrypted values unrecoverable.
