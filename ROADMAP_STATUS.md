# ROADMAP_STATUS — NEXUS AI agent (as of 2026-09-20, v3.11.0, Phase 6)

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
| Release | v3.11.0 housekeeping cut — version lock-step, continuum refresh, this document, decision log r6 | this PR | — |
| **Wave 2.5** | **Telegram surface for the slideshow pack** — `/slideshow` handler → `JobQueuePort.enqueue("slideshow_render", …)` → in-process worker runs the Wave 2c lane → D4 completion hook sends `master.mp4` back. Hard input limits (≤ 5 images / ≤ 30 s), typed failures rendered as plain user-facing messages, no new heavy dependency. **Owner decision 2026-09-20: precedes Wave 3** because no user path to Wave 2 exists yet (CLI only) | **NEXT** — own branch/PR | decision log r6 |
| Wave 3 | (a) optional local upscale stage — never in the main render path (`ffmpeg scale`/lanczos first; a small ONNX model only as an opt-in pack); (b) image generation **behind an adapter**: Pollinations by default (free, key-less), Gemini opt-in behind a key (no free tier for image-output models), core stays free | PLANNED — after Wave 2.5 | decision log r6 |
| Other 60 TDD operations | Each still needs its own contract, ownership boundary and decision entry | NOT STARTED | — |

None of the Nagar surfaces is reachable from the Telegram bot or the API yet;
Wave 2.5 is exactly that gap.

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

`.nexus/continuum.json` (schema v2) refreshed with v3.11.0: `plan` “Nagar
Phase 6”, `step` **`ebe995a`** (the Wave 2c merge), ledger rows for the merged
lifecycle line and Waves 1–2c, `test_count_expected` **586**.

The verifier's invariant counts **test functions** (an AST walk over
`tests/**/test_*.py`), not pytest's collected cases — pytest reports **622
passed** because of parametrization. The previous value (320) dated from the
PR3 line, so `nexus continuum verify` had been red since v3.10.0; it is green
again. The `env_fingerprint` (Python / Alembic / SQLAlchemy versions) is
machine-specific by design: it was recorded on Python 3.11.2 and will report a
fingerprint mismatch on another interpreter — that is drift *detection*, not a
failure of the checkout.

## Quality gates (measured 2026-09-20 at `ebe995a`)

| Gate | Result |
|------|--------|
| `make lint` | green — `All checks passed!`, 276 files already formatted |
| `make types` | green — `Success: no issues found in 173 source files` |
| `make test` | green — **622 passed, 20 skipped in ~40s** (PG-runtime legs skip without `NEXUS_DATABASE_URL`; covered by the CI `migrate-postgres` job) |
| `nexus continuum verify` | green after this refresh (`✓ continuum snapshot matches checkout`) |
| `make smoke` | full graph via FakeLLMProvider, exit 0 (fixed in `02f8d18`) |

Baseline delta since the previous revision of this file (`02f8d18`, 320
passed): +302 passed, skips unchanged (20, PG-only); ruff 211 → 276 files;
mypy 140 → 173 files. No test was removed or weakened.

CI (`.github/workflows/ci.yml`): two jobs — `test` (ruff / mypy / pytest, no
service) and `migrate-postgres` (service container `pgvector/pgvector:pg16`,
runs `nexus migrate` idempotently through head `f4a9c2e71b08`, the create-all
race regression, the head assertion, and the adapter-contract and
lifecycle-store-contract suites on the PG leg). Both were green on PR#23.

## Dependencies

Core: unchanged since v3.10.0 — no new runtime dependency entered with Waves
2a–2c (`numpy` and `pillow` were already core; tone templates are JSON, not
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
