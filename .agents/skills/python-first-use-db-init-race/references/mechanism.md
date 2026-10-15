# Mechanism: why the first-use DDL stampede surfaces SQLITE_SCHEMA

## The two racing shapes

A lazy initialiser usually looks like this (NEXUS `storage/db.py`, pre-fix):

```python
async def create_all_tables(db_path: str) -> None:
    normalized = str(Path(db_path).expanduser())
    engine = _get_engine(normalized)
    if normalized in _initialized_paths:  # (1) guard
        return
    await _set_wal_mode(engine)
    await create_all_metadata(engine, SQLModel.metadata)  # (2) DDL
    _initialized_paths.add(normalized)  # (3) set AFTER the DDL
```

Because the guard is set only at (3), N concurrent callers all reach (2). The
two failure shapes:

1. **`table … already exists`** — `MetaData.create_all` runs `checkfirst=True`:
   it asks "does this table exist?" and *then* emits `CREATE TABLE`. Two callers
   both see "no", both emit DDL, the loser gets `already exists`.
2. **`database schema has changed` (SQLITE_SCHEMA)** — a *different connection*
   committed DDL between this connection's `checkfirst` probe and its own DDL.
   The schema cookie this connection prepared against is now stale.

Both are benign: the winner did the loser's work.

## Why SQLITE_SCHEMA reaches Python

`sqlite3_prepare_v2` (and `v3`) store the statement text, so SQLite transparently
re-prepares a statement whose schema cookie changed, up to
`SQLITE_MAX_SCHEMA_RETRY` (default 50) attempts. Only if the schema keeps
changing across all retries does `SQLITE_SCHEMA` (result code 17) surface. Under
a concurrent DDL stampede the schema changes repeatedly, so the retries are
exhausted and the error escapes. Sources: <https://www.sqlite.org/rescode.html>
(result code 17), <https://www.sqlite.org/c3ref/prepare.html> (auto-reprepare).

## The two-part fix

1. **Serialise first use (in-process).** A per-path `asyncio.Lock`, created
   lazily (an `asyncio.Lock` must be constructed inside a running loop) and
   double-checked, so exactly one caller runs the DDL:

   ```python
   lock = _init_locks.setdefault(normalized_path, asyncio.Lock())
   async with lock:
       if normalized_path in _initialized_paths:
           return
       await _set_wal_mode(engine)
       await create_all_metadata(engine, SQLModel.metadata)
       _initialized_paths.add(normalized_path)
   ```

   Keep `engine = _get_engine(normalized)` on the **fast path** (before the
   early return): `_get_engine` also (re)creates the session factory, and a
   later caller whose engine was disposed needs it rebuilt. Moving it inside the
   lock/behind the guard makes later tests fail with
   `RuntimeError: Database session factory is not initialized`.

2. **Absorb the residual error (cross-process).** The lock is per-process; two
   processes (two replicas, a CLI migrate beside a running bot) can still race.
   Extend the create-conflict predicate so a retry re-runs `create_all`:

   ```python
   message = str(getattr(exc, "orig", exc)).lower()
   return "already exists" in message or "schema has changed" in message
   ```

## Deterministic tests (no sleeps)

- **single-creator count**: monkeypatch the DDL function with a counter wrapper,
  fire six concurrent `create_all_tables`, assert the counter == 1. RED when the
  lock is removed.
- **real-DDL stampede**: six concurrent `get_session` calls on a fresh path; all
  succeed, file ends in WAL. Companion to the count test using the real DDL.
- **predicate unit**: build `OperationalError(..., sqlite3.OperationalError(...))`
  for `database schema has changed`, `table x already exists`, `disk I/O error`;
  assert retryable, retryable, not-retryable.

## Non-vacuity probes (mutations)

- remove the `async with lock:` block → single-creator test RED.
- drop `or "schema has changed" in message` → predicate test RED.

Both were killed in NEXUS at commit `4a8ac85` (10/10 mutants in
`scripts/asset_library_mutations.py`).
