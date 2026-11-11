# NEXUS-OMNI-FORENSIC-CLOSURE — Stage 0/1/2/4 evidence (2026-11-11)

Reproducible, SHA-based evidence for the overnight campaign. Every number below
was measured in this session on this machine; nothing is copied from an earlier
report. Claims are tagged **Verified** / **Evidence-based** / **Experimental** /
**Unknown**.

## Environment (Stage 0 — LIVE TRUTH)

| Fact | Value | Status |
|---|---|---|
| Host | `Linux-6.8.0-1055-gke-x86_64-with-glibc2.41 (4 cpu)` | Verified |
| Python (host default) | `3.13.15` (`/usr/local/bin/python3`) | Verified |
| CI matrix Pythons | 3.10, 3.11, 3.12 (installed here via `uv`) | Verified |
| `uv` | `0.13.5` | Verified |
| `gh` | logged in as `bot523h` via `GITHUB_TOKEN` | Verified |
| Docker | `docker info` OK (daemon started with `sudo dockerd`) | Verified |
| `main` SHA | `5fe9451228ad6e4d5ac067aaa4cbd2df8c633cd4` | Verified |
| Open PRs | 49 | Verified |
| Branch protection | GitHub API returns **403** (unreadable) | Verified |

## Stage 1 — PR #325 CI closure (the F1 ownership fence)

### Root cause (Verified — read from the failing run's logs)

PR #325 head `af00b0ad` reddened CI run `42862865698`. The failing jobs and
their **distinct** causes:

| Job | Result | Cause |
|---|---|---|
| `python-parity (3.10)` | failure | `AttributeError: module 'asyncio' has no attribute 'Barrier'` in `test_durable_project_revision_pinning.py` |
| `python-parity (3.11)` | failure | `test_pg_integration_safety.py` — unscoped `DELETE ... WHERE project_id IN (:a, :b)` |
| `python-parity (3.12)` | failure | same as 3.11 |
| `test (pytest -m "not slow")` | failure | same as 3.11 (1 failed, 6458 passed, 43 skipped) |
| everything else | success/skipped | — |

`asyncio.Barrier` was added in Python 3.11; `hasattr(asyncio, "Barrier")` is
`False` on 3.10.22 and `True` on 3.13.15 (measured).

### Fix

Two independent defects, one commit `5836084` on branch
`fix/pr325-py310-pg-cleanup-20261111` (base `af00b0ad`):

1. Replace `asyncio.Barrier(2)` with a local `_AsyncBarrier` built on
   `asyncio.Condition` — same release-together semantics, bounded wait so a
   lost participant fails loudly, and both timeout and cancellation decrement
   the arrival count so a departed waiter cannot strand the survivor.
2. Scope every cleanup `DELETE` in the PG fence module to one bound
   `project_id` at a time (never an `IN` list), keeping the FK order. The
   `artifactcontentowner` delete is now scoped by the run's `project_id`
   instead of the derived `artifact_id`.
3. Strengthen the guard: pin the exact `IN (:a, :b)` form as unsafe and add a
   survival-baseline test over **all** cleanup tables.

Files changed: `tests/integration/test_durable_project_revision_pinning.py`,
`tests/integration/test_artifact_binding_postgres_concurrency.py`,
`tests/unit/test_pg_integration_safety.py`.

### Verification (Verified)

| Check | Command | Result |
|---|---|---|
| Full pinning module (SQLite) | `pytest tests/integration/test_durable_project_revision_pinning.py` (py3.13) | **52 passed** |
| PG fence module on real PostgreSQL | `NEXUS_DATABASE_URL=... NEXUS_PG_TEST_DESTRUCTIVE=1 pytest tests/integration/test_artifact_binding_postgres_concurrency.py` | **5 passed** (postgres:16, migrated to head `20261109contentowner`) |
| Guard + barrier on real CPython 3.10.22 | `pytest tests/unit/test_pg_integration_safety.py <barrier test>` | **13 passed** |
| Barrier behaviour on 3.10 | standalone exec of `_AsyncBarrier` (release-together, timeout, cancel-safety) | **OK on 3.10.22** |
| Guard mutation | revert one `DELETE` to `IN (:a, :b)` | **2 tests RED**; restore → **12 green** |
| Lint | `ruff format --check` + `ruff check` on the 3 files | clean |

