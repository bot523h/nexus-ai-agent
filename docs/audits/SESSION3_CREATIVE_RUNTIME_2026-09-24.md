# Session 3: artifact-producing Creative Runtime — record

- **Date:** 2026-09-24
- **Branch:** `arena/01a0d2e0-nexus-ai-agent`
- **PR:** https://github.com/bot523h/nexus-ai-agent/pull/67 (base `main`)
- **Base:** `035a896` (PR#65 MERGED; its CI is green)
- **Board claim:** `task-177-creative-runtime-s3` → `active_in_review`
- **Scale:** 68 files, +22 478 / −2 692 vs `main`

## Goal

Turn `main` into a trusted artifact-producing Creative Runtime: one canonical
Project → ExecutionPlan → LaneIR → compiler → executor → verification path,
artifact truth (separate logical/spec/physical sha256, ffprobe evidence,
size>0, failure≠COMPLETED), a real 3-segment A→B→C render proof, integer-µs
time algebra, real OpenTimelineIO interop, honest LUT/caption availability
(incl. RTL), fail-closed capability/pack gating, no second renderer.

## Commits on the branch

| SHA | Content |
|---|---|
| `59e7cbe` | Session 3 work (runtime, proofs, docs) + load-bearing session-2 residue |
| `eb408ca` | Board claim → review status (see §3: corrected to `active_in_review`) |
| `a28fa52` | Follow-up: legal board status, stub-ffprobe parser pins, robust LUT margin |
| `72d13ed` | Board note: CI evidence |
| `7749f85` | Session record (this file, v1 — unindexed, see postscript 1) |
| `9320bf3` | Docs-map index row for the record |
| `bd74721` | Temporary CI annotation mirror (flake hunt scaffold) |
| `e148a86` | Revert of the scaffold + hunt outcome in the record |
| `f292f11` | Test-only deflake of the reminder sent-status race |
| this file | v2: final CI evidence + PR#66 collision triage |

## CI failure triage (the one regression)

First CI on PR#67: `lint`/`lint-fast`/`migrate-postgres` passed, `test` failed —
while `main` was green, so the failure was ours. CI logs were unreachable from
the sandbox (EOF on results-receiver), so the cause was found by replication:

1. Full local battery matched base everywhere (unit fail-set identical,
   integration identical, arch 99/99, ruff clean, mypy identical).
2. A single-process CI-shape run (`pytest tests/unit tests/architecture
   tests/integration -m "not slow"`) showed exactly **one** extra failure vs the
   split runs: `test_agent_board.py::test_statuses_are_legal`.
3. Root cause: the board claim used status `"in_review"`, which is **not** in
   `LEGAL_STATUSES` — the convention is `"active_in_review"`. It only showed up
   late because the status edit landed after the split runs.

Fix (`a28fa52`): status → `active_in_review`. No order interaction, no product
code involved.

## CI-only path hardening (same follow-up)

Two paths execute only where a full engine exists, so they were hardened
without being able to run them locally:

- **ffprobe prover** (`_probe_with_ffprobe`): pinned with stub-binary tests in
  `tests/unit/test_artifact_truth.py` — parses the documented ffprobe JSON
  schema (streams/format/duration) and fails closed (no stderr-prover fallback)
  when ffprobe resolves but exits non-zero.
- **LUT pixel proof**: the x264-encoded measurement had L1 18.5 vs threshold 15
  (too tight across engine builds). The test now measures the compiled lane
  graph via `rawvideo` (encoder-independent) on a `silver` stimulus
  (measured L1 ≈ 25 vs threshold 15, ~68% margin).
- The `drawtext` title proof runs for the first time on CI's full FFmpeg
  (requires `libfreetype` + `Vazirmatn` font from the repo) — passes there.

## CI evidence (final head)

Run `35994439928` — all four jobs pass:

- `lint` (ruff + mypy + version lockstep): pass, 7m12s
- `lint-fast`: pass, 9s
- `migrate-postgres`: pass, 5m26s
- `test` (pytest `-m "not slow"`): pass, 7m37s

Previous run `35993537741` (head `a28fa52`) was also all-green; the final head
only adds a board note.

## Postscript: the unindexed-record incident (same day)

The commit adding this very file (`7749f85`, docs-only) turned CI `test` red —
not a flake: `test_every_document_is_indexed_in_docs_readme` requires every
`docs/**/*.md` to be indexed in `docs/README.md`, and the new record was not.
The failure reproduced locally in 0.52s once the right test file was run
(`tests/unit/test_docs_integrity.py`). Fix: one row in the Audits table of
`docs/README.md` (rule 1 of the docs map). Lesson recorded: docs commits must
run the docs-integrity test before push, like any other suite.

## Local proof battery (clean worktree, `/tmp/commitcheck`)

`419 passed, 1 skipped`: execution semantics, artifact truth, LUT twins,
3-segment assembly proof, time algebra, OTIO interop/roundtrip, capability
lifecycle, lane LUT/subtitle, LUT library, anti-vacuity mutations (M1–M7),
render jobs, creative surface, duration algebra, render plan, architecture.

## Deliberate scope decisions (do not undo silently)

- `grade/lut` is now EXECUTABLE, so the unsupported-path e2e test uses `noir`
  (`tests/integration/test_creative_chain_e2e.py`) — intent preserved.
- No wholesale merge of PR#33; no duplication of PR#58; queue changes only
  after reading PR#60/63. Locked paths (`delivery/models|operations`,
  `api/app`, queue infra) untouched.
- Board `exclusive_paths` must sit inside the declared zone — `bot/…` and
  `pyproject.toml` touches are recorded as non-exclusive note entries instead.
