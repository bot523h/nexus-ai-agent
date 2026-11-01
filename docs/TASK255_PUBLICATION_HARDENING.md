# Task-255: AttemptStaging-based Publication for creative_render

**Status:** HARDENED_BUT_NOT_COMPLETE
**Claim:** gen 1, owner arena/1f7a74db-nexus-ai-agent, TTL 24h
**Files:** `src/nexus_ai_agent/creative/render_jobs.py`, `tests/integration/test_creative_chain_e2e.py`

## Problem

The `creative_render` job handler writes its artifact directly to the workspace
and the completion notifier publishes it without an atomic, attempt-scoped
staging step. This means:

1. **No isolation between attempts** — a stale attempt's artifact can overwrite
   a successor attempt's published output.
2. **No recoverable backup** — if publication is refused, the previous artifact
   is lost.
3. **No crash-recovery distinction** — a crash between publication and commit
   leaves the system in an ambiguous state with no way to distinguish
   staged/published/committed.

## Solution

Implemented `AttemptStaging`-based publication callbacks for `creative_render`
jobs, wired into the queue's `ArtifactPublication` contract.

### Components

#### `creative_render_publication_callback(payload, result, artifact_root)`

Stages the artifact from the workspace into a per-attempt `AttemptStaging`
staging tree, keeps a `.prev` backup of any previously published artifact, and
atomically publishes to the final destination via `os.replace`.

- **Staging root:** `<artifact_root>/staging/<job_id>/<attempt_id>/staging/`
- **Final root:** `<artifact_root>/published/`
- **job_id:** `sha256(workspace_path).hexdigest()[:16]`
- **attempt_id:** `sha256(artifact_path).hexdigest()[:16]`

Returns `{"artifact_path": str(published), "artifact_backup": str(backup) or None}`.

#### `creative_render_retract_callback(payload, result, artifact_root)`

- **Pre-publish refusal** (artifact_path is under workspace): removes only the
  staged temp. The published artifact is untouched.
- **Post-publish refusal** (artifact_path is the published path): removes the
  refused published bytes and restores the previous artifact from `.prev`, or
  removes the published file when there was no backup.

#### `creative_render_finalize_callback(payload, result, artifact_root)`

Best-effort `staging.cleanup()` after the durable commit. Never raises.

#### `build_creative_render_publication(artifact_root: Path)`

Factory returning an `ArtifactPublication` with closure-bound callbacks. The
caller (composition root) wires this into the queue via
`register_artifact_publication("creative_render", publication)`.

## Tests (17 total in test_creative_chain_e2e.py)

### Publication callback tests (6)
1. `test_creative_render_publication_stages_and_publishes` — stages and publishes atomically
2. `test_creative_render_publication_keeps_previous_as_backup` — `.prev` backup on second publication
3. `test_creative_render_retract_restores_previous` — retract restores previous on post-publish refusal
4. `test_creative_render_finalize_cleans_staging` — finalize cleans staging directory
5. `test_creative_render_publication_is_idempotent` — repeated publication produces same result
6. `test_creative_render_publication_via_queue` — wiring verified via ArtifactPublication contract

### Mandatory race tests (5)
7. `test_creative_render_publication_attempt_isolation` — distinct workspaces → isolated staging trees
8. `test_creative_render_publication_crash_recovery_states` — staged/published/committed distinguishable
9. `test_creative_render_publication_no_unsafe_direct_write` — published artifact is a copy, not a direct write
10. `test_creative_render_publication_concurrent_publishers` — two threads publish without cross-contamination
11. `test_creative_render_publication_retry_after_ambiguous_crash` — publish→crash→retry is idempotent

### Additional hardening tests (2)
15. `test_creative_render_retract_removes_staged_temp_pre_publish` — retract removes staged temp on pre-publish refusal
16. `test_creative_render_publication_atomic_rename_preserves_inode` — atomic rename preserves inode (not copy)
17. `test_creative_render_publication_callback_atomic_rename` — callback uses atomic rename via AttemptStaging

### Existing tests (3)
14-17. Original creative chain e2e tests (render, failure, idempotent re-enqueue)

## Known Limitations

### attempt_id NOT in payload or result

The `ArtifactPublication` callback receives `payload` (raw job payload) and
`result` (handler result). Neither contains the queue-minted `attempt_id`. The
callbacks derive a per-execution unique identifier from the workspace and
artifact path hashes:

- `job_id = sha256(workspace_path)[:16]`
- `attempt_id = sha256(artifact_path)[:16]`

This provides per-execution uniqueness but is **NOT** the same as the queue's
minted attempt identifier. Full attempt-token fencing requires queue changes
(blocked by task-256 lease).

### Same artifact name → published path collision

Two different attempts with the same artifact name (`output.mp4`) will collide
at the published destination (`published/output.mp4`). The second publication
overwrites the first. This is acceptable for distinct attempts (different
artifact names), but is a known limitation for the same-name scenario.

### Composition root wiring — DONE

The publication is wired into `bot/app.py`'s composition root:

```python
_artifact_root = _Path(f"{_queue_db}.artifacts")
job_queue.register_artifact_publication(
    "creative_render", build_creative_render_publication(_artifact_root)
)
```

This makes the AttemptStaging-based publication active in the real bot.

## Mutation Probes (5/5 caught)

`scripts/task255_mutation_probes.py` applies 5 targeted mutations to the
publication callbacks and verifies the test suite catches each:

| # | Mutation | Result |
|---|---|---|
| 1 | Remove staging write | CAUGHT (11 tests failed) |
| 2 | Remove .prev backup | CAUGHT (2 tests failed) |
| 3 | Replace atomic publish with direct copy | CAUGHT (1 test failed) |
| 4 | Remove retract backup restore | CAUGHT (14 tests failed) |
| 5 | Remove retract staged temp cleanup | CAUGHT (1 test failed) |

## What's Left

1. **Race tests with queue integration** — pause owner A, let B become
   authoritative, resume A, assert A cannot overwrite B (requires queue changes
   blocked by task-256)
2. **Dedicated PR** — after task-256 is merged

## Evidence

- Commit `53e8d17`: callbacks + basic tests
- Commit `f1b564d`: mandatory race tests
- Commit `1ecc584`: mutation probes + atomic rename tests
- Commit `27a2be8`: composition root wiring
- All 17 creative chain integration tests pass
- 5/5 mutation probes caught
- Lint + format clean (ruff)
