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
| this file | Session record (persistence; no code impact) |

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
  (API-readable). It caught nothing (next run green) and was reverted
  (`bd74721` + revert) to leave CI untouched.
- Local robustness signal: render/artifact/jobs suites 3× green; 12
  timing-sensitive unit/integration files 5× identical (132 passed + 19
  pre-existing env failures, zero variance).

If `test` flakes again on this PR: re-add the annotation mirror temporarily —
it is the only failure channel readable from a sandbox.

## Open follow-ups

- PR#67 awaits review/merge; PR#64/58/33/60/63 still OPEN and conflicting.
- Stale board claims 165–167 + task-122 cleared during this session.
- `pyproject.toml` version field: this session did not bump; confirm release
  process expectations before merge if needed.
