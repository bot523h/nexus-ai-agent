# Phase D — Schema Management Architecture

Status: **complete** (D9 partially — see *Known limitations*).
Released as **3.5.0**.

Phase D replaced "the schema is whatever `SQLModel.metadata.create_all` happens
to produce today" with an explicit, versioned, portable migration chain, and
added PostgreSQL (Neon) as a first-class backend.

---

## 1. Phase ledger

| Step | Commit | What landed | Tests added |
|---|---|---|---|
| C1 | `c81f299` | `NEXUS_DATABASE_URL` → PostgreSQL/Neon; URL parsing, async engine, Postgres checkpointer | `test_database_url.py`, `test_postgres_checkpointer.py` |
| D1–D3 | `f9bcd83` | Alembic skeleton, `migration_metadata` target hook, `resolve_migration_url` mirroring the runtime backend priority | `test_migration_metadata.py` |
| — | `4a8b267` | `scripts/bootstrap_env.py` — deterministic dev-env contract (pins read from `pyproject.toml`) | — |
| D4 | `799dbd0` | Initial revision `47903d282ede` (30 tables, hand-audited for both dialects) | `test_migrations.py` |
| D4 finalize | `f93cb16` | Bootstrap helper as single source of truth | `test_bootstrap_env.py` |
| D5 | `79b720d` | `nexus migrate`; Alembic-first startup (`ensure_startup_schema`) | `test_migrate_cli.py` |
| D6 | `29bbd41` | Legacy SQLite adoption before stamping | `test_migrate_adoption.py` |
| D7 | `acab2f4` | Postgres `create_all` stopgap retired; pgvector revision `2a1c4b6d8e9f` (chain head) | (updated 4 files) |
| D8 | `f61a8e1` | Token encryption at rest (Fernet, key from `NEXUS_SECRET_KEY` only) | `test_crypto.py` |
| D9 | `f448e45` | CI job proving the chain on a real `pgvector/pgvector:pg16` container | — (CI only) |
| D10 | `e7f944d` | Legacy PostgreSQL adoption (`nexus adopt-pg`) + fail-fast on un-stamped databases | `test_adopt_pg.py`, `test_fail_fast_unstamped.py` |
| D10 fix | `a875e28` | Concurrent `create_all` converges instead of colliding | `test_create_all_race.py`, `test_migrate_race_condition.py` |

Test count: **179** at `f448e45` → **277 collected / 275 passing + 2 skipped** at
Phase D exit (`make test`).

---

## 2. Backend selection — one rule, used everywhere

```
NEXUS_DATABASE_URL set (and non-blank)?
├── yes → PostgreSQL   (normalised to postgresql://, driven as postgresql+asyncpg://)
└── no  → SQLite       (NEXUS_DB_PATH, default data/app.sqlite)
```

`resolve_database_url()` answers the question; `resolve_migration_url()` returns
the same answer in the form Alembic needs. Runtime and migration therefore
cannot disagree about which database they are talking to — that was the C1/D3
invariant and every later step reuses it rather than re-deriving it.

---

## 3. Startup flow

```
ensure_startup_schema()
│
├── SQLite
│   ├── file absent / empty / already stamped → alembic upgrade head   {"sqlite", "alembic"}
│   ├── tables but no alembic_version         → adopt: create_all + stamp head
│   │                                                                   {"sqlite", "legacy_adopted"}
│   └── already initialised this process      → nothing                 {"sqlite", "none"}
│
└── PostgreSQL
    ├── probe unreachable (Neon scaled to zero) → defer to first query  {"postgresql", "deferred"}
    ├── stamped or empty                        → defer to first query  {"postgresql", "deferred"}
    ├── tables, no stamp                        → raise UnstampedPostgresError
    └── tables, no stamp, expected tables gone  → raise IncompatiblePostgresSchemaError
```

`get_session()` prepares PostgreSQL lazily through the same `run_migrations`
path, once per URL. `ensure_startup_schema` deliberately does **not** migrate a
serverless host at boot: a Neon database that is idle or scaled to zero must
not stop the bot from starting.

