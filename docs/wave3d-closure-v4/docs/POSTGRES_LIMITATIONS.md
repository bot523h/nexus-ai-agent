# PostgreSQL checkpointing — explicit limitations (step 15)

Status: **DEFERRED** — not tested, not claimed, not reproduced.

## What is true

* The A/B in leg 3 exercises the **SQLite** checkpointer only, because the six affected node IDs
  live in `tests/integration/test_checkpoint_adapter.py`, `tests/unit/test_reconciler.py` and
  `tests/unit/test_lifecycle_adversarial.py`, all of which use the local SQLite saver.
* PostgreSQL was not available in this environment: no server, no connection string, and no
  `psycopg`-backed saver run was performed. `asyncpg==0.31.0` is installed in the working venv, but
  installing a driver is not evidence of a PostgreSQL run.
* The repository pins `langgraph-checkpoint-postgres==3.1.2` and
  `langgraph-checkpoint-sqlite==3.1.1` on main; PR #211 loosens the sqlite pin to `>=1.0`. Nothing in
  this bundle changes either pin.
* No claim is made that PostgreSQL checkpointing is unaffected by the 3.1.2 change. The change that
  breaks the six tests (`writes.task_path` appearing in the SQLite schema golden) is a
  **SQLite-saver** detail; whether an equivalent drift exists on the PostgreSQL side was not
  investigated.

## Reproducible acceptance plan (unexecuted)

1. Provision a PostgreSQL 16 instance and export `NEXUS_POSTGRES_DSN` (the repo's integration tests
   read it; see `tests/unit/test_postgres_checkpointer.py` for the skip conditions).
2. On PR #211's tree (`878842b6`), install `langgraph-checkpoint-postgres==3.1.2` **and** `==3.1.1`
   into two venvs.
3. Run, per version, the PostgreSQL-marked subset:
   `python -m pytest -q -p no:randomly tests/unit/test_postgres_checkpointer.py tests/integration/test_checkpoint_adapter_contract.py`.
4. Acceptance: record the version, the schema/adapter hash, and either an identical result on both
   versions (⇒ the Postgres path is pin-insensitive) or a concrete divergence with the failing node
   IDs (⇒ the pin question extends to PostgreSQL).
5. Evidence required before any PostgreSQL claim: raw logs with the server version, DSN host/port
   (no credentials), installed wheel versions, exit codes, and the tree hash under test.

Until step 4 has been executed and its evidence attached, every PostgreSQL statement in this wave
stays **DEFERRED**, and no report may present PostgreSQL as verified, unaffected, or production-ready.
