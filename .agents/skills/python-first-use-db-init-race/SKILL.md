---
name: python-first-use-db-init-race
description: This skill should be used when a Python test suite or service intermittently fails with sqlite3.OperationalError "database schema has changed" (SQLITE_SCHEMA) or "database is locked" (SQLITE_BUSY) on a fresh/first-use database, or when adding tables to a project whose tests initialise a SQLite schema lazily. It diagnoses the check-then-act stampede in first-use schema initialisation and applies the serialise-then-retry fix with a deterministic regression test and a killing mutation probe.
---

# Python first-use DB init race (SQLITE_SCHEMA / SQLITE_BUSY)

## Purpose

Fix the flaky class of failures where several concurrent callers initialise a
fresh SQLite database at the same time and one of them dies with
`sqlite3.OperationalError: database schema has changed` or
`... database is locked`. The failure looks random and CI-only, survives
reruns, and is *not* caused by the test that names it — it is a
check-then-act race in the shared first-use initialiser.

## Activation criteria

Apply when **any** of these is true:

- A test or service fails intermittently with `database schema has changed`
  (`SQLITE_SCHEMA`) or `database is locked` (`SQLITE_BUSY`).
- The traceback's deepest frames are a first-use/`create_all`/`get_session`
  style initialiser, not the named test.
- Several `asyncio` tasks or threads share one lazy schema initialiser.
- You just **added tables** to a project and previously-green concurrency
  tests started flaking — adding tables widens the DDL window.

## Required context

- The initialiser that both (a) guards "already initialised" and (b) runs DDL.
- The connection/engine setup (busy timeout, WAL pragma).
- The concurrency tests that exercise first use.
- Ability to run the suite in a loop and to edit one module + one test file.

## Procedure

1. **Reproduce deterministically, not by luck.** Run the suspect test in a loop
   (`for i in $(seq 1 20); do pytest -q <node> || break; done`). If it only
   fails in the full suite, run the full suite in a loop. Record the exact
   failing node and the deepest app frame.
2. **Find the check-then-act.** In the initialiser, look for a membership guard
   (`if path in _initialized: return`) whose set is populated **after** the DDL.
   Every concurrent caller therefore passes the guard and runs its own DDL.
3. **Confirm the mechanism.** Two connections on one fresh file run DDL; the
   loser's prepared statement sees a changed schema. `sqlite3_prepare_v2`
   auto-recompiles up to `SQLITE_MAX_SCHEMA_RETRY` (default 50); if the schema
   keeps changing, `SQLITE_SCHEMA` surfaces to the application.
4. **Serialise first use.** Add a per-path `asyncio.Lock` (double-checked) so
   exactly one caller runs the DDL and the rest wait and return. Keep any
   synchronous engine/factory creation on the fast path — do not move it inside
   the lock or behind the early return, or you break subsequent callers.
5. **Make the retry predicate absorb the error.** Teach the existing
   create-conflict predicate to treat `database schema has changed` as retryable
   (it already treats `already exists`), so a cross-process stampede still
   converges.
6. **Add deterministic regression tests** (see Validation) — never a sleep-based
   "hope they overlap" test.
7. **Prove non-vacuity.** Add a mutation that removes the lock (and one that
   removes the predicate clause) and require the new tests to go RED; revert.

## Safety boundaries

- Do **not** disable WAL, drop the busy timeout, or catch-and-ignore the error.
- Do **not** move `_get_engine`/session-factory creation behind the early return.
- Do **not** hold the lock across the whole request — only across first use.
- Keep the change inside the initialiser module; do not refactor callers.

## Validation

Exact commands (adapt node names to the repo):

```bash
# focused, repeated — must be green N/N
for i in $(seq 1 20); do python -m pytest -q tests/unit/test_storage_wal_init_race.py || break; done

# deterministic single-creator assertion (spy counts DDL runs == 1)
python -m pytest -q tests/unit/test_storage_wal_init_race.py::test_concurrent_first_use_serialises_to_a_single_creator

# predicate unit test
python -m pytest -q tests/unit/test_storage_wal_init_race.py::test_schema_changed_is_a_retryable_create_conflict
```

Regression tests to add:

- **single-creator count** — spy on the DDL function; six concurrent callers
  must run it exactly once (RED if the lock is removed).
- **real-DDL stampede** — six concurrent `get_session` calls on a fresh path all
  succeed and the file ends in WAL mode.
- **predicate unit test** — `SQLITE_SCHEMA` and `already exists` are retryable;
  an unrelated error is not.

## Success criteria

- The loop run is green N/N (N ≥ 20) with the fix, and demonstrably red without.
- The single-creator test asserts exactly one DDL run.
- Both mutation probes turn the new tests RED; working tree restored clean.
- Full suite green; no new failures introduced.

## Failure handling

- If the loop still flakes, the race is elsewhere: re-read the traceback's
  deepest app frame; the named test is rarely the culprit.
- If a "fix" makes many other tests fail with "session factory is not
  initialized", you moved engine creation behind the early return — restore it.
- If the error is a *persistent* `SQLITE_SCHEMA` after retries, suspect a second
  writer doing DDL in a loop (a migration or another initialiser), not the lock.

## Known limitations

- The lock serialises callers **within one process**. Cross-process boot races
  are covered only by the retry predicate, not the lock.
- This is a first-use fix; it does not make an already-corrupt or concurrently
  migrated schema safe.
- On PostgreSQL the failure mode differs (DDL locks), so this skill is
  SQLite-specific.

## Provenance

- SQLite result code 17 `SQLITE_SCHEMA`: https://www.sqlite.org/rescode.html
  and the auto-reprepare behaviour of `sqlite3_prepare_v2`:
  https://www.sqlite.org/c3ref/prepare.html (retrieved 2026-10-14).
- NEXUS `bot523h/nexus-ai-agent`, branch `arena/nagar-asset-library-v1`,
  commit `4a8ac85`: the check-then-act in `storage/db.py::create_all_tables`
  was closed with a per-path `asyncio.Lock` + an extended conflict predicate;
  regression tests live in `tests/unit/test_storage_wal_init_race.py` and the
  probes in `scripts/asset_library_mutations.py`. Observed RED:
  `test_r_f28_concurrent_identical_learns_collapse_into_one` and
  `test_concurrent_first_use_sets_wal_without_error` failed on all three CI
  parity legs before the fix; 15/15 green after.

## Maintenance

- Re-verify if `storage/db.py`'s initialiser changes, if `_initialized_paths`
  is replaced, or if a new migration runner does DDL at first use.
- If the regression tests ever pass with the lock removed, they have gone
  vacuous — repair the spy/assertion before trusting them.
- Details and the deeper mechanism: see `references/mechanism.md`.
