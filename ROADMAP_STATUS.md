# ROADMAP_STATUS — NEXUS AI agent (as of 2026-09-21, v3.12.0 housekeeping, Phase 6)

Header: **No hidden migration. No hidden mutation. No implicit repair.**
Phase 6 adds: **packs are data; commands carry evidence; only the last step
of a lane is impure.**

The seven-phase roadmap (`docs/DECISION_LOG.md`): Phase 0 (control plane /
security) → 1 (core product) → 2 (local-LLM direction) → 3 (multi-provider
routing, scale-to-zero) → 4 (schema management, PostgreSQL/Neon) → 5 (durable
storage, lifecycle, R2) → **6 (Nagar creative studio — in progress)**.
Phases 0–5 are complete on `main`.

## Active workstream: Phase 6 — Nagar

| Stage | Content | State | Anchor |
|-------|---------|-------|--------|
| Design | `docs/NAGAR_70_OPERATIONS_TDD.md` accepted as the Phase 6 baseline (typed commands, content-addressed packs, Preview/Analysis/Master lanes) | ACCEPTED | PR#13 `db1ba54` |
| Wave 1 | “Green Cockpit” core — `nagar.command.v1` envelope, `Domain > Capability > OperationSpec` registry, semantic reference resolver, atomic command bus, five operations (`media.play`, `media.pause`, `timeline.mark`, `timeline.split_at_playhead`, `system.undo`) | **MERGED** — released as v3.10.0 | PR#19 `ac6c25b`, release PR#20 `c550420` |
| Wave 2a | Capability-pack substrate — strict data-only `nexus.capability-pack.v1` manifest, verifier that reports every finding, `PackRegistry` (external packs cannot introduce unknown operations) | **MERGED** | PR#21 `865780e` |
| Wave 2b | Slideshow pack `nexus.slideshow.compose` — five pure operations (`scan_assets` B, `score_images` A, `suggest_tone` A, `compose` B, `render` C), 14 tone templates as JSON, deterministic planning, evidence pinned above the bus (Pillow probe, numpy beat grid, opt-in fail-closed Gemini analysis) | **MERGED** | PR#22 `aa7b2f4` |
| Wave 2c | Render lane — plan → `RenderIR` → filtergraph → argv → **one** FFmpeg process (no shell, staging file + atomic publish, no overwrite by default), `probe_video` measures the master with the same binary, `render_from_files()` dispatches `slideshow.render` with measured evidence; CLI `nexus slideshow render` | **MERGED** | PR#23 `ebe995a` |
| Release 3.11.0 | Housekeeping cut after Waves 2a–2c | **MERGED** | PR#24 `8b27625` |
| **Wave 2.5** | Telegram `/slideshow` surface → `JobQueuePort` → existing render lane → completion delivery; five-image/30-second limits and typed failures | **MERGED** | PR#25 `316ed33` |
| Wave 3 — image generation | Isolated `ImageGenProvider`, Pollinations default and fail-closed paid Gemini; bounded retry/cache, `/imagine`, opt-in slideshow autofill, cost/consent/cleanup tests and import-boundary checks | **MERGED** | PR#26 `52329e6` |
| Release 3.12.0 | Version lock-step and release notes for Wave 2.5 + Wave 3 image generation; refresh README, roadmap, continuum and decision log | **THIS PR** — separate release commit, pending merge | base `52329e6` |
| Wave 3 — local upscale | Optional local stage, not the main render path; contract and owner approval required before implementation | **DEFERRED / NOT IMPLEMENTED** | decision log r6 and release-scope decision 2026-09-21 |
| Other 60 TDD operations | Each still needs its own contract, ownership boundary and decision entry | NOT STARTED | — |

Telegram now exposes `/slideshow` and `/imagine`; the latter and slideshow
`--fill` enforce owner/allowlist access. Plain slideshow rendering remains
upload-only. No new HTTP API surface or local upscale stage is part of this cut.

## Checkpoint lifecycle workstream (Phases 4–5) — complete

Correction to the previous revision of this file: the PR1/PR2/PR3 lineage was
**merged to `main` through PR#7 (`acdbcb7`) and released as v3.6.0** — the
“DONE on branch (NOT merged)” rows were stale. Phase 5 (R2 blob tier, scheduled
maintenance) completed with v3.9.0 (`994a509`).

