# F1 artifact-binding ownership fence — live-truth audit (2026-11-08)

Independent overnight audit of the task-282 Slice-2 **F1 ownership fence**
(`fix/binding-ownership-fence-20261107`). Method: **trust nothing from the
brief** — every claim was re-derived from git, the GitHub API, the board, CI and
a real PostgreSQL run before it was accepted.

## 1. Live truth (re-derived, not assumed)

| Fact | Value | How it was derived |
| --- | --- | --- |
| `origin/main` | `5ed6d4e10be55f5baf5e9f2a8aa99775577621db` | `git rev-parse origin/main` |
| Branch head | `5e80b23f34bfe3c1531fadb55ae6fb05310795b3` | `git rev-parse HEAD` (4 commits ahead of `main`, 0 behind) |
| Branch pushed? | **No** | no remote branch matches the local head |
| main alembic head | `20261106artbind` | `git show main:migrations/versions/` — **no `20261107bindfence`** |
| PR #319 | merged (`65c020cf…`) | GitHub API: `state=closed, merged=true, base=main` |
| PR #319 content | **Stage 1 only** | merge diff: adds `20261106_artifact_revision_binding.py`, **not** the fence |
| Fence on any remote branch | **None** (0 of 247) | `git ls-tree` over every `origin/*` ref |
| PR #324 | open (`unstable`) | other agent, `openhands/nagar-one-final-closeout-20261107` |
| PR #294 | open, `mergeable_state=dirty` | Slice-3 prerequisite |
| Env | `NEXUS_DATABASE_URL` unset | clean (the earlier env leak is gone) |
| PostgreSQL | 16.15 (docker `pgvector/pgvector:pg16`) | local audit harness |

## 2. Findings

### F-T1 (HIGH, corrected) — a FALSE "merged" claim in the board

`.agents/board.json` recorded, for `task-282-slice-2-artifact-assetversion-binding`:

> "F1 ownership fence: migration `20261107bindfence` adds the queue (job_id,
> attempt) fencing token … Evidence: harness 29/29 killed …"

and a `completion_record` asserting the fence was delivered through PR #319.

Both are **false**. PR #319 contained **Stage 1 only**: it added
`20261106_artifact_revision_binding.py` and the binding table, **without** the
`attempt` column. `main` has **no** `20261107bindfence`, **no** `attempt` column
and **no** `retract()`. The fence exists on **no** remote branch. The claim was
corrected in `board.json` (the record now states Stage 1 is on `main` and the
fence is delivered by the open PR), with an explicit CORRECTION note. This is a
governance-integrity defect: a false "merged" record is worse than an honest
"pending".

### F-T2 (HIGH, live on main) — an orphan binding survives a lost terminal CAS

`main`'s queue commit path is:

```python
if publication is not None and publication.confirm is not None:
    confirm_error = await self._confirm_safely(...)  # commits the binding
    ...
committed = await self._mark_completed(...)  # terminal CAS
if not committed:
    self._log_rejected(job_id, claim, JobStatus.COMPLETED)
    return  # <-- the binding stays
```

When a takeover lands *inside* the confirm window, the confirm commits the
binding and the `COMPLETED` CAS then fails — and `main` leaves the binding in
place. That is durable provenance for an attempt that never reached `COMPLETED`,
which a later attempt can neither overwrite (append-only) nor remove (no
`retract`). This branch fixes it by recording the queue-minted `(job_id,
attempt)` token and retracting exactly the row the lost attempt wrote.

### F-T3 (MEDIUM) — the durable mutation harness lacked the shadowed-import guard

Four of the five mutation harnesses pin `PYTHONPATH` to the tree under mutation
(GOV-1: in an isolated worktree the package is also editable-installed, so
without the pin the mutated bytes are never imported and every probe falsely
survives). The durable harness (`scripts/durable_project_state_mutations.py`)
did **not**. Fixed: it now pins `PYTHONPATH=ROOT/src` and runs a
`--collect-only` preflight that fails fast and closed when a target suite does
not even collect (a missing optional dependency would otherwise turn the
baseline RED with an opaque message only after the multi-minute campaign
started). Anchored by four new unit tests.

### F-T4 (INFO) — non-unique anchor risk (already guarded)

The harness applies `str.replace(old, new, 1)` with no uniqueness check of its
own; a duplicated anchor would mutate only the first occurrence. This is already
caught statically by
`test_durable_project_state_anchors_occur_exactly_once`, which requires every
anchor to occur exactly once. No change needed.

## 3. Verification performed

* **Mutation campaign** (independent reproduction, clean tree, env unset):
  `29/29 killed`, baseline GREEN, restored baseline GREEN.
* **Multi-process PostgreSQL fence test**
  (`tests/integration/test_artifact_binding_postgres_concurrency.py`) against a
  real PostgreSQL 16.15: `3 passed`, repeated ×4 with no flakes.
* **Migrations** applied cleanly on PostgreSQL to head `20261107bindfence`.
* **Adversarial reading** of the fence: the token advance and the retraction are
  both scoped to the owning `job_id`, so a different job publishing identical
  bytes (the same content address) can neither seize the canonical row nor
  delete another job's binding — confirmed by
  `test_two_independent_jobs_sharing_bytes_are_fenced_apart`.

## 4. Residual (honest, non-terminal)

* **R1** — the fence is not on `main` until this branch's PR is merged.
* **R2** — the `as an AssetVersion` leg is not wired end-to-end: the producer
  (`render_jobs`) never registers the artifact in the Asset Library nor
  populates `artifact_version_id` (the column and link exist and are exercised
  by tests, but no live producer fills them).
* **R3** — a crash *after* the confirm commit and *before* the terminal CAS
  leaves the binding as append-only history; this is the documented, honest
  model (the job is not `COMPLETED`; a completed job can never lack its
  binding), not a defect. See
  `test_crash_after_confirm_before_completed_recovers_honestly`.

_This audit was produced by an AI agent (OpenHands) on behalf of the repository
owner._
