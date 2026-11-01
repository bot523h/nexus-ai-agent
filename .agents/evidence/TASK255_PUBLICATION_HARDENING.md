# Task-255: AttemptStaging-based Publication for creative_render

**Status:** HARDENED_BUT_NOT_COMPLETE
**Claim:** gen 1, owner arena/1f7a74db-nexus-ai-agent, TTL 24h
**Branch:** arena/1f7a74db-nexus-ai-agent
**Base SHA:** 691020102be9587bc20dc88dd81407ffeaf48ec9
**Implementation commits:**
- `53e8d17` — AttemptStaging-based publication callbacks for creative_render
- `f1b564d` — mandatory race tests for publication hardening
- `1ecc584` — mutation probes + atomic rename tests
- `27a2be8` — composition root wiring into bot/app.py

## Summary

Implemented `AttemptStaging`-based publication callbacks for `creative_render`
jobs, wired into the queue's `ArtifactPublication` contract. The publication
stages the artifact in a per-attempt staging tree, keeps a `.prev` backup of
any previously published artifact, and atomically publishes to the final
destination via `os.replace`.

## Components

| Component | File | Description |
|---|---|---|
| `creative_render_publication_callback` | `render_jobs.py` | Stages artifact, keeps .prev backup, atomic publish |
| `creative_render_retract_callback` | `render_jobs.py` | Removes staged temp, restores previous on post-publish refusal |
| `creative_render_finalize_callback` | `render_jobs.py` | Best-effort staging cleanup after durable commit |
| `build_creative_render_publication` | `render_jobs.py` | Factory wiring callbacks into ArtifactPublication |
| Composition root wiring | `bot/app.py` | Registers publication in InProcessJobQueue |

## Tests (17 total)

### Publication callback tests (6)
1. `test_creative_render_publication_stages_and_publishes`
2. `test_creative_render_publication_keeps_previous_as_backup`
3. `test_creative_render_retract_restores_previous`
4. `test_creative_render_finalize_cleans_staging`
5. `test_creative_render_publication_is_idempotent`
6. `test_creative_render_publication_via_queue`

### Mandatory race tests (5)
7. `test_creative_render_publication_attempt_isolation`
8. `test_creative_render_publication_crash_recovery_states`
9. `test_creative_render_publication_no_unsafe_direct_write`
10. `test_creative_render_publication_concurrent_publishers`
11. `test_creative_render_publication_retry_after_ambiguous_crash`

### Hardening tests (3)
12. `test_creative_render_retract_removes_staged_temp_pre_publish`
13. `test_creative_render_publication_atomic_rename_preserves_inode`
14. `test_creative_render_publication_callback_atomic_rename`

### Existing tests (3)
15-17. Original creative chain e2e tests

## Mutation Probes (5/5 caught)

| # | Mutation | Result |
|---|---|---|
| 1 | Remove staging write | CAUGHT (11 tests failed) |
| 2 | Remove .prev backup | CAUGHT (2 tests failed) |
| 3 | Replace atomic publish with direct copy | CAUGHT (1 test failed) |
| 4 | Remove retract backup restore | CAUGHT (14 tests failed) |
| 5 | Remove retract staged temp cleanup | CAUGHT (1 test failed) |

## Verification

- All 17 creative chain integration tests pass
- All 42 heartbeat integration tests pass (combined: 59 passed)
- 5/5 mutation probes caught
- Lint (ruff) + format clean
- Composition root wiring verified (publication registered in queue)

## Known Limitations

1. **attempt_id NOT in payload or result** — The ArtifactPublication callback
   receives raw payload and handler result. Neither contains the queue-minted
   attempt_id. The callbacks derive per-execution uniqueness from workspace and
   artifact path hashes. This is NOT the same as queue-minted attempt fencing.
   Full attempt-token fencing requires queue changes (blocked by task-256).

2. **Same artifact name → published path collision** — Two attempts with the
   same artifact name collide at the published destination. Acceptable for
   distinct attempts (different artifact names).

3. **Race tests with queue integration pending** — Pause owner A, let B become
   authoritative, resume A, assert A cannot overwrite B. Requires queue
   changes blocked by task-256 lease.

## Governance

- task-255 CLAIMED via board CLI (gen 1, TTL 24h)
- task-256 lease NOT touched (still active)
- PR #283 NOT merged/closed/approved
- No safety assertions weakened
- No evidence fabricated
- Status: HARDENED_BUT_NOT_COMPLETE (awaiting task-256 merge for full integration)