| Stage | Content | State | Anchor commits |
|-------|---------|-------|----------------|
| Phase D | Alembic schema management, Postgres/Neon support | MERGED (v3.5.0) | `c81f299` → `f9bcd83` → `f93cb16` → `79b720d` → D10 `c42cccb` |
| PR1 | Read-only SQLite checkpoint adapter contract + lifecycle metadata store | MERGED via PR#7 | `f8a08d4`, `88cdfdd`, `4a06ff2`, `ca615cd`, `6d953e4` |
| PR2 C1–C4 | hooks/reconciler/health gate/kill-switch, inspect-v1 slice, O1 observability, adversarial hardening | MERGED via PR#7 | `6d172f2`, `dba1702`, `35e0ba6`, `fdef331` |
| PR3 (option A) | PG read-only adapter + shared contract, lifecycle index **in the database** (`f4a9c2e71b08`), backend-aware inspect/reconcile, human-only `golden update`, Neon runbook | MERGED via PR#7 (v3.6.0) | `063e7df` → `aed8728` → `e9e77df` → `d214e1a` / `1133df1` → `82a6377` |
| D1–D4 bundle | pending-only `resume_pending_jobs` + `nexus jobs resume`, `pypdf` extraction job, fail-safe Telegram completion hook, dead code removed | MERGED | PR#18 `d9f5cf9` |
| POST_V1 | Per-checkpoint (delta-chain) deletion surgery | DEFERRED — guarded by `POST_V1_DELETE_MARKER` + architecture test | — |

## Closed, non-merged

- PR#1 `feat/phase1-control-plane` and PR#2 `feat/phase2-local-llm` —
  **closed** (abandoned proposals; the provider seam they wanted arrived as
  litellm routing in v3.7.0). PR#5, PR#12 and PR#15 were superseded and closed.
  Branch dispositions live in `docs/DECISION_LOG.md` (“Zombie / abandoned
  branches”); deleting them on GitHub remains an owner action.

## Continuum