`test_pg_integration_safety.py` also asserts every registered PG-writing module
is *actually run* by the `migrate-postgres` CI job; that job runs
`test_artifact_binding_postgres_concurrency.py` (verified in `ci.yml`).

## Stage 2 — Backup, restore & operational recovery

### CI reality (Verified)

The nightly `maintenance.yml` run `42805914147` (2026-11-10, `5fe94512`) failed
in `backup-db` at the **preflight**, with
`missing repository secrets: NEXUS_DATABASE_URL`. The preflight is fail-closed
and correct; this is an owner-side configuration gap, not a code defect.
`housekeeping` and `restore-drill` were skipped as a consequence.

### Independent local drill (Verified — real PostgreSQL)

Against a disposable `postgres:16` container (schema migrated to head
`20261109contentowner`, 44 tables), two seeded rows in `appproject`:

| Step | Measurement |
|---|---|
| `pg_dump` (real) | exit 0, **74 662 bytes**, footer present |
| artifact SHA-256 | `0d01e812fdfb256ec685385b845998ab69727fc347acb67d047c74ddc5153a80` |
| restore into fresh `nexus_restore` | exit 0 |
| `appproject` rows source → restored | **2 → 2** |
| tables source → restored | **44 → 44** |
| restored alembic head | `20261109contentowner` |
| restored rows | `drill-proj-1, drill-proj-2` |

Timing (this host, 74 KB dump): **backup 180 ms, restore 674 ms** → a measured
RTO of ~0.85 s for this schema size. **RPO = nightly** (cron `17 3 * * *`), i.e.
up to ~24 h of data at risk. **Unknown**: RTO/RPO at production data volume;
this is a size-dependent estimate, not a production figure.

## Stage 4 — Security: output-path containment (nagar_ccr prototype)

The prototype (`/workspace/project`, branch
`fix/output-path-containment-20261111`, head `2f46707`) fixes a real defect:
`policy.evaluate` confined *source* paths but never the *output* path.

### Independent before/after PoC (Verified)

Same script, two checkouts (`/tmp/ccr-before` = `f223757` pre-fix;
`/workspace/project/src` = post-fix). It grants one allowed root, then asks the
runtime to write `OUTSIDE_X.png` into a **different** temp directory.

| Checkout | Policy decision | File written outside root |
|---|---|---|
| BEFORE (`f223757`) | `allowed=True`, reasons `(permissions ok, egress ok, resources ok, sources ok)` | **True (12 347 bytes)** |
| AFTER (`2f46707`) | `PathDeniedError: '.../OUTSIDE_X.png' escapes the allowed roots (...)` | **False** |

### Prototype suite (Verified)

| Check | Result |
|---|---|
| `pytest tests -q` | **36 passed** |
| `scripts/mutation_campaign.py` | **11/11 mutants killed** (incl. `policy_write_containment_removed`) |
| `scripts/demo.py` | both composites run, verify PASS, passports emitted |
| `nagar_ccr.bench` (640×360, 3 repeats) | `light.relight` best 0.0721 s / 68.9 MB; `mask.subject` best 0.0555 s / 68.9 MB |

## Residuals / honest limits

- PR #325 CI was **not** re-run here; the fix is verified locally and on real
  PostgreSQL, but a green CI on the pushed SHA is the remaining external proof.
- The `backup-db` secret gap is owner-side; it cannot be fixed from a branch.
- RTO/RPO are measured at 74 KB / 44 tables only.
- Branch protection is unreadable (403); governance state is inferred from
  `main`'s SHA and the open-PR set, not from the protection API.

This document was produced by an AI agent (OpenHands) on behalf of the
repository owner.