- Portrait/scene pack composition in `packs/runtime.py` + composition tests +
  `pack_coverage.py` + 2 docs came from session-2 residue and are load-bearing
  (clean-worktree proof fails without them).

## Postscript 2: one transient `test` failure, unidentified (same day)

After the docs-index fix (`9320bf3`), the `push` CI run went green while the
`pull_request` run (same minute) failed `test`. Forensics:

- `main` never moved; `git diff HEAD origin/pr/67/merge` is **empty** — both
  runs tested the identical tree, 3 seconds apart. Deterministic causes are
  ruled out: no test reads git state or `GITHUB_*` env (verified by grep; the
  one `GITHUB_*` fixture is hermetic), durations show no crash truncation.
- Verdict: a transient single-runner flake (one occurrence in 6+ runs of the
  same code), test id unknown — CI logs, job logs and the `pytest-log`
  artifact are all unreachable from the sandbox (EOF on blob storage), and
  check-run annotations carry only infra notices.
- Diagnostic scaffold used: a temporary `if: failure()` step mirroring
  `FAILED|ERROR` lines from `pytest.log` as `::error::` annotations
  (API-readable). It was reverted (`bd74721` + revert) to leave CI untouched.

## Resolution: the reminder race (same day, after the revert)

The scaffold paid off before the revert landed: the `push` run of `bd74721`
failed while its `pull_request` twin passed, and its annotations named the
test: `test_reminder_system.py::test_delivers_to_originating_chat_not_user_id`
— `assert 'pending' == 'sent'`. Mechanism (pre-existing, not session-3 code):
`ReminderSystem._deliver` sends in-memory, then marks the row `sent` via an
`asyncio.to_thread` hop; the test polled for the send but read the DB once,
immediately — winning the race against the commit on loaded runners. Fix is
test-only and mirrors the file's own `test_..._failed` pattern: poll for the
`sent` status with `_sleep_until` (3s). Product ordering (send-then-mark,
at-least-once) intentionally untouched. `tests/unit/` is inside this claim's
exclusive paths, so no zone conflict.

Validation in a fresh CI-like venv (`pip install -e ".[dev]"`, sqlmodel
0.0.42 as CI resolves — vs 0.0.47 in the stale sandbox venv, which explains
the 82 pre-existing local sqlalchemy failures): reminder file 9/9 green 5×,
render/artifact/docs/architecture 159 passed + 1 skipped. If `test` flakes
again on this PR: re-add the annotation mirror temporarily — it is the only
failure channel readable from a sandbox.

## PR#66 collision triage (parallel P0 creative pass — integrator input)

PR#66 (`arena/01a0d2d6`, "P0 creative integration", base `2cf2213`, 9 commits
behind `main`) reworks the same creative surface in parallel. Measured with
`git merge-tree` (base `2cf2213`):

- #66 vs current `main` (`035a896`): **26 conflicts** (pre-existing staleness).
- #66 vs `main`+#67: **27 conflicts** — the only one attributable to #67 is
  `docs/README.md` (adjacent audit-table rows, trivial). The
  `tests/integration/test_creative_chain_e2e.py` **add/add** (main added the
  file in its 9 commits; #66 added its own 383-line version) predates #67.
- #66 state: `CONFLICTING`/`DIRTY`, CI green on its own head. Forced merge
  order: **#67 first** (mergeable, clean, on current main, green), #66 rebases.

Textual cost is negligible; the **semantic** collisions must be reconciled by
#66's rebase (its own tests will red-flag them — resolve in this direction):

| Operation | #67 (this PR) | #66 | Correct resolution |
|---|---|---|---|
| `grade/lut` | EXECUTABLE (shipped `.cube` + `lut3d` lane + render-job proof) | refused (`NOT_AVAILABLE`, pinned) | #67 wins → flip #66's pin to executable |
| `caption/burnin` | EXECUTABLE (staged SRT + `subtitles` lane, RTL proven) | refused, pinned | #67 wins → flip the pin |
| `caption/transcribe`, `grade/proxy` | accepted at surface (inherited from main; no new lane primitive) | refused, pinned | #66 wins → keep refused (more honest) |
| `grade/otio` | accepted (medialess surface op; interop adapter in `creative/interop/`) | stance unclear from sampled hunks | verify during rebase, do not guess |
| `verify_lane_artifact` | canonical in `creative/artifacts.py` | second local copy in `adapters/creative_render_job.py` | delete #66's copy, import #67's (one artifact truth) |
| `build_lane_ir` body | plan compiler in `rendering/plan.py` | hand-built `LaneIR(main=LaneSource(...))`, docstring anticipates swapping to `compile_execution_plan` | delegate as its docstring says |
| e2e chain proof | main's chain tests + session-3 `noir` edit | 383-line worker-chain proof (5 tests) | fuse: keep BOTH assertion sets, no silent drops |

Also note: #66 touches `api/app.py`, `worker.py`, `bot/app.py` and 15 i18n
locales — broad blast radius, needs a full-suite re-verification after rebase.
#67's locked paths (`delivery/models|operations`, `api/app`, queue infra)
remain untouched by #67 itself.

## Open follow-ups

- PR#67 awaits review/merge (OPEN, MERGEABLE, CLEAN, no reviews yet).
- PR#64/58/33/60/63 still OPEN; PR#66 is CONFLICTING and must rebase after
  #67 (see triage above).
- Stale board claims 165–167 + task-122 cleared during this session.
- `pyproject.toml` version field: this session did not bump; confirm release
  process expectations before merge if needed.