`.nexus/continuum.json` (schema v2) now anchors the merged feature state at
**`52329e6`** (PR#26). Wave 2.5 is corrected from stale `in_review` to merged,
Wave 3 image generation is recorded separately from local upscale, and the
v3.12.0 metadata cut remains `in_review` until its own PR merges.

The verifier counts **test functions** using an AST walk, not parametrized
pytest cases. The refreshed `test_count_expected` is **649**; pytest reports
**742 passed / 20 skipped** locally. The previous snapshot's 656 was stale.
The environment fingerprint (Python 3.11.2, Alembic 1.20.0, SQLAlchemy 2.0.54)
is intentionally machine-specific; a different interpreter reports drift rather
than silently rewriting the snapshot.

## Quality gates (reverified 2026-09-21 on PR#26 head `471803c`)

The working tree was verified byte-for-byte against the requested commit, with
a fresh local virtualenv and disabled Ruff/mypy/pytest caches before merging.

| Gate | Result |
|------|--------|
| `make lint` | green — 295 files already formatted |
| `make types` | green — 184 source files |
| `make test` | green — **742 passed, 20 skipped**, one upstream Starlette/AnyIO deprecation warning |
| GitHub `test` | **SUCCESS**, both `push` and `pull_request` runs on `471803c` |
| GitHub `migrate-postgres` | **SUCCESS**, both runs; real PostgreSQL service-container verification |

Actual CI evidence: [pull-request run 35542282107](https://github.com/bot523h/nexus-ai-agent/actions/runs/35542282107)
and [push run 35542278734](https://github.com/bot523h/nexus-ai-agent/actions/runs/35542278734).
PR#26 merged as **`52329e6022a0bdd9f3f9e287da581b752204c811`**. Release PR
checks run separately; the feature results must not be mistaken for release CI.

All 20 local skips require PostgreSQL / `NEXUS_DATABASE_URL`; no new skip marks
were added. Local verification did not install the two heavy inference stacks
or exercise paid image APIs. Hosted CI installs the complete `.[dev]` set. The
PostgreSQL job checks idempotent migrations through `f4a9c2e71b08`, the create-all
race and the adapter/lifecycle contracts on PostgreSQL.

## Dependencies

Core: unchanged since v3.10.0 — no new runtime dependency entered with Waves
2a–3 image generation (`httpx`, `numpy` and `pillow` were already core;
retries use the standard library; tone templates are JSON, not
YAML; the beat detector is numpy, **not `librosa`**; upscaling is not in the
render path, **no Real-ESRGAN/torch**). `ffmpeg` is the single declared
external binary of the slideshow pack. Heavy flags that remain from earlier
phases: `sentence-transformers` (pulls torch transitively), `llama-cpp-python`
(compiles llama.cpp from source), `chromadb`.

Extras: `[dev]` gains `imageio-ffmpeg` (Wave 2c) so the render tests execute a
genuine encode on any machine; production uses the system FFmpeg
(`NEXUS_FFMPEG_BIN` overrides the lookup).

## Known doc/spec friction

- `REQUIREMENTS_LEDGER.md` is the PR1/PR2/PR3 lifecycle ledger and contains no
  `R-0XX` rows; identifiers such as `R-001`/`R-026` (and any `R-003` /
  `R-005` cited in planning conversations) are citation labels only — see the
  numbering table in `docs/DECISION_LOG.md`.
- `ConversationStorePort` (durable message history) has no adapter that
  implements the port; the bot uses `features/conversation_store.py` directly
  (SQLite, working). Closing that port/adapter gap is a small, non-blocking
  item.
- ~~`docs/architecture/DATA_LIFECYCLE.md` described a `nexus_operation_journal`
  table~~ — resolved earlier; the journal table remains forbidden until Stage 3.

## Appendix — 2026-09-21, color/exposure lane (session `arena/01a0c58e-nexus-ai-agent`)

Appended, not rewritten: the tables above remain the record of Waves 1–3 and the
lifecycle line. This appendix records one wave plus a forensic correction.

**Baseline this wave started from.** `main` at `b422512` (the PR#42 merge,
`ci(task-113)` lint-fast rail). Measured on that base before any edit:
`pytest -q -m "not slow"` → **1181 passed, 20 skipped**; `ruff check .` → 0
errors; `ruff format --check .` → nothing to reformat; `mypy src` → clean, 221
source files.

**What this wave added.**

| Item | Content | State |
|---|---|---|
| `exposure` lane op | `ExposureOp` in `creative/rendering/ir.py` — the executable twin of the pack op `color.adjust_exposure`. Photometric `gamma = clamp(2**EV, 0.1, 10.0)`, contrast pass-through, `temperature_k` 1000–40000 (6500 = the filter's own neutral), `tint/50 → colorbalance gm`, explicit `pl` | **IMPLEMENTED** |
| Compiler stage | `_exposure_stage()` in `creative/rendering/compiler.py`: `eq` in YUV, then at most one shared `yuv420p → rgb24 → yuv420p` round trip for the RGB-only filters; both RGB stages elided at neutral; audio-only lane with an exposure op is a `LaneError` | **IMPLEMENTED** |
| Golden pins | `tests/unit/test_rendering_lane_exposure.py` — 16 cases, including both clamp ends as contracts against FFmpeg's declared ranges | **IMPLEMENTED** |
| Algebra property guard | `tests/unit/test_lane_duration_algebra.py` — 162 cases: 40 seeded random lanes × 4 invariants + 2 structural guards; duration algebra restated independently, argv `-t` cross-checked, byte-identical recompile | **IMPLEMENTED** |
| Decisions | D-0005 (no `SplitOp`), D-0006 (continuum only at a release cut), D-0007 (EV mapping + every colour bound sourced from FFmpeg's filter code), D-0008 (no `tonemap=hable` yet) | **RECORDED** in `docs/DECISION_LOG.md` |
| Runbook | `docs/ops/COLOR_LANE.md` — mapping table, seven hard contracts, FFmpeg-free validation command, troubleshooting table | **ADDED** |

Gates after the wave: **1359 passed, 20 skipped** (+178, no new skips, no
regressions); `ruff check .` 0 errors; `ruff format --check .` nothing to
reformat; `mypy src` clean.

**Numbering warning.** "Wave 8" is overloaded in this repository. PR#36 shipped
*"wave 8 apply lane"* (the lane itself). This wave is the colour/exposure stage
*inside* that lane and is deliberately labelled by session id rather than by a
second "wave 8". The same hazard applies to the decision ids: `D-0007` is not
the historical `D7`.

**Forensic correction — a reported wave that left no artifact.** A prior session
reported six committed "wave 5–8" commits (`ae1b9ef`), 1228 passing tests, a
`docs/ops/COLOR_LANE.md`, `docs/audits/WAVE5..WAVE7`, decisions D-0005..D-0008,
and a pending `git push` + `gh pr create` from `arena/01a0c506-nexus-ai-agent`.
None of that is in this repository:

- `git cat-file -t ae1b9ef` → `fatal: Not a valid object name` (checked against
  all 292 commits of `main` *and* the remote tip of that branch, `f265995`);
- `arena/01a0c506-nexus-ai-agent` is **already an ancestor of `main`** — it was
  merged as **PR#41** on 2026-09-21 18:38Z, so pushing it again is a no-op and
  opening a PR from it is impossible;
- at this base commit `ExposureOp`, `colortemperature`, `deband` and `SplitOp`
  had **zero** occurrences under `src/` and `tests/`, and no `COLOR_LANE.md` or
  `WAVE5..7` audit existed;
- the reported test count did not match either: the real base was 1181.

The substance was therefore rebuilt here from the FFmpeg sources rather than
restored. Everything above is verifiable in this working tree.
