# Phase D — Schema Management Architecture

A concise engineering record of Phase D (C1 → D10). Max 200 lines by design.

## 1. Phase table

| Phase | Commit | What landed |
|-------|--------|-------------|
| C1 | `c81f299` | `NEXUS_DATABASE_URL` → PostgreSQL/Neon storage support |
| D1–D3 | `f9bcd83` | Alembic migration skeleton + V1 metadata hook |
| D4 | `799dbd0`, `f93cb16` | Initial Alembic revision (full schema, zero drift) |
| D5 | `79b720d` | `nexus migrate` CLI, Alembic-first startup |
| D6 | `29bbd41` | Legacy SQLite adoption (create_all + `stamp head`) |
| D7 | `acab2f4` | Postgres `create_all` stopgap retired; pgvector revision |
| D8 | `f61a8e1` | Token encryption at rest (Fernet) |
| D9 | `f448e45` | CI on real Postgres + pgvector (Neon test still pending URL) |
| D10 | `c42cccb` | Postgres adoption / fail-fast + continuum snapshot |

## 2. Flow diagrams

### SQLite legacy → adopt → alembic-managed

```
nexus migrate
  └─ resolve_migration_url()          # NEXUS_DB_PATH or default data/app.sqlite
      └─ decide_sqlite_bootstrap()
          ├─ "alembic"     → alembic upgrade head
          ├─ "create_all"  → _adopt_legacy_sqlite(db_path):
          │                  create_all_tables()  (idempotent, WAL)
          │                  alembic stamp head   (no replay → no CREATE clash)
          └─ "nothing"     → (stamped, no-op)
```

### Postgres legacy → adopt-pg → alembic-managed

```
get_session()  [NEXUS_DATABASE_URL set]
  └─ _ensure_pg_schema(url)
      └─ prepare_postgres(url)          # D10 seam
          ├─ _inspect(url)              # asyncpg inspect: tables, alembic_version
          ├─ decide(report):
          │    "alembic_managed" → upgrade head  (worker thread; env.py runs own loop)
          │    "migrate"         → upgrade head  (fresh DB)
          │    "adopt"           → 0-drift legacy → alembic stamp head
          │    "fail_fast"       → RuntimeError("run `nexus adopt-pg --dry-run` …")
          └─ cache normalized URL in _pg_prepared_urls  (once per URL)
```

## 3. Key design decisions

1. **Alembic-first, single source of truth.** `create_all` was the C1-era
   stopgap; D7 removed it for Postgres so migrations and the runtime can never
   drift apart. SQLite keeps `create_all` only as the legacy-adoption repair
   path, exactly once per file.

2. **Adopt, not replay (D6/D10).** Replaying the initial revision on a legacy
   database crashes on pre-existing tables. The safe path is `alembic stamp head`
   after proving the schema matches. Zero-Data-Loss > ceremony.

3. **Fail-fast on drift (D10).** A legacy database that *does not* match the
   metadata is never silently stamped: `RuntimeError` with an actionable
   message. Silent fallback was removed as an anti-pattern.

4. **`render_as_batch=True` only for SQLite.** Postgres supports real `ALTER`;
   batch mode is slower and unnecessary there.

5. **Reserved word `user`.** SQLAlchemy quotes it in Postgres DDL automatically.
   CI's real-Postgres job is the regression guard (proves the chain works).

6. **Async in Alembic.** Official async template: `asyncio.run()` in `env.py` +
   `run_sync`; from the app, Alembic runs in a worker thread (`asyncio.to_thread`).

## 4. Test references

- `tests/unit/test_migrations.py` — chain + stamp assertions
- `tests/unit/test_database_url.py` — URL resolution, startup decision, ensure-pg
- `tests/integration/test_migrate_cli.py` — `nexus migrate` CLI behaviour
- `tests/integration/test_migrate_adoption.py` — SQLite legacy adoption safety
- `tests/unit/test_adopt_pg.py` — Postgres adoption decision matrix + redaction
- `tests/integration/test_phase_d_e2e.py` — fresh/legacy/race integrity
- `tests/unit/test_continuum_snapshot.py` — snapshot write/read/verify
- `.github/workflows/ci.yml` — real Postgres + pgvector CI proof (D9 groundwork)
