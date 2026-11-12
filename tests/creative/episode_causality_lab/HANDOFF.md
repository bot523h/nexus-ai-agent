# ATLAS lab — handoff & evidence (task-atlas-episodic-causality-lab)

**Branch:** `arena/atlas-episodic-causality-lab`
**Zone:** `src/nexus_ai_agent/creative/episode_causality_lab/**`, `tests/creative/episode_causality_lab/**`
**Verdict:** EXPERIMENTAL_WIP (advisory layer; not a registered production capability)

## Evidence — exact commands, results, exit codes

Tested SHA: the HEAD of this branch at commit time (see `git log -1 --format=%H`).

| # | Command | Result | Exit |
|---|---|---|---|
| 1 | `python -m ruff check src/nexus_ai_agent/creative/episode_causality_lab tests/creative/episode_causality_lab` | All checks passed | 0 |
| 2 | `python -m ruff format --check <same paths>` | 23 files already formatted | 0 |
| 3 | `python -m mypy --strict src/nexus_ai_agent/creative/episode_causality_lab` | Success: no issues in 12 files | 0 |
| 4 | `pytest tests/creative/episode_causality_lab --cov=... --cov-fail-under=95` | 162 passed; coverage 98.97% | 0 |
| 5 | `pytest tests/architecture -q` | 367 passed | 0 |
| 6 | `git diff --check` | clean | 0 |
| 7 | `python scripts/agent_board.py check --no-remote --branch arena/atlas-episodic-causality-lab --files <changed>` | no overlap (local verdict) | 0 |

## Real-vs-simulated classification

* REAL DISK: the end-to-end test writes the report through `WorkspaceFilesystem`
  and re-opens + re-hashes the bytes (`test_end_to_end.py`).
* REAL COMPUTATION: causal state machine, Bellman–Ford STN, DP planner, incremental
  replay — all executed on real inputs.
* MOCK/STUB: none in this package.
* NOT RUN: no real preview render. Reason: no FFmpeg-backed preview harness is
  wired for this package, and dispatching real commands requires the authorized
  CommandBus policy flow, which is out of zone. The integration contract below
  records the exact remaining wiring.
* Full repository suite: 249 baseline failures in `tests/unit/*` are environmental
  (sqlalchemy 2.0.54 naive/aware datetime mismatch + missing optional deps such as
  `telegram`); **zero** reference `episode_causality_lab`.

## Integration contract for a later authorized PR (out of zone)

1. The handoff manifest (`build_handoff`) is `advisory`; a consumer must still go
   through `creative/studio/bus.py` policy before dispatching anything.
2. `ShotCandidate.asset_ref` stays opaque; resolution to `AssetVersion`/`Revision`
   is owned by existing infrastructure — ATLAS never mints a passport.
3. A preview render would need an out-of-zone adapter that compiles
   `PlanResult.steps` into the existing rendering/compiler contracts.