---

## 4. The two adoption flows

**SQLite (D6) — automatic.** A local single-user file carries no risk that
justifies an extra manual step, so `nexus migrate` and bot startup adopt it
silently: idempotent `create_all` repairs drift, then `alembic stamp head`
records it as versioned. The initial revision is never replayed over existing
tables, so no row is lost. There is no `nexus adopt` command, by design.

**PostgreSQL (D10) — explicit.** A shared hosted database is a different risk
class, so adoption requires a command:

```
nexus adopt-pg --dry-run    # inspect, change nothing
nexus adopt-pg --yes        # create missing tables + stamp at head
```

The decision is a pure function of one introspection report:

| `alembic_version` | user tables | expected tables present | state | action |
|---|---|---|---|---|
| yes | any | any | `managed` | nothing |
| no | none | — | `fresh` | `alembic upgrade head` |
| no | some | yes | `adoptable` | create_all + stamp head |
| no | some | no | `incompatible` | **refuse** |

`incompatible` refuses even under `--dry-run`: stamping a schema the database
does not have would be the actual data-loss risk, because every later revision
would then be applied to a database that silently disagrees with it.

---

## 5. Key decisions and why

**Alembic-first, `create_all` only for adoption.** Alembic is the single source
of schema truth. `create_all` survives in exactly one role — repairing a
pre-Alembic database up to the current models so it can be stamped — because it
is idempotent and therefore cannot lose data.

**Fail-Fast over silent adoption on PostgreSQL.** Both principles point the
same way once you notice that auto-adopting a *shared* database mutates
something other processes are using. The guard fires before Alembic writes
anything and names the command to run.

**Never block startup on a serverless host.** The D10 probe fails fast only
when it can actually reach the database. Unreachable means "cannot tell", which
defers — preserving the pre-D10 guarantee.

**Credentials never appear in messages.** `NEXUS_DATABASE_URL` carries a
password; every user-facing string goes through `redact_url()`. Quoting the URL
in an error would leak it into logs and CI output, undoing D8.

**Concurrent bootstrap must converge.** `create_all(checkfirst=True)` has a
TOCTOU race: two processes both see "does not exist", both emit `CREATE TABLE`,
and the loser dies on `already exists`. `create_all_metadata` treats that
specific error as benign and retries, so racers converge. Anything else
propagates immediately.

---

## 6. Where to look

| Concern | File |
|---|---|
| Backend + URL resolution, SQLite bootstrap decision, `create_all_metadata` | `src/nexus_ai_agent/storage/db.py` |
| Alembic dispatch, startup decision | `src/nexus_ai_agent/storage/migrations.py` |
| PostgreSQL adoption + fail-fast guard | `src/nexus_ai_agent/storage/adopt_pg.py` |
| Autogenerate target metadata | `src/nexus_ai_agent/storage/migration_metadata.py` |
| Migration environment (async, per-run URL override) | `migrations/env.py` |
| Revisions | `migrations/versions/47903d282ede_initial_schema.py`, `…/2a1c4b6d8e9f_pgvector.py` |
| CLI | `src/nexus_ai_agent/cli.py` |
| Cross-turn state | `src/nexus_ai_agent/continuum/snapshot.py`, `.nexus/continuum.json` |

Operator-facing instructions live in [`MIGRATION_GUIDE.md`](MIGRATION_GUIDE.md).

---

## 7. Known limitations

- **D9 is CI-only.** The `migrate-postgres` job proves the chain against a local
  `pgvector/pgvector:pg16` service container. Running the same path against real
  Neon is blocked on `NEXUS_DATABASE_URL` from the project owner.
- **pgvector is enabled but unused.** Revision `2a1c4b6d8e9f` creates the
  extension; no column of type `vector` exists yet.
- **`nexus migrate --db-path`** still bootstraps via `create_all` for backwards
  compatibility. Prefer `NEXUS_DB_PATH`.
