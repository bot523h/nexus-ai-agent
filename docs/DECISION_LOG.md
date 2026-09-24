# NEXUS AI — Architecture Decision Log

**Status:** Canonical historical record; revision 9 effective 2026-09-24  
**r9 scope:** Gate 2 canonical command + capability reconciliation, board task-179 (session `arena/01a0d43c-nexus-ai-agent`): the v1/v2 contract conflict resolved to one canonical contract (D-0013); see `architecture/COMMAND_CAPABILITY_CONTRACT.md` and `architecture/adr/0005-canonical-command-capability-contract.md` for evidence, scoring, and limits.  
**r8 scope:** owner-directed P0 stabilization day (session `arena/01a0d23e-nexus-ai-agent`, board claims task-165/166/167): the legacy `/creative/*` HTTP lane disposition (D-0010), wiring the creative studio surface onto the canonical chain (D-0011), and verifiable backup success (D-0012). Evidence root: `docs/audits/P0_STABILIZATION_2026-09-24.md`.

**Scope:** Architectural, operational, and roadmap decisions from Phase 0 through the released v3.13.0 baseline (P0 week-1 security batch + feature-engine wiring), the accepted Phase 6 Nagar design, the implemented Nagar Waves 1–3 (2a substrate, 2b pack, 2c render lane, 3 image generation) and the owner decisions that sequence what comes next.  
**Main baseline for this revision:** `93cee5e` (the PR#34 squash merge — P0 week-1 security batch; PR#35 lint rescue merged on top). The live head may have advanced; consult `git log origin/main`.  
**Current release baseline:** `v3.13.0` (cut by the repo-hygiene housekeeping PR that carries this revision).

**Revision history**

- **r1 (2026-09-20, PR#14):** initial canonical log — phase history, core rejections, Nagar acceptance, numbering families.
- **r2 (2026-09-20):** added the Cognee and manual-Neon-keep-alive rejections, sharpened the "Nexus World" rejection (3-D world model + alleged quantum decision algorithms), converted the zombie-branch list into a per-branch disposition table with the required manual-deletion note, added the old-continuum `Phase D/E` numbering row, mapped every numbering family onto the final seven-phase roadmap, and recorded an open-PR snapshot.
- **r3 (2026-09-20, PR#19 + the v3.10.0 release commit):** recorded the implemented Nagar Wave 1 core as an accepted decision, moved the release baseline to `v3.10.0`, corrected the Phase 6 “implementation has not started” status, added the PR#18/PR#19 rows to the PR snapshot, marked the D1–D4 bundle as merged (`d9f5cf9`), and locked the Celery/Redis scan result into the record.
- **r4 (2026-09-20, PR#21 + the Wave 2 slideshow pack):** recorded Nagar Wave 2 — the capability-pack substrate and the slideshow pack — as an accepted and implemented decision, including the “evidence above the bus, pure handlers inside it” split, the additive state extension (`Project.assets` / `Clip.effects` / `AssetRecord` / `EffectLayerRef`), the level assignments of the five new operations, and the dependency verdicts (librosa deferred, Real-ESRGAN deferred, hosted image *generation* left out of the render path).
- **r5 (2026-09-20, PR#23):** recorded Nagar Wave 2c — the render lane (pure `RenderIR` → filtergraph → argv, one FFmpeg process, staging publish, measured evidence) — as an accepted and implemented decision with its rejected alternatives (agent-authored filtergraphs, `-y` against the destination, trusting the plan's duration, a second `ffprobe` binary, encoding inside a handler, a Python video library).
- **r7 (2026-09-21, repo-hygiene pass — owner-directed, session `arena/01a0c484`):** release baseline moved to `v3.13.0` (the merged P0 week-1 security batch — README already described its behavior as v3.13.0 while VERSION/pyproject still said 3.12.0); docs reorganized without content loss (`docs/audits/`, `docs/history/`, `docs/ops/`, `docs/README.md` index); the broken root `termux_install.sh` removed and `scripts/termux_install.sh` repaired (canonical `nexus run-bot` entrypoint); PR #33 closed as superseded (security scope already delivered by merged PR #34; feature-wiring scope double-claims agent B's active lease — evidence: `mergeable=CONFLICTING`, head checks green but base-diverged), then **reopened the same day** when the `ci-gates-steward` board (15:21Z) re-designated it as the task-110 vehicle; 28 merged/closed remote branches deleted with per-branch dispositions below.
- **r6 (2026-09-20, v3.11.0 housekeeping PR):** moved the release baseline to `v3.11.0`; recorded two owner decisions — *image generation behind an adapter (Pollinations by default, Gemini opt-in)*, which resolves the open question left by Wave 2 item 7, and *Wave 2.5 (Telegram surface for the slideshow pack) precedes Wave 3*; corrected the Phase 6 status text to Waves 1–2c merged; updated the PR snapshot (PR#23 merged as `ebe995a`, PR#1/PR#2 closed); noted that the lifecycle PR1/PR2/PR3 line has been on `main` since PR#7 (`acdbcb7`, v3.6.0) — the roadmap file had still called it unmerged.
- **r8 (2026-09-24, P0 stabilization day):** D-0010 legacy `/creative/*` HTTP lane = keep+harden (strictly harden-edged) on a deprecation track gated on open PR#58's SSRF scope, never a competitor pipeline; D-0011 `/edit` `/caption` `/grade` wired through the canonical chain with message-anchored idempotency, the bogus `mapper` handler key removed, honest op matrix (`lut`/`burnin` refused, not faked), all replies through the i18n catalog; D-0012 backup success must be measured and round-trip-verified, never asserted — plus the r8 coordination facts (task-106 superseded into task-166, task-164 narrowed to owner-secrets, docs number-resync against measured values: 57 registered ops).

This document is the single reference point for architectural decisions in this repository. A new decision must be appended here with its date, status, rationale, rejected alternatives, and repository evidence. Existing historical documents remain useful as detailed records, but this log is authoritative when summaries differ.

## How to Read This Log

A **decision** records an intentional choice, not merely an implementation detail. A decision may be superseded without being erased. When a later decision changes an earlier one, the newer entry must name the earlier entry and explain the transition. Dates below are repository dates from commit history or the owner-directed decision date recorded in the repository; they are not claims about an unrecorded conversational session.

## Phase History

### Phase 0 — Control plane and security foundation

**Period:** 2026-09-17 and earlier.  
**Status:** Complete on `main`.

Phase 0 established the control-plane foundation, repaired broken commands, applied baseline security fixes, and restored the quality gates. The project retained a modular application structure and treated runtime contracts, configuration boundaries, and testable operations as first-class concerns.

The phase also established the principle that operational behavior must be explicit and testable. Background execution, persistence, and destructive operations were not allowed to become hidden startup side effects.

### Phase 1 — Core product foundation

**Period:** 2026-08-31 onward.  
**Status:** Historical baseline; later phases build on it.

Phase 1 created the control-plane foundation, storage model, bot command surface, and initial application architecture. It provided the product substrate on which later LLM, lifecycle, deployment, and storage work was layered.

### Phase 2 — Provider-agnostic local LLM direction

**Period:** 2026-08-31 onward.  
**Status:** Proposed/partially represented by the still-open `feat/phase2-local-llm` branch; not treated as a reason to destabilize the mainline architecture.

The project kept a provider-agnostic LLM seam so that model providers could change without rewriting product flows. This did not authorize speculative infrastructure or a second product architecture.

### Phase 3 — Multi-provider routing and scale-to-zero deployment

**Date:** 2026-09-19.  
**Status:** Complete on `main`.

The project adopted multi-provider routing through `litellm.Router` and added a Telegram webhook run mode suitable for scale-to-zero deployment. The deployment decision explicitly accepted cold-start trade-offs and treated durable external storage as necessary for state that cannot live on ephemeral local disk.

### Phase 4 — Schema management and PostgreSQL/Neon support

**Period:** 2026-09-17 to 2026-09-19.  
**Status:** The schema-management work was merged through the Phase D/PR6 line; later lifecycle work continued on a separate branch lineage before Phase 5.

The project adopted Alembic-based schema management, PostgreSQL/Neon support, adoption checks, and fail-fast behavior on schema drift. Legacy SQLite adoption remained an explicit compatibility path rather than a reason to replay an initial migration over an existing database.

### Phase 5 — Durable storage and lifecycle operations

**Date:** 2026-09-20 baseline at `main` `994a509`, released as `v3.9.0`.  
**Status:** Complete on `main` before the Nagar integration.

Phase 5 added the Cloudflare R2 blob tier and scheduled maintenance. The lifecycle work established backend-aware checkpoint inspection and reconciliation, a PostgreSQL lifecycle index for the Neon path, golden fingerprints, kill-switch behavior, retention guards, and adversarial tests. The PostgreSQL lifecycle index was selected over a sidecar for the serverless path because local files are ephemeral there.

The post-Phase-5 mainline then accepted the modular-monolith foundation and the Nagar design through PR#13. The resulting `main` head is `db1ba541717dbc0ae4c26bf6e835881be6d015d1`.

### Phase 6 — Nagar architecture

**Date:** 2026-09-20.  
**Status:** Architecture accepted; **Wave 1 (“Green Cockpit” core) implemented and merged** through PR#19 (`ac6c25b`, released as v3.10.0); **Wave 2a/2b/2c (pack substrate, slideshow pack, render lane) implemented and merged** through PR#21 (`865780e`), PR#22 (`aa7b2f4`) and PR#23 (`ebe995a`, released as v3.11.0). Ten operations exist (five Wave 1, five slideshow). None is reachable from the Telegram bot yet — closing that gap is Wave 2.5 (see “Wave 2.5 — the Telegram surface for the slideshow pack precedes Wave 3”). The remaining 60 TDD operations are not implemented.

`docs/NAGAR_70_OPERATIONS_TDD.md` is accepted as the formal Phase 6 architecture document. Acceptance is limited to the design baseline. It does not authorize implementation work until the relevant operation contracts, ownership boundaries, and execution gates are separately approved.

The Nagar choice is based on three architectural properties. Commands are typed rather than simulated UI clicks. Content is stored and transported through content-addressed packs. Execution is divided into three explicit paths: **Preview**, **Analysis**, and **Master**. These choices make intent, reproducibility, and resource cost visible before an operation is executed.

This record was updated when Wave 1 landed: the five Wave 1 operations and their substrate are implemented (see “Nagar Wave 1 — Green Cockpit core implemented from the TDD baseline” under Accepted Architectural Decisions). The other 65 operations remain unimplemented, and each still requires its own contract, ownership boundary and decision before implementation.

## Rejected or Retired Decisions

### Multi-cloud “Leviathan” architecture

**Status:** Rejected.  
**Reason:** The proposed multi-cloud architecture introduced operational and consistency complexity without a demonstrated product consumer. The project instead keeps provider seams where they have a real consumer and avoids distributing the runtime merely to signal scale.

### Redis/BullMQ/Celery as a mandatory job system

**Status:** Rejected and superseded by the modular-monolith decision.  
**Reason:** An always-on external broker would violate the project’s modular-monolith and scale-to-zero constraints. It would add deployment, synchronization, and failure modes before the product had a requirement that justified them. Durable job hand-off is therefore owned by the application through `JobQueuePort` and the in-process SQLite adapter.

The rejection does not deny that a distributed queue could be appropriate in a future phase. Such a change would require a new decision, an explicit consumer, and updated port and operational contracts.

### Standalone `security/crypto.py` module

**Status:** Rejected/retired.  
**Reason:** The module had no real consumer. Keeping security code without a caller creates false assurance, increases maintenance surface, and encourages configuration that is not exercised. Security mechanisms belong at the boundary where the protected data or action is actually handled.

### Early pgvector adoption

**Status:** Rejected/retired.  
**Reason:** pgvector was considered premature before a real RAG consumer, query contract, and operational requirement existed. A storage extension without a product consumer would increase migration and deployment cost while leaving the application behavior unchanged.

### Cognee as the RAG/memory framework

**Status:** Rejected (never implemented).  
**Reason:** Cognee was evaluated as a replacement for the in-repo RAG stack, but no replacement was ever implemented: as of this revision the vector store remains `chromadb` and no pgvector (or Cognee) substitution has landed. Adopting an external memory framework without an implemented consumer and a query contract would have added a heavy dependency and an unowned data path. Revisiting it requires a new decision with a real consumer, an ingestion/query contract, and a migration plan for existing documents.

### Manual Neon keep-alive traffic

**Status:** Rejected.  
**Reason:** Sending synthetic "keep-alive" requests to prevent the Neon serverless instance from autosuspending was rejected: it masks the platform's actual behaviour, burns resources to fight the pricing model instead of designing for it, and adds a moving part that fails silently. The accepted alternative is the Phase 3 scale-to-zero posture — accept cold starts, keep all durable state in the database, and rely on `nexus migrate` / the Neon runbook for deterministic wake-and-repair on cold boot.

### “Nexus World” / Qwen rewrite

**Status:** Rejected.  
**Reason:** The proposal (“Nexus World”) combined a 3-D world model with alleged “quantum decision-making algorithms” for agent behaviour. The quantum claims were baseless — no algorithm, benchmark, or peer-reviewed basis was ever produced — and the 3-D layer had no product consumer. On top of that it demanded a broad rewrite without a justified product outcome, migration plan, or bounded scope. The project retained incremental, contract-first evolution instead.

## Accepted Architectural Decisions

### Modular monolith and in-process job queue

**Date:** 2026-09-20; accepted and merged through PR#13.  
**Evidence:** `R-001`, `R-026`; `docs/architecture/PORTS.md`; `src/nexus_ai_agent/adapters/in_process_job_queue.py`.

Background jobs remain inside the application process. The queue is durable through an application-owned SQLite table, while execution is scheduled on the current asyncio event loop. There is no mandatory Celery, Redis, or worker service. The `JobQueuePort` keeps the application contract explicit and leaves room for a future adapter without forcing distributed infrastructure today.

### Nagar as the formal Phase 6 architecture

**Date:** 2026-09-20; accepted by merge of PR#13.  
**Evidence:** `docs/NAGAR_70_OPERATIONS_TDD.md`.

Nagar is accepted as the Phase 6 design baseline because it makes operation intent explicit through typed commands, avoids brittle UI-click simulation, and uses content-addressed packs for reproducible content movement. Its three execution paths provide a controlled separation between cheap previews, analytical work, and authoritative master execution.

### Nagar Wave 1 — “Green Cockpit” core implemented from the TDD baseline

**Date:** 2026-09-20; implemented through PR#19 (merged as `ac6c25b`).  
**Status:** Accepted **and implemented**. This entry does not authorize any of the remaining operations.  
**Problem:** The Phase 6 architecture was accepted as a *design*, but no execution surface existed: there was no typed command envelope, no capability registry, no reference semantics, no atomic apply path and no undo contract. Implementing any single operation first would have meant inventing that substrate implicitly, one operation at a time.
**Decision:** Wave 1 implements exactly the substrate the TDD asks for first (executive summary, execution order step 1: “تثبیت Command Envelope، timecode_us، Registry، State revision و Undo برای Green Cockpit”) and nothing more — the models, the `Domain > Capability > OperationSpec` registry, the semantic reference resolver and the atomic command bus in `src/nexus_ai_agent/creative/studio/`, exposed through exactly five operations: `media.play`, `media.pause` (level A), `timeline.mark`, `timeline.split_at_playhead` (level B, non-destructive — the source `MediaRef` is never mutated), and `system.undo` (level A). The implementation follows `docs/NAGAR_70_OPERATIONS_TDD.md` directly; no operation outside the document was invented, and the catalog is pinned by an architecture gate.
**Consequences recorded for the future (in-code decisions that must not silently drift):**
- `state_hash` is **content-derived** — canonical JSON over `project_id`, `name` and `timeline` — and **excludes** `state_revision`, which stays monotonic. Undo therefore restores the exact previous hash, and revision+hash preconditions stay sound across undo cycles.
- References (`"اینجا"`, «۵ ثانیه قبل», absolute/relative/start/end, Arabic-Indic digits) are pinned **once at command receipt** with `captured_at_command=True`, before validation and application. No later stage may re-interpret an expression against a moved playhead.
- `timecode_us` is authoritative; `frame_number` is a derived convenience only (VFR is a documented black swan).
- Handlers are pure `(project, context) -> outcome` and never touch I/O. Atomicity depends on that purity: a failing handler leaves central state exactly as it was.
- The studio package stays UI-free and stdlib+pydantic-only; both properties are machine-enforced by `tests/architecture/test_nagar_studio_isolation.py`.
- `system.undo` skips its own `system.undo` records (classic NLE semantics), which is what makes repeated undo meaningful rather than self-consuming.
**Rejected alternatives:** re-interpreting references at apply time (non-replayable and racy); inverse-patch undo (a later optimization that must not change the snapshot-based contract); a separate error module (the typed error tree belongs beside the models it protects); UI-click simulation as the operation surface (already rejected in “Nagar as the formal Phase 6 architecture”).
**Impact on contracts:** the `nagar.command.v1` envelope, the A/B/C/D permission ladder, `EditTransaction` / `CommandResult` and `compute_state_hash()` are now live contracts. Any later change to them requires a new entry that names this one.
**Verification:** `tests/unit/test_nagar_wave1_green_cockpit.py` (33 tests — for every registered operation: command validates, applies on in-memory state, and undo restores the previous state hash) and `tests/architecture/test_nagar_studio_isolation.py` (3 gates — dependency allow-list, package boundary, exact Wave 1 catalog). Fresh gates on the PR head (`848a40a`): `ruff` (252 files), `mypy src` (159 files), `pytest -m "not slow"` = **460 passed / 20 skipped**; CI `test` (7m44s) and `migrate-postgres` (5m55s) green.
**Celery/Redis lock:** a repository-wide scan at this revision found **no live Celery or Redis reference** — no import under `src/`, no dependency in `pyproject.toml`, and no document claiming Celery-based background processing. The remaining mentions are intentional records only: the guard test `tests/architecture/test_modular_monolith.py`, the rejection entry and the modular-monolith decision in this log, `docs/architecture/PORTS.md` (“Redis/Celery are forbidden in the Modular Monolith”), and a historical v1.x changelog note. The modular-monolith decision stands unchanged.

### Nagar Wave 2 — packs are data, commands carry evidence

**Date:** 2026-09-20; substrate merged through PR#21 (`96ad952`, merge `865780e`), slideshow pack implemented on the Wave 2 branch.  
**Status:** Accepted **and implemented** for the eighth pack (`nexus.slideshow.compose`). This entry does not authorize any *other* pack or capability.  
**Problem:** The TDD declares packs to be declarative (“Manifest فقط Executorهای declarative، مدل‌ها، hash، مجوزها و محدودیت منابع را معرفی می‌کند. هیچ `post_install`، shell command یا entrypoint آزاد مجاز نیست”), but nothing enforced that, and the Wave 1 command bus requires **pure** handlers — which is incompatible with the obvious implementation of “scan these files, analyse these images, render this master” if that work is placed inside a handler.
**Decision:** Wave 2 splits the feature along one hard line.

1. **A pack is data plus registered adapter ids.** `src/nexus_ai_agent/creative/packs/` holds a strict, immutable `nexus.capability-pack.v1` manifest, a verifier that reports *every* finding, and a registry that refuses to let an external pack introduce an operation the runtime does not already know. Builtin packs register with *pending* capabilities and can only be activated once the live runtime knows all of them.
2. **I/O happens above the bus; evidence is pinned into the command.** Probing, hashing, beat detection, image analysis and (Wave 2c) encoding live in the `creative/slideshow/` adapter. The commands carry content-addressed `AssetEvidence`, `BeatGrid` and `ImageScore` records, so handlers stay pure `(project, context) -> outcome`, atomicity survives, and a command can be replayed verbatim.
3. **The pack's five operations are pure and explicitly levelled.** `slideshow.scan_assets` (B, registers hashed media and refuses a changed hash for a known id), `slideshow.score_images` (A, normalizes a score sheet and derives the narrative order — a hosted model may propose an order, the pack assigns roles), `slideshow.suggest_tone` (A, deterministic tempo/image-count heuristic), `slideshow.compose` (B, **one** atomic transaction that builds tracks, clips and effect layers), `slideshow.render` (C, records the produced master as a derived asset and requires `confirmed=true`).
4. **Canonical state grows additively.** `Project.assets`, `Clip.effects`, `Track.effects`, `AssetRecord`, `EffectLayerRef` (parameters **and** their content hash, `reversible=True`) and `compute_parameters_hash()` are added; `state_hash` now covers the asset registry. Wave 1 states remain valid, and the Wave 1 catalog (`build_wave1_registry()`) stays frozen — Wave 2 composes a new registry around it.
5. **Tone is data.** The shipped library is one JSON file with 14 templates (12 primary + 2 alternates) holding rhythm/transition/motion/color/audio/render parameters. It contains no filtergraph, no executable key, and adds no dependency (JSON, not YAML), which a gate enforces.
6. **Heavy dependencies stay out.** `librosa` 1.0.0 requires Python ≥ 3.12 while the project supports ≥ 3.10 and brings numba/llvmlite/scikit-learn; `realesrgan` 0.3.0 (2022) needs torch + basicsr + facexlib + gfpgan, and the ncnn route needs libomp/libvulkan. Neither enters the core: beat detection is a numpy energy-flux estimator behind the same `BeatGrid` contract, upscaling stays a *separate, optional* stage (never in the main render path), and `ffmpeg` stays the single declared external binary.
7. **Hosted analysis is fail-closed.** Gemini (or any hosted model) is used for *analysis* only, only when `NEXUS_SLIDESHOW_ALLOW_IMAGE_UPLOAD=1` is explicitly set, and only with downscaled (≤768 px) copies. Image *generation* is not part of this pack: current Google pricing lists **no free tier** for image-output models, so the free image path (Pollinations) stays the default and any change remains an owner decision.
**Rejected alternatives:** an "entrypoint"/hook manifest (arbitrary code by another name); letting a handler do the I/O (breaks atomicity — the bus could not roll back a half-written file); trusting a hosted model's ordering and roles (non-replayable and unauditable: the model proposes, the pack decides); storing effect parameters only as hashes (a render could not be reproduced from the state hash alone); YAML templates (a new dependency for a data file); librosa/Real-ESRGAN in the core (dependency weight and Python-version pressure on a light-studio project).
**Impact on contracts:** manifest schema + verification codes, `PackRegistry` activation semantics, the pack↔registry coherence requirement (a gate asserts the manifest's capabilities are exactly the operations the pack registers), the additive state models and the extended `state_hash` payload. Any change to these requires a new entry naming this one.
**Verification:** `tests/unit/test_pack_manifest_verify.py` (37 tests), `tests/unit/test_slideshow_pack.py`, `tests/unit/test_slideshow_engine.py` (real generated JPEG/WAV fixtures; only the hosted transport is mocked), `tests/unit/test_slideshow_cli.py`, `tests/architecture/test_pack_manifest_is_data_only.py` and `tests/architecture/test_slideshow_adapter_boundary.py`. Local gates on the Wave 2b tree: `ruff check`/`ruff format --check` (274 files), `mypy src` (172 files), `pytest -m "not slow"` = **602 passed / 20 skipped**.

### Nagar Wave 2c — the render lane: an IR, one encoder, measured evidence

**Date:** 2026-09-20; the slideshow pack merged through PR#22 (`a0c23e4`, merge `aa7b2f4`), the render lane implemented on the Wave 2c branch.  
**Status:** Accepted **and implemented**. This entry extends “Nagar Wave 2 — packs are data, commands carry evidence” and authorizes no other external binary or pack.  
**Problem:** Wave 2b produced a plan and a level-C operation that *records* a master, but nothing rendered one. The TDD's master lane requires that (a) the agent never writes FFmpeg syntax — the filtergraph is derived from a render IR, (b) a source or existing artifact is never overwritten (no `-y` against a user path), and (c) the result is reproducible from canonical state. Rendering also cannot live in the pure layers: `creative/studio/` and `creative/packs/` may not import `subprocess`, `shutil` or `os` at all, and an architecture gate enforces that.
**Decision:** the render lane is staged, and only its last step is impure.

1. **Parameters → IR (pure).** `render_ir_from_plan` maps the composed plan into a small dataclass IR: per-shot duration, camera move, grade, transition, audio fades, output profile. It contains no FFmpeg syntax.
2. **IR → filtergraph → argv (pure).** `build_filtergraph` and `build_command` derive both from the IR, so the exact encoder invocation is asserted in unit tests without running anything — and the command that runs is the command that was asserted.
3. **Crossfades are centred on the cut.** `xfade` consumes each transition from the tail of one clip and the head of the next, so both neighbours are authored half a transition longer than their slot. The extensions sum to exactly the transitions consumed (the master keeps the duration the pack promised), and every fade is centred on the boundary the planner chose — which is what lets a beat-aligned plan survive the render.
4. **One process, no shell, staging publish.** `encode` resolves exactly one binary (explicit override → `NEXUS_FFMPEG_BIN` → `PATH` → the `imageio-ffmpeg` wheel), runs `subprocess.run` with an argv list and no `shell=`, writes `.<name>.part.<ext>` and publishes by an atomic `Path.replace`. An existing destination is replaced only when the caller passes `overwrite=True`; a failure or timeout deletes the staging file and leaves nothing behind.
5. **Evidence is measured, not assumed.** `probe_video` reads duration, frame size and stream layout back out of the produced file *with the same allow-listed binary* (no second `ffprobe` dependency), and `render_from_files()` dispatches `slideshow.render` with `output_sha256`, the measured duration, the IR hash and the pre-render state hash. The encode happens *before* the command, so the bus stays pure and atomic and `system.undo` can take the record back without touching the file.
6. **The encoder stays a declared external binary.** `ffmpeg` remains the single binary the manifest names; `imageio-ffmpeg` is a dev/test extra (so the suite executes a genuine encode anywhere), never an application dependency, and no codec or container library enters the project.

**Rejected alternatives:** letting the agent or the CLI author filtergraph strings (unreviewable and unreproducible, and it would put syntax back into the pack); passing `-y` against the destination (a failed render could destroy an existing master); trusting the plan's duration instead of probing the produced file (a silently truncated render would be recorded as truth); adding `ffprobe` as a second required binary (a minimal install frequently lacks it); encoding inside the handler (breaks bus purity and atomicity); adding a Python video library such as `imageio`/`moviepy` (a dependency for a job one process already does correctly).
**Impact on contracts:** `RenderInput`'s evidence fields are now populated by measurement; the settings surface gains `NEXUS_FFMPEG_BIN` and `NEXUS_SLIDESHOW_RENDER_TIMEOUT`; the development extra gains `imageio-ffmpeg`; and `tests/architecture/test_slideshow_adapter_boundary.py` now pins that exactly one adapter module may spawn a process and may never use a shell. Any change to these requires a new entry naming this one.
**Verification:** `tests/unit/test_slideshow_render.py` (19 tests) — the pure IR/argv layer, typed failures (`FfmpegUnavailableError`, unusable resolution, missing media), overwrite refusal, staging cleanup, byte-identical re-encodes of the same IR, and **four genuine FFmpeg encodes**, including a 60-second master read back with `probe_video` and the CLI path. Local gates on the Wave 2c tree: `ruff check`/`ruff format --check` (276 files), `mypy src` (173 files), `pytest -m "not slow"` = **622 passed / 20 skipped**.

### Image generation behind an adapter — Pollinations by default, Gemini opt-in

**Date:** 2026-09-20; owner decision recorded with the v3.11.0 housekeeping PR.  
**Status:** Accepted — **not yet implemented**. This entry resolves the question that “Nagar Wave 2 — packs are data, commands carry evidence” (item 7) explicitly left to the owner. It authorizes the *shape* of the generation seam; the generation pack itself (Wave 3) still needs its own contract and lands only after Wave 2.5.  
**Problem:** Two generation paths exist or are wanted: the free, key-less Pollinations endpoint that the bot has used since v2.0.0 (`features/image_gen.py`), and Google's image-output models, for which the official price list shows **no free tier** (Nano Banana 2 Lite ≈ $0.034 per 1K image, Nano Banana 2 ≈ $0.067, Pro ≈ $0.134; verified 2026-09-20). Hard-wiring either one is wrong: Pollinations alone is a single point of failure with anonymous rate caps, throttling under load and possible watermarks; Gemini alone would make the free product cost money per image. Local diffusion models are not an option on the deployment targets (Koyeb scale-to-zero web instance, Termux/Android, a GPU-less Docker image), and the Wave 2 dependency verdict already keeps torch out of the core.  
**Decision:**

1. **Image generation is an adapter behind one contract.** A single provider-neutral seam (prompt + size/style/seed in, bytes + provenance — provider id, model id, seed, request digest — out) with one adapter per provider. The Nagar operation that consumes it (Wave 3) sees the contract, never a provider.
2. **Pollinations is the default adapter.** It needs no key and costs nothing, so the shipped configuration keeps the product free. Its limits (rate caps, throttling, watermark on the anonymous tier) are surfaced as typed failures and user-facing messages, not hidden retries.
3. **Gemini is opt-in behind a key and an explicit setting.** It activates only when both an API key and an explicit provider selection are present (the same fail-closed pattern as `NEXUS_SLIDESHOW_ANALYSIS_PROVIDER` / `NEXUS_SLIDESHOW_ALLOW_IMAGE_UPLOAD`); absent either, the code path is unreachable and no billable request can be made by accident.
4. **The core stays free.** No adapter may become a required dependency of the free path; a paid provider is never a fallback that engages silently when the free one fails — falling back to a billable provider is itself an explicit owner/user choice.
5. **Egress is declared truthfully.** A generation pack sends the *prompt* (never user media) to a provider, so its manifest declares that egress as a distinct permission rather than reusing `egress_media_optin`. Results are recorded as derived assets with provider/model/seed provenance (TDD black swan 6: reproducibility across model changes must be explicit).

**Rejected alternatives:** a local diffusion model (no GPU on any deployment target; torch already rejected for the core); Gemini as the default (no free tier for image output — the free product would start costing money per image); Pollinations hard-wired with no seam (single point of failure, and the second provider would arrive as a fork of the first); silent paid fallback (turns an outage into a bill); putting generation inside the slideshow pack (a different capability with different egress — it gets its own pack and decision).  
**Impact on contracts:** none yet on shipped code. When Wave 3 lands it must: add the seam under `application/ports/` next to `LLMPort`, register the pack with a prompt-egress permission, and route both adapters through the existing SSRF egress guard. Any change to this default/opt-in posture requires a new entry naming this one.  
**Verification plan:** contract tests executed against every adapter with the transport mocked (no network in the suite); a gate that the free configuration imports no paid client; an architecture test that the Gemini adapter is unreachable without both the key and the explicit selection.

### Wave 2.5 — the Telegram surface for the slideshow pack precedes Wave 3

**Date:** 2026-09-20; owner decision recorded with the v3.11.0 housekeeping PR.  
**Status:** Accepted — **implementation pending on its own branch and PR** (this entry is appended before implementation, as the lifecycle rule requires).  
**Problem:** After Waves 1–2c the studio has ten operations, a real render lane and a CLI, but no user can reach any of it: the bot and the API know nothing about Nagar. Starting Wave 3 (upscale + generation) would add capability to a surface nobody can use. The v3.11.0 verification report made this explicit and the owner agreed: **build the surface before the next feature.**  
**Decision:** Wave 2.5 connects the existing slideshow lane to the Telegram bot through the infrastructure that already exists, adding no new heavy dependency and no new external binary:

1. **A bot command** (`/slideshow`) collects the user's inputs (uploaded images, an optional caption/prompt) and validates them **before** anything is enqueued.
2. **Hard resource limits are enforced in the handler**, not in the worker: at most **5 images** and at most **30 seconds** of output per request (DoS containment — FFmpeg time is bounded by the smallest of these and by `NEXUS_SLIDESHOW_RENDER_TIMEOUT`). Anything larger is refused with a plain message.
3. **The request is a job, not an inline call.** The handler calls `JobQueuePort.enqueue("slideshow_render", payload)` on the durable in-process queue (D1–D4 line, PR#18); the Telegram handler returns immediately. No Celery/Redis — the modular-monolith decision stands.
4. **The in-process worker runs the Wave 2c lane unchanged** (`render_from_files()` → `RenderIR` → one FFmpeg process → measured evidence → `slideshow.render` through the bus). The worker does not grow a second render path.
5. **Completion uses the D4 completion hook**: on success the produced `master.mp4` is sent back to the requesting chat; on failure the typed error (`FfmpegUnavailableError`, unusable resolution, missing media, timeout, encoder failure) is rendered as a short user-facing message — **never a stack trace** — and the failure is logged with the redaction rules already in place.
6. **Verification is an end-to-end test**: at least one integration test drives enqueue → process → notify with the encoder's output file mocked, plus unit coverage of the limits and the error mapping.

**Rejected alternatives:** rendering inline in the handler (blocks the bot's event loop and Telegram's delivery window; a long encode would time out the update); a dedicated worker service (Celery/Redis — already rejected); exposing every studio operation to chat at once (a chat surface for a 70-operation catalog needs the agent/registry work that is not designed yet; one bounded command is the right first surface); skipping limits because the queue serialises work (the queue bounds *concurrency*, not *cost per job*).  
**Impact on contracts:** a new job type `slideshow_render` in the job registry; a new bot command; settings for the limits if they are made configurable. No change to the bus, the pack manifest, the render IR or `JobQueuePort`. Any deviation (a new binary, a new dependency, an inline render) requires a new entry naming this one.

### Wave 2.5 revision r7 — surface deltas recorded before implementation

**Date:** 2026-09-21; owner architecture directive of Wave 2.5 (Telegram ↔ Nagar wiring).  
**Status:** Accepted — names the "Wave 2.5 — the Telegram surface for the slideshow pack" entry above and revises it only where implementation contact demanded precision; everything not listed here stands unchanged.  
**Problem:** Drafting the wave against the live tree surfaced five points where the recorded decision needed either a correction of terminology or an explicit deviation: the pack's frozen target-duration set excludes 30 s; the proposed file layout (`bot/handlers/slideshow_handler.py`) would collide with the existing `bot/handlers.py` module; the architect's `render_slideshow(render_ir, output_path)` does not exist as such; `finally`-cleanup inside the worker would delete the master before the D4 notifier could send it; and the free-text argument of `/slideshow` has no consumer in the render lane (the plan takes images and a template, not a prompt).  
**Decision:**

1. **The pack gains one target duration: `30_000_000` µs is added to `TARGET_DURATIONS_US` and `TargetDurationUS`** (models.py; the set stays closed, CLI `--duration-min 1/2/5` untouched). The Wave 2.5 "at most 30 seconds" ceiling is only enforceable if 30 s is a representable plan target; every shot math (`MIN_SHOT_US`, image counts 1–5) validates at 30 s without touching the pure planner.
2. **File layout honors the repo, not the sketch:** `bot/slideshow_handlers.py` (PTB glue) and `bot/slideshow.py` (pure limits/sessions/message-mapping, no Telegram imports) beside the other `*_handlers.py` modules — a `bot/handlers/` package directory would shadow `bot/handlers.py` and break every existing import; a collision of two same-named modules is an import-system bug, not a naming preference.
3. **The worker calls the real entry point:** `creative/slideshow/worker_adapter.py` awaits `asyncio.to_thread(render_from_files, PlanningRequest(...), output_path=…)` — the same pure planning+encode function the CLI uses (Wave 2c lane unchanged, one FFmpeg process, evidence back through the bus). "render_slideshow" in the directive is read as this function; no second render path is born.
4. **Cleanup ownership follows delivery:** the worker `finally` deletes the Telegram-downloaded input images and a failed/partial output; a **successful** `master.mp4` is owned by the notifier, which removes it (with its workspace directory) after `send_document`, and a 24-hour age sweep inside the worker prunes what a crash orphaned. Deleting in the worker's `finally` unconditionally would remove the file before the completion hook runs.
5. **Free text is a name, not a prompt:** the `/slideshow` caption becomes `PlanningRequest.project_name` (≤ 60 chars, allow-listed characters), which is where such labels live in canonical state; the render lane has no prompt field and none is invented.
6. **The queue payload is a trust boundary:** the handler enforces the limits (≤ 5 images, 30 s fixed, resolution `1280x720`) as UX, and the worker's payload model re-validates the same envelope with `extra="ignore"` and typed failures — so a hand-crafted or CLI-drained payload cannot exceed the bot ceiling. The provider stays `NEXUS_SLIDESHOW_ANALYSIS_PROVIDER` (default `local`) and uploads stay gated by `NEXUS_SLIDESHOW_ALLOW_IMAGE_UPLOAD`; the bot flow cannot make a billable or egressing call.
7. **Failure vocabulary:** the worker returns `{"success": false, "error_code": …}` with codes `ffmpeg_unavailable | render_failed | unusable_image | invalid_request | internal`; `bot/slideshow.py` maps each to a short Persian user message (the architect's example: "❌ خطا در ساخت ویدیو: فرمت تصویر پشتیبانی نمی‌شود"). No path, traceback or raw exception text crosses into the chat.

**Rejected alternatives:** a `bot/handlers/` package (breaks `nexus_ai_agent.bot.handlers` resolution — see 2); widening the Literal to arbitrary ints (freezes the pack's owner-approved-duration invariant away); letting the payload choose duration above 30 s "for flexibility" (contradicts the recorded DoS limit); passing the caption as `prompt` into the analysis path (no such input exists; inventing one mutates the pack contract for cosmetics); deleting outputs in the worker `finally` (drops the product before delivery).  
**Impact on contracts:** `TARGET_DURATIONS_US`/`TargetDurationUS` grow additively (plans valid before stay valid); `nexus.worker.default_job_handlers()` gains `"slideshow_render"`; the job payload shape is defined by `SlideshowRenderPayload` in the adapter; `build_handlers()` gains a `CommandHandler("slideshow", …)` and a `MessageHandler(filters.PHOTO, …)`; `_build_job_completion_notifier` gains one `slideshow_render` branch. `JobQueuePort`, the bus, the manifest and the render IR are untouched.  
**Verification plan:** unit — 30 s plans compose and tile exactly (fixture images via `tests/unit/slideshow_media.py`), the session store caps/dedupes, the mapper covers every code; integration — real `InProcessJobQueue` round-trip with `render_from_files` mocked (success delivers and cleans, typed failures map to codes and clean); notifier — `telegram` stubbed as a `ModuleType` (the pattern in `test_in_process_job_queue.py`) asserting document-on-success / text-on-failure and silence without `chat_id`.

### Typed commands instead of UI clicks

**Status:** Accepted for Nagar.  
**Reason:** A typed command is inspectable, serializable, testable, and suitable for deterministic replay. UI clicks are presentation events and do not provide a stable operation contract.

### Content-addressed packs

**Status:** Accepted for Nagar.  
**Reason:** Content addressing makes identity independent from a mutable filename or UI location. It supports deduplication, integrity checks, reproducibility, and safe hand-off between Preview, Analysis, and Master paths.

### Preview, Analysis, and Master execution paths

**Status:** Accepted for Nagar.  
**Reason:** The paths separate intent validation from expensive analysis and from authoritative execution. Preview can be safe and inexpensive. Analysis can compute plans and diagnostics. Master is the only path allowed to perform the final operation under its explicit contract.

### Neon durability over local sidecars for the PostgreSQL path

**Date:** 2026-09-19.  
**Status:** Accepted for the PostgreSQL lifecycle index.  
**Reason:** Serverless instances do not provide durable local files across restarts. The lifecycle index therefore belongs in the same PostgreSQL database on the Neon path. SQLite continues to use its local sidecar where that is the selected backend.

### Human-triggered golden updates

**Status:** Accepted.  
**Reason:** Schema and checkpoint fingerprints describe compatibility boundaries. Updating them must be an explicit human action rather than an incidental test or startup side effect.

### Fail-fast drift handling

**Status:** Accepted.  
**Reason:** A database that does not match the expected schema must not be silently stamped or repaired. Fail-fast behavior makes the migration decision visible and prevents silent data-contract divergence.

## Branch and Numbering Disposition

### Zombie / abandoned branches

The following branches are historical, open, or abandoned proposals and are not part of the active mainline decision path. **They must be deleted manually on GitHub by the owner** — deletion is a remote administrative action and is deliberately not performed by documentation changes. **Update (r7, 2026-09-21):** the owner directed the repo-hygiene session to perform this deletion; all four are no longer present on the remote:

| Branch | Status | Reason / evidence |
|---|---|---|
| `trae/agent-FdzTxJ` | Rejected — duplicate | A parallel “Creative Studio MVP implementation” (`843f004`) stacked on the PR#9 merge point, superseded by the creative-studio line that actually merged through PR#10 (`62e25ce`). Never merged. |
| `feat/phase1-control-plane` | Abandoned — origin unclear | The “phase one control plane foundation” proposal (open as PR#1 historically). Never merged into `main`; its rate-limiter/control-plane ideas survive only as history. Treat as unowned. |
| `feat/phase2-local-llm` | Abandoned — stacked on an unmerged base | “Provider-agnostic local LLM engine” built **on top of the unmerged `feat/phase1-control-plane`**, so it can never merge cleanly. The underlying need (a provider seam) was satisfied properly by litellm routing in v3.7.0 (Phase 3). |
| `circleci-project-setup` | Irrelevant — CI platform cut | Only adds `.circleci/config.yml` (commits `265d6a0`, `2818d9d`). `.circleci/` does not exist on `main`; the project standardizes on GitHub Actions (`.github/workflows/ci.yml`, `maintenance.yml`). |

### Remote branch deletion — dispositions (r7, 2026-09-21)

Executed by the `repo-hygiene-2026-09-21` session on owner instruction. Each deletion was
verified against the GitHub compare API (`main...<head>`) before deletion. Preserved branches:
`main`, active session branches (`arena/01a0c316` — agent A active lease; `arena/01a0c34d` —
agent B, PR #32 open; `arena/01a0c3aa` — PR #33 archive, kept despite closure; `arena/01a0c460`
— agent E active work; `arena/01a0c484` — hygiene session).

| Branch | Evidence | Disposition |
|---|---|---|
| `arena/01a0ac24` | PR#3 merged | deleted (behind main) |
| `arena/01a0ae59` | 1 unique commit: C1 Postgres support — delivered via PR#7 lineage | deleted (superseded) |
| `arena/01a0af6a` | PR#4 merged | deleted (behind main) |
| `arena/01a0b0bf` | PR#6 merged | deleted (behind main) |
| `arena/01a0b123` | fully behind main (session rescued via `arena/01a0b1e8`) | deleted (behind main) |
| `arena/01a0b1e8` | 1 unique commit: rescue merge of Stage-1 + partial PR2 — delivered via PR#7 lineage | deleted (superseded) |
| `arena/01a0b5d7` | PR#7 merged; unique commit is bookkeeping only | deleted (superseded) |
| `arena/01a0bace` | PR#8 merged | deleted (behind main) |
| `arena/01a0bb1d` | PR#9 merged | deleted (behind main) |
| `arena/01a0bb93` | PR#10 merged | deleted (behind main) |
| `arena/01a0bd16` | PR#11 merged | deleted (behind main) |
| `arena/01a0bd99` | fully behind main | deleted (behind main) |
| `arena/01a0beae` | PR#15 closed — superseded by `feat/d1-d4-clean-rebuild` (PR#18) | deleted (closed-superseded) |
| `arena/01a0bf3b` | 3 unique commits, self-documented "already superseded by the merged main line" (wave-1 duplicate) | deleted (superseded) |
| `arena/01a0c0eb` | PR#26 + PR#27 merged | deleted (behind main) |
| `arena/01a0c05a` | PR#24 merged | deleted (behind main) |
| `arena/01a0c099` | PR#25 merged | deleted (behind main) |
| `arena/01a0c286` | PR#28 merged | deleted (behind main) |
| `arena/01a0c2d5` | PR#29 merged | deleted (behind main) |
| `arena/01a0c2ec` | 4 unique commits, self-archived "superseded by PR#29" | deleted (self-archived) |
| `arena/01a0c36f` | PR#31 merged (squash `c41b1b0`); pre-squash wave commits | deleted (delivered via squash) |
| `arena/01a0c3a0` | PR#34 merged (squash `93cee5e`) | deleted (delivered via squash) |
| `arena/01a0c3ca` | PR#35 merged; unique commit is board bookkeeping | deleted (delivered via squash) |
| `chore/release-v3.10.0` | PR#20 merged | deleted (behind main) |
| `docs/decision-log-history` | PR#17 merged | deleted (behind main) |
| `feat/nagar-wave1-green-cockpit` | PR#19 merged | deleted (behind main) |
| `feat/security-hardening` | PR#16 merged | deleted (behind main) |
| `feat/wave2a-pack-substrate` | PR#21 merged | deleted (behind main) |

### Pull-request snapshot at this revision (2026-09-20)

- **PR#12** — “Return to the modular monolith … + decisions D1–D4”: **CLOSED 2026-09-20** — its orphaned-history branch (`arena/01a0bdda-…`, no merge base with `main`) was deleted with the owner's approval; the D1–D4 logic is re-built cleanly on the post-PR#16 mainline instead (see `feat/d1-d4-clean-rebuild`).
- **PR#15** — re-implementation of D1–D4 on the in-process `JobQueuePort` architecture (single commit on the post-PR#14 mainline); **superseded by `feat/d1-d4-clean-rebuild`**, which carries the same reviewed content onto the current `main` (disposition: close in favour of the rebuild).
- **PR#16** — security hardening (CORS allowlist, fail-closed HMAC endpoint auth, SSRF/DNS-rebinding egress guard, log redaction): **MERGED 2026-09-20** (`4e92371`).
- **PR#18** — D1–D4 re-implemented cleanly on the post-PR#16 mainline (`feat/d1-d4-clean-rebuild`): **MERGED 2026-09-20** (`d9f5cf9`).
- **PR#19** — Nagar Phase 6 Wave 1 “Green Cockpit” core (`feat/nagar-wave1-green-cockpit`, head `848a40a`): **MERGED 2026-09-20** (`ac6c25b`), CI green (`test` + `migrate-postgres`).
- **PR#20** — v3.10.0 release (`chore/release-v3.10.0`, head `bab27e1`): **MERGED 2026-09-20** (`c550420`); semver-minor bump with the rationale recorded in the changelog, `/version` de-hardcoded, pyproject version drift fixed.
- **PR#21** — Nagar Phase 6 Wave 2a, the capability-pack substrate (`feat/wave2a-pack-substrate`, head `96ad952`): **MERGED 2026-09-20** (`865780e`), CI green (`test` 7m30s, `migrate-postgres` 6m7s).
- **PR#22** — Nagar Phase 6 Wave 2b, the slideshow pack (`feat/wave2b-slideshow-planning`, head `a0c23e4`): **MERGED 2026-09-20** (`aa7b2f4`), CI green (`test` 7m10s, `migrate-postgres` 6m12s).
- **PR#23** — Nagar Phase 6 Wave 2c, the render lane (`feat/wave2c-slideshow-render`, head `934f70b`): **MERGED 2026-09-20** (`ebe995a`), CI green (`test` ×2, `migrate-postgres` ×2); head branch deleted.
- **PR#1 / PR#2** — the abandoned `feat/phase1-control-plane` and `feat/phase2-local-llm` proposals: **CLOSED** (unmerged; see the zombie-branch table).
- **v3.11.0 housekeeping PR** (opened from the session branch `arena/01a0c05a-nexus-ai-agent`, 2026-09-20): release lock-step, continuum refresh, roadmap rewrite, this revision (r6). Wave 2.5 follows on its own PR once this one is merged.
- **PR#33** — “v3.13.0 — deliver the P0 security code, wire the dead engines, fix 4 production bugs” (`arena/01a0c3aa`): **CLOSED 2026-09-21 as superseded** — its security scope landed through merged PR#34 (`93cee5e`), its head was `CONFLICTING` with `main`, and its feature-wiring portion overlaps agent B's active `feature-wiring` lease (PR#32). **REOPENED the same day** after the `ci-gates-steward` board (15:21Z, merged via PR#36) designated it the **task-110 vehicle** (OTIO round-trip validation + ConversationStorePort adapter + manifest-signature spike); disposition: keep open, rebase on post-PR#36 main before merge.
- **PR#34** — P0 week-1 security batch + feature-engine wiring: **MERGED 2026-09-21** (`93cee5e`, squashed).
- **PR#35** — task-101 lint rescue: **MERGED 2026-09-21**; `main` CI green (run 35613892356).

The repository contains several numbering systems from different workstreams. They must not be interpreted as one chronological sequence. The final roadmap is the **seven-phase plan** documented above: Phase 0 (control plane/security) → 1 (core product) → 2 (local-LLM direction) → 3 (multi-provider routing, scale-to-zero) → 4 (schema management, PostgreSQL/Neon) → 5 (durable storage, lifecycle, R2) → 6 (Nagar creative studio, design accepted).

| Identifier family | Historical meaning | Current disposition | Maps to the final roadmap |
|---|---|---|---|
| `C1–C4` | Checkpoint/lifecycle composition and contract steps (PR2 line: hooks/reconciler, inspect slice, O1 observability, adversarial hardening) | Historical records; superseded by the final lifecycle baseline where applicable | Phase 5 (lifecycle, released v3.6.0) |
| `D1–D10` | Phase D schema management, migration, adoption, and PostgreSQL decisions (D7 pgvector and D8 token-encryption portions were **reverted** for having no consumer) | Historical records; not the same as the PR#12 D1–D4 labels | Phase 4 (schema/PostgreSQL, released v3.5.0) |
| Old continuum `Phase D` (aka “Leviathan”) | Workstream name for the Alembic + Postgres/Neon line in `ROADMAP_STATUS.md` and `.nexus/continuum.json` | Merged as v3.5.0 | Phase 4 |
| Old continuum `Phase E` | Referenced in some roadmap discussions; **no surviving artifact exists in the repository** | Undefined — treat any `Phase E` reference as having no recorded meaning; do not act on it | — |
| `S1/V1/L1/M0/T1` | Earlier roadmap or contract vocabulary | Historical labels; use the current decision entry and repository contract instead | Pre-Phase-0 vocabulary; no direct phase mapping |
| `R-0XX` (e.g. `R-001`, `R-026`) | Requirement identifiers cited by the modular-monolith decision (PR#13 lineage) | Citation labels only — **no `R-0XX` rows exist in `REQUIREMENTS_LEDGER.md`**; keep for traceability to that decision text | Phase 6 foundation (monolith basis of the Nagar baseline) |
| `PR#12 D1–D4` | A separate feature bundle for job resume, dead-code removal, PDF extraction, and Telegram notification | Cleanly re-implemented on the post-PR#16 mainline (`feat/d1-d4-clean-rebuild`): pending-only `resume_pending_jobs` + `nexus jobs resume`, `pypdf` extraction under the `pdf_extract` job type, fail-safe completion hook; `nightly_channel_management` dead code removed — port signatures untouched | Post-Phase-5 feature bundle — **merged via PR#18 (`d9f5cf9`)** |

These numbering families are now treated as historical labels inside the final seven-phase roadmap. A new decision must use a descriptive title and a unique date, and may include an identifier only when it improves traceability.

## Decision Lifecycle Going Forward

Every new architectural decision must be appended to this file before implementation begins. Each entry must state the date, status, problem, decision, rejected alternatives, impact on contracts, and verification plan. A decision that changes `main` must still follow the project rule that changes enter only through a Pull Request with green local and GitHub gates.

The current record deliberately separates **accepted architecture** from **implemented operations**. Nagar is accepted as the Phase 6 design. No execution operation should be implemented merely because the design has been recorded.

## References

[1]: https://github.com/bot523h/nexus-ai-agent/blob/main/docs/NAGAR_70_OPERATIONS_TDD.md "Nagar 70-operation technical design"
[2]: https://github.com/bot523h/nexus-ai-agent/blob/main/docs/architecture/PORTS.md "NEXUS architecture ports"
[3]: https://github.com/bot523h/nexus-ai-agent/blob/main/ROADMAP_STATUS.md "NEXUS roadmap status"
[4]: https://github.com/bot523h/nexus-ai-agent/blob/main/REQUIREMENTS_LEDGER.md "NEXUS requirements ledger"
[5]: https://github.com/bot523h/nexus-ai-agent/pull/13 "PR 13 — Nagar modular-monolith foundation"

## 2026-09-20 — Wave 3 image generation refinement

**Status:** Accepted for this session; verification recorded below as work completes.
**Problem:** The supplied checkout starts at `316ed33` (Wave 2.5). The five
reported local commits, push watchdog, `creative/image_gen/` adapters and
`ImageGenProvider` are absent. GitHub connectivity and an initial push of the
session branch succeeded. Do not invent or rewrite missing work.

**Decision:** Introduce a self-contained `creative/image_gen/` provider protocol,
Pollinations and Gemini HTTP adapters, bounded per-instance prompt-hash caches,
and bounded asynchronous retry with injectable HTTP transports. Gemini is
fail-closed behind operator `paid_tier` approval, checked before cache access;
log successful provider responses with explicit estimated costs, never secrets
or prompts. Do not claim failed/ambiguous requests are free. No fallback may
silently switch to a paid provider.

The bot and worker compose the provider, not the pack. `/imagine` explicitly
requests text egress; `/slideshow --slides N --fill <title>` consents to generate
only missing images, never uploads existing images, and remains opt-in. Keep
network work in the existing queue for slideshows, preserve five-image/30-second
limits, and preserve notifier-owned successful output cleanup.

**Rejected alternatives:** unbounded/global image caches, retries of validation
or authorization failures, implicit billing, new broker, new render lane,
wholesale dead-code deletion based solely on textual reference counts.
**Contracts:** additive provider API and optional slideshow payload fields;
existing `/image` and upload-only slideshow behavior remain compatible.
**Verification plan:** mock HTTP network/status failures and cost guards; test
three uploaded plus two generated images, consent and cleanup; enforce AST
import boundaries; run `make lint`, `make types`, `make test`. Clean only session
history, keep commits atomic and conventional, never rewrite shared mainline.

### Cleanup disposition

Repository-wide Ruff unused-import checks found no violations; no `# DEBUG`
comments or root `test_*.py` scripts were present. Removed stale root gate
reports, `test_story.png`, and the unreferenced downloaded font ZIP (the actual
`assets/fonts/Vazirmatn.ttf` remains). Replaced the print-only `tests/test_rtl.py`
manual script with `tests/unit/test_story_rtl.py`, using `tmp_path` and real
assertions. Ignore disposable gate reports/root render artifacts. Historical
roadmaps, TODO records, supported command handlers and fixtures are not dead
code merely because they are old; preserve them rather than guess at reachability.

### Implemented filenames, consent, limits and accounting

- `creative/image_gen/provider.py`: immutable request/result and asynchronous
  `ImageGenProvider` protocol. `pollinations_adapter.py` and `gemini_adapter.py`
  are HTTP adapters, not a new command-bus execution manifest. Shared transport
  mechanics live in `resilience.py`; no bot/storage imports, including transitive
  project dependencies. `tests/architecture/test_image_gen_boundary.py` resolves
  absolute/relative imports and tests the detector against prohibited examples.
- `application/image_generation.py` is the operator-settings composition root;
  provider and cache live for the process. Settings changes require a restart.
  `bot/handlers.py` registers `/imagine` without adding another Telegram module
  to the frozen import-boundary baseline. Existing `/image` is unchanged.
- The provider guard requires `paid_tier=True`, a key and a positive operator
  estimate. No automatic paid fallback. `/imagine` and `--fill` enforce the
  existing owner/allowlist policy; `/imagine` uses the existing request limiter.
- `bot/slideshow.py` parses leading `--slides N --fill` options. The flag provides
  consent without a second interaction; a number alone never grants it. Require
  at least one uploaded image and preserve the five-image/30-second queue limit.
  Titles remain subject to the existing 60-character grammar. Generation prompts
  add a distinct scene index; they do not include uploaded image bytes.
- `creative/slideshow/image_fill.py` generates the deficit before the existing
  render lane. `SlideshowRenderPayload` revalidates consent, prompt and counts
  at the queue boundary. Failures use `image_generation_failed`, not raw provider
  exceptions. No change to pack manifests, `JobQueuePort` or the render IR.
- Cleanup: generated files are `generated_<index>_<uuid>.<verified image extension>`
  inside the existing `slideshow_<user>_<job>` workspace. Partial generation is
  removed on exceptions/cancellation; the worker deletes all input images after
  rendering. `master.mp4` is retained only on success for notifier-owned delivery
  and deletion; existing 24-hour stale-workspace pruning remains in place.
- Cache: SHA-256 of provider/model/all request fields, per-instance TTL of one
  hour, 16-entry and 32-MiB LRU limits. Immutable validated image bytes, not paths
  that another job can delete. Serialize requests per adapter to prevent duplicate
  in-flight generation. Never cache failures or bypass billing authorization.
- Retry: three attempts by default (configurable internally within 1–5), async
  exponential delay plus jitter, capped numeric Retry-After; only transport,
  429 and 5xx failures. No redirects, arbitrary endpoints or raw HTTP exceptions
  at the application boundary; responses/images have byte limits and Pillow
  verifies image structure. Gemini honors supported aspect ratios, not a promise
  of exact pixel dimensions, and rejects unsupported seeds.
- Accounting: `image_generation_cost` logs an explicit **estimate per successful
  HTTP response**, including undecodable responses; guards and cache hits emit
  none. Network ambiguity is not proof of zero billing. No assertion that these
  logs form a provider invoice, budget limit or exactly-once billing guarantee.

**Verification so far:** 48 offline adapter cases, real handler registration and
queue integration tests, and 40 architecture tests pass. All source types pass.
No live Pollinations/Gemini requests or paid operations were performed. Final
whole-repository gates and Git synchronization are recorded after sanitization.

### History sanitization and final local gates (2026-09-20)

Interactive autosquash (`GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash
HEAD~8`) folded the access-control follow-up into the slideshow feature commit.
The seven substantive phase commits were retained with Conventional Commit
messages; no mainline commit was rewritten and the already-pushed baseline
remained an ancestor. A pre-rebase bundle was kept in Git's local metadata as a
recovery aid, not as a tracked artifact. Dates were not fabricated.

Final verification on Python 3.11.2 in the local `.venv`:

- `make lint`: passed; Ruff checks and formatting, 295 files.
- `make types`: passed; 184 source files.
- `make test`: passed; **742 passed, 20 skipped**, repeated with `-rs` to inspect
  skips. All 20 require PostgreSQL / `NEXUS_DATABASE_URL`; the GitHub workflow's
  dedicated PostgreSQL job remains the external-service verification gate.
- One upstream Starlette/AnyIO deprecation warning; no test failures. An initial
  run also reported a non-fatal LiteLLM remote-price-map connection warning.
- `git diff --check`: passed. No root `test_*.py`, generated images, downloaded
  archives or stale gate-output reports remain tracked.

Commands use `.venv/bin` on PATH. The test environment did not install the heavy
`llama-cpp-python` / `sentence-transformers` runtime stacks; this session does not
claim live local-model inference, live hosted image-provider availability, or
real-account billing verification. The complete non-slow repository suite above
was run unchanged (no tests disabled or marks added to make the gates pass).
The final verification record is committed before the last push/PR update.

## 2026-09-21 — v3.12.0 release metadata after Waves 2.5 and 3

**Status:** Accepted; separate release PR, pending review/merge.
**Problem:** PR#26 merged as `52329e6022a0bdd9f3f9e287da581b752204c811`,
following Wave 2.5 in PR#25 (`316ed33`), but VERSION and pyproject still report
3.11.0 and the changelog has no released entry for either user-facing addition.
New commands/providers must not ship silently under the old minor version.

**Decision:** Follow the PR#24 housekeeping pattern: synchronize VERSION and
pyproject at **3.12.0**, move Wave 2.5's Unreleased notes into a dated 3.12.0
entry, add Wave 3 notes, and refresh README, roadmap and continuum. This is a
backward-compatible **minor**, not a patch (new `/imagine`, provider adapters and
optional slideshow autofill) or major (no removed command, changed existing
payload requirement or schema migration). The optional local upscale stage is
still unimplemented and is explicitly excluded from the release scope.

**Rejected alternatives:** keep 3.11.0 unchanged; record only a changelog note
without changing installed package metadata; claim all planned Wave 3 work is
complete; fold release bookkeeping into the already-approved feature PR.
**Contracts:** metadata/documentation only; no runtime code, dependency version,
pack minimum-version requirement, database schema, golden or public port change.
`/version` continues reading installed distribution metadata; refresh the editable
installation before testing it. No tag or hosted GitHub Release is created by
this PR, and it must not auto-merge under the authorization for PR#26.

**Pre-merge evidence:** On the exact PR#26 head `471803c`, all three local gates
were re-run in a fresh Python 3.11.2 environment with Ruff/mypy/pytest caches
disabled: lint (295 files), types (184 files), tests (742 passed / 20 PostgreSQL
skips). Actual GitHub runs `35542282107` (pull_request) and `35542278734` (push)
completed successfully, each with both `test` and `migrate-postgres` jobs. The
merge was head-locked to the tested commit; see the PR#26 verification comment.
**Verification plan:** rerun all three gates after reinstalling version metadata,
verify `nexus continuum verify` and version lock-step; let real CI run on the
release PR. Keep the existing session branch, fast-forwarded to the merged main,
so the new PR contains one release commit only and no duplicate feature changes.

**Release-local results:** all three gates passed after refreshing the editable
installation: lint 295 files, types 184 files, tests **742 passed / 20
PostgreSQL-dependent skips** (one upstream deprecation warning). VERSION,
pyproject, installed metadata and `/version` all report 3.12.0.
`nexus continuum verify` passed with 649 AST-counted test functions.
The first release-suite attempt exposed an existing network-timing flake: a
LiteLLM background remote-cost-map warning entered the CLI runner's captured JSON
and failed `test_plan_command_emits_the_plan_as_json` (741 passed, one failed).
The full rerun used the dependency's documented
`LITELLM_LOCAL_MODEL_COST_MAP=True` to read its packaged price map; no source,
test assertions, skip marks or CI workflow were changed to conceal the failure.
Hosted CI for the release commit must still be observed independently.

## 2026-09-21 — Nagar Wave 3 local upscale: optional FFmpeg Lanczos stage

**Status:** Accepted by the owner and implemented; pending PR review/merge.

**Problem:** The slideshow pack can compose and render stills, but it has no
explicit, auditable way to enlarge a low-resolution source before planning. A
model-based upscaler would add a large runtime/model download and violate the
light adapter boundary; silently scaling inside the main render would also hide
a separate derived-media operation from the command history.

**Decision:** Add `slideshow.upscale` as a level-B reversible operation. FFmpeg
runs above the pure command bus and pins measured source/output hashes and
sizes into the command; the handler records a derived image with its parent and
Lanczos provenance. The adapter emits a lossless PNG via the allow-listed
`scale=<width>:<height>:flags=lanczos` filter, uses argv without a shell,
protects the source and existing destinations, writes through a staging file,
and caps scale/dimensions at 4x and roughly 8K pixels. The opt-in Telegram
surface is `/slideshow --upscale 2 <title>`; it executes inside the existing
bounded queue workspace before planning/render and is never inferred. The
standalone adapter also accepts an exact target resolution. There is no new
runtime dependency or external binary: the existing FFmpeg resolver remains
the only process boundary.

**Rejected alternatives:** Real-ESRGAN, torch or ONNX Runtime in this stage
(model/runtime weight and deployment pressure); adding upscale to `/imagine`
(which would put local FFmpeg work into that command's direct hosted-generation
response path); implicit upscale on every render; lossy JPEG output; shell
commands or agent-authored filtergraphs; overwriting uploaded source media.
Model-based super-resolution remains a later optional pack, not a quality claim
made by Lanczos interpolation.

**Impact on contracts:** the slideshow manifest advances to pack version 0.2.0
and declares six capabilities; the frozen Wave 1 registry remains unchanged.
The FFmpeg adapter still contains the repository's sole subprocess call site,
now shared by render/probe/upscale argv. Existing `/slideshow` requests and job
payloads remain valid because `upscale_factor` is optional. VERSION, pyproject,
release files, migrations and workflows are unchanged.

**Verification:** `tests/unit/test_slideshow_upscale.py` runs genuine FFmpeg on
a generated 8x6 PNG and reads back a 16x12 lossless PNG; it also covers exact
resolution, bounded dimensions, overwrite protection, level B, derived-parent
provenance and undo. Command parsing/payload, manifest/catalog coherence and
the one-spawner architecture gate are covered by the existing suites extended
for the sixth operation. Final Ruff, mypy and non-slow pytest results are
recorded on the implementation commit and must be independently confirmed by
GitHub CI before merge.

## 2026-09-21 — Nagar Wave 4a: Caption pack substrate (nexus.language.caption)

**Status:** Accepted by the owner and implemented; pending PR review/merge.

**Problem:** Video creation and playback in Nagar requires captioning capabilities
(speech-to-text, timestamps, Persian/RTL subtitles). However, introducing heavy
deep-learning frameworks (WhisperX, Faster-Whisper, PyTorch, pyannote) immediately
drags massive binary weights, GPU memory pressure, and deployment friction.

**Decision:** Implement Wave 4a strictly as a pure substrate without adding any
heavy dependencies or modifying pyproject.toml:
1. **Manifest:** Add `nexus.language.caption` pack manifest (`pack.manifest.json`)
   declaring 10 capabilities (TDD Section 2.5), data-only, with fail-closed security.
2. **Typed models:** Add `TranscriptSegment`, `WordTiming`, `SpeakerTurn`,
   `TranscriptRef`, and `CaptionAsset` using pydantic models and microsecond timecodes.
3. **Pure operations:** Register `caption.transcribe` (Level A, IMMEDIATE) and
   `caption.generate_srt` (Level B, REVERSIBLE) in the capability registry.
4. **Deterministic formatters:** Implement pure SRT and WebVTT formatters. VTT is
   stored as a companion rendition on the same `CaptionAsset` and derived
   `AssetRecord` rather than a separate operation.
5. **Port and unavailable adapter:** Add `CaptionEnginePort` in `application/ports`
   and `UnavailableCaptionAdapter` which fails closed by raising a typed
   `CaptionProfileUnavailableError` (`caption_profile_unavailable`) whenever
   invoked on unconfigured installations (strict prohibition of silent fallback).
6. **Architecture gate:** Add `test_caption_substrate_boundary.py` to forbid
   imports of `torch`, `whisperx`, `pyannote`, and other heavy speech packages
   in the substrate.

**Verification:**
- `tests/architecture/test_caption_substrate_boundary.py` enforces zero heavy imports,
  pure pack boundaries, port separation, data-only manifest, and typed fail-closed errors.
- `tests/unit/test_caption_pack.py` covers byte-identical golden SRT and VTT outputs,
  Unicode/Persian/ZWNJ text preservation, multiline cues, character escaping,
  timestamp rollover (seconds, minutes, hours, >24h), Level A/B bus dispatch, and undo.

## 2026-09-21 — Nagar Wave 4b: Advanced SubStation Alpha (ASS), Persian/Arabic RTL, and Cognitive Memory

**Status:** Accepted and implemented by Agent C (`arena/01a0c36f-nexus-ai-agent`).

**Problem:** Wave 4a introduced simple SRT and WebVTT formatting, but complex video
typography, Persian/Arabic right-to-left layout, and word-by-word karaoke timing
require Advanced SubStation Alpha (ASS v4.00+). Additionally, LangGraph cognitive
memory writes were not persisted to `LongTermMemory`, and router intent classification
depended strictly on English keywords.

**Decision:**
1. **ASS v4.00+ and RTL Formatter:** Implement pure `format_ass` with microsecond-to-centisecond
   rollover (`H:MM:SS.cc`), bidirectional punctuation anchoring (`\u200F`), and `\N` newline handling.
2. **Pure Operations:** Register `caption.generate_ass_rtl` (Level B, REVERSIBLE),
   `caption.style_vazirmatn` (Level B, REVERSIBLE), and `caption.highlight_words` (Level A, IMMEDIATE)
   in `CapabilityRegistry`.
3. **Burn-in RenderIR Extension:** Extend `RenderIR` with optional `subtitle_path` in `creative/slideshow/ffmpeg.py`
   to build allow-listed `ass` and `subtitles` filtergraphs cleanly.
4. **Multilingual Intent Router:** Add Persian keyword normalization and tokenization in
   `orchestration/router.py` to route Persian user requests accurately across task, memory, and personas.
5. **Cognitive Memory Persistence:** Wire `LongTermMemory.store` into LangGraph's `_memory_writer`
   and wire `ToolRegistry` into `_executor_agent`.

**Verification:**
- 47 architecture boundary tests passed (`tests/architecture/`).
- 63 unit and integration tests passed (`test_caption_ass.py`, `test_caption_pack.py`, `test_router_multilingual.py`, `test_graph_memory.py`, `test_graph.py`, `test_router.py`, `test_persona_routing.py`).
- Pre-push coordination check passed with zero overlap against Agent A and Agent B leases.

## 2026-09-21 — P0 Week-1 security batch + feature-engine wiring (audit follow-through)

**Status:** Implemented on `arena/01a0c3a0-nexus-ai-agent`; pending review/merge.
**Problem:** The 2026-09-21 audit (`AUDIT_REPORT_2026-09-21.md`) found four
critical P0 gaps — bot auth consulted in only two of ~80 command paths
(P0-2), public PII on the dashboard API (P0-5), path traversal in
`/cloud` + `/download` (P0-6) and a duplicate shadowed `CommandHandler("start")`
that left the referral loop dead (P0-4) — plus 379 lines of "documented"
feature engines (`features/tools.py`, WordleFA, NumberGuess, QuickPoll) that
nothing imported (P0-1/P0-8).

**Decisions:**

1. **Global access guard as a conditional-check handler (P0-2).**
   `AccessGuardHandler` (PTB `BaseHandler`) is registered in group -1 and
   its `check_update` returns True *only for denied users*. PTB then runs
   the denial callback (rate-limited reply + structured audit log) and
   blocks the update; allowed users are invisible to the guard. This gives
   one choke point with zero per-command edits and no double-processing,
   while keeping the existing per-command checks (defense in depth).
   *Rejected alternatives:* wrapping every handler (unmaintainable); a
   `TypeHandler(Update)` that always claims (would also block allowed
   users, since blocking is decided by `check_update`, not the callback
   return value); middleware at the HTTP layer only (polling mode has no
   HTTP layer).
2. **AST-whitelist calculator instead of `eval` (P0-1).** The engine parses
   with `ast.parse(mode="eval")` and walks a closed node whitelist
   (numeric constants, the six arithmetic ops + pow/mod, whitelisted math
   functions/constants). Attribute access, subscripts, strings and
   containers are unrepresentable, which kills the classic
   `().__class__.__bases__[0].__subclasses__()` class of escapes outright;
   bounds on length (200), node count (128), depth (64), integer exponents
   (≤1000), factorial (0–170) and result magnitude stop `9**9**9`-style DoS.
   Persian/Arabic-Indic digits and `^`/`×`/`÷` are normalized.
   *Rejected alternatives:* `simpleeval`/`asteval` dependencies (new
   supply-chain surface for a 150-line stdlib module); keeping `eval` with
   a tighter regex (regex gates are the thing that failed the audit);
   keeping `%` as "÷100" (silently wrong for `100%20`; modulo is the
   calculator-correct semantics — documented as a behaviour change).
3. **One shared `FeatureEngines` container (P0-8).** Engines are built once
   in `_init_v2_engines`, stored in `bot_data["feature_engines"]`, and
   passed into `build_handlers` (optional kwarg; tests may omit it).
   Commands are closures from `build_feature_command_handlers` bound to
   that container. This fixes per-call engine construction (which dropped
   quiz/wordle/guess state between messages), the double `ReferralEngine`
   construction, and makes every command unit-testable without a bot.
   **Lazy bot binding:** the Telegram bot only exists at runtime, so
   bindable engines (`ReminderSystem.bind`, `ForceJoinManager.bind`,
   `AnonymousChatManager.bind`) are bound in `post_init` and defensively
   re-checked per use (`_ensure_bot`). *Rejected alternatives:* passing
   `application.bot` through `build_handlers` (build-time API does not
   have it, and would couple tests to a live bot); constructing engines
   inside each handler (the bug being fixed).
4. **Dashboard: PII-free responses + optional bearer gate + private bind
   (P0-5).** Responses are PII-free in *all* modes (only the internal
   surrogate id + join time). `NEXUS_DASHBOARD_TOKEN` enables a constant-time
   bearer check (401 fail-closed when set and wrong). When unset the API
   is open *by design* for local development, and `docker-compose.yml`
   binds 8000 to `127.0.0.1` so the default deployment cannot leak it.
   *Rejected alternative:* always-503 when no token is set (would break
   every existing local/CI consumer of `/api/dashboard/stats`); CORS-only
   (CORS does not stop `curl`).
5. **Path sanitization as a reusable helper (P0-6).** `bot/safe_paths.py`
   (`sanitize_file_name` = base-name-only + control-char/length checks;
   `safe_join` = suffix + `resolve()` + `is_relative_to`) is used by both
   `/cloud` (upload temp file, unique suffix against overwrite) and
   `/download` (DB name → safe local path). The raw command argument is
   never joined to a path; the unclosed file handle in the download
   fallback is fixed.

**Contracts:** new public settings `NEXUS_DASHBOARD_TOKEN`; new commands
`/guess`, `/cancel_remind`, `/reminds`; `/calc` `%` semantics change
(percent → modulo); unlisted users are now denied on all surfaces
(deployments must configure `NEXUS_ALLOWED_USER_IDS`/owner id); two new
files registered in `tests/architecture/legacy_baseline.json`
(`bot/access_guard.py`, `bot/feature_handlers.py` — bot-layer `telegram`
imports, same category as every existing `bot/*` file). No schema
migration: `Reminder.status` is a free-form string (`pending|sent|
cancelled|failed`).

**Evidence:** 108 new tests (behavioural + security payloads + DoS inputs);
881 passed / 20 PostgreSQL-only skips; `ruff check` + `ruff format --check`
clean; `mypy src` clean (197 files). The stale `P0-security-batch` lease
from the finished session `arena/01a0c316-nexus-ai-agent` (PR#30 merged as
`5e5009a`) was released with an explanatory board note and re-claimed by
this session per owner instruction; see `.agents/board.json`.

## 2026-09-21 — P0-7 LLM-egress consent gate + P1-2 event-loop non-blocking

**Status:** Implemented on `arena/01a0c3a0-nexus-ai-agent`; pending merge in PR#34.

### P0-7: AIMemory consent gate

**Problem.** The main message handler (`on_message`) created a fresh
`AIMemoryEngine()` per message and fire-and-forget'd
`update_from_message(user_id, text)` — sending *every* user's raw
message text to the external Gemini model with no consent, no opt-out,
no rate limit, and one new provider per message.

**Decisions:**

1. **Default-deny, consent tri-state.** `UserMemory.ai_memory_consent` is
   `str | None` (tri-state: `None` = unset, `"granted"`, `"denied"`).
   No egress unless exactly `"granted"`. Rejected: opt-out model
   (pre-existing users would egress without knowing), checkbox in
   /settings (too hidden for a privacy gate).

2. **One-time inline-keyboard question.** Shown exactly once per user
   (tracked via `ai_memory_prompted: bool | None`). Ignoring the question
   does not re-prompt (no prompt spam). Rejected: a persistent /consent
   command (would require users to know the command exists).

3. **Gate enforced inside the engine, not at call sites.** Returns an
   outcome string (`EGRESSED` / `SKIP_*`). No future caller can bypass
   the gate. `ensure_consent_prompted()` is a pure-orchestration helper
   (testable, in `memory_handlers.py`).

4. **New Alembic revision `7c2f9d41e8a3`.** Three nullable columns on
   `usermemory` (no server default → zero drift on `alembic check` on
   both SQLite and PostgreSQL). CI pinned from `f4a9c2e71b08` to
   `7c2f9d41e8a3`.

5. **Shared engine instance.** `FeatureEngines.ai_memory` is built once
   in `build_feature_engines` (single Gemini provider, single rate-limit
   dict). `/memory`, `/forget_me`, `on_message` and the consent callback
   all share it.

6. **`forget_user` = consent revoke.** The row is deleted (including
   `ai_memory_consent`), so any future egress requires a fresh vote.
   Rejected: soft-delete (complex, privacy-unfriendly).

**Tests.** 17 new tests in `test_ai_memory_consent.py` (engine gate,
global kill switch, one-time prompt, callback, rate limit, forget-wipes,
shared instance, schema + migration chain). 2 pre-existing tests in
`test_ai_memory.py` rewritten to the new contract with proper DB
isolation.

### P1-2: Event-loop non-blocking

**Problem.** All four sync-DB feature engines (`ReminderSystem`,
`ReferralEngine`, `ForceJoinManager`, `AnonymousChatManager`) executed
synchronous `Session(engine)` blocks inside `async` handler coroutines,
blocking the event loop on every message.

**Decisions:**

1. **`asyncio.to_thread(sync_core)` reference pattern.** Each public
   async method is a thin wrapper over a `*_sync` core. The task
   management (`_schedule`, `task.cancel()`) stays on the loop thread.
   Rejected: migrating to `AsyncSession` (too invasive for a batch
   change, touches every engine and its tests).

2. **Cached engines with `check_same_thread=False`.** `_sync_engine` is
   `@lru_cache`'d per `db_path` (or stored on `self`). Sessions are
   created and used within a single worker thread (safe), but the
   connection pool may be accessed from both the loop and worker threads
   (requires the flag).

3. **Call-site offload for owner commands.** `ForceJoinManager.set_config`
   / `get_config` (sync classmethod calls from `handlers.py`) wrapped at
   the call site with `asyncio.to_thread`. Same for referral
   `format_stats` / `format_leaderboard` / `process_referral` /
   `get_referral_link`.

**Tests.** All 990 existing tests pass (20 PG-only skips). The sync
methods are exercised through the existing behavioural tests which now
go through the async wrapper + `to_thread`.

**Evidence.** `make lint` ✅, `make types` ✅ (209 files), `make test` ✅
(990 passed, 20 skipped, 53.58s).

### Nagar Wave 8/9 — apply lane + local speech (agent F, task-104/105) + task-110 deferral

**Date:** 2026-09-21; implemented on `arena/01a0c460-nexus-ai-agent` (this session first mis-declared عامل E; corrected to F — E was taken twice already, see board `identity_map_note`).
**Status:** Accepted **and implemented** (PR: 104+105 only).

**Decision:**
1. **Apply lane (`nexus.apply.lane`, `creative/rendering/`):** typed `LaneOp` values → validated `LaneIR` → pure compiler (filtergraph + byte-exact argv, golden-pinned) → exactly one FFmpeg process (no shell, staging + atomic publish, probed evidence). Loudness is film-standard two-pass. 8 composable ops: trim/speed/reverse/freeze/xfade/title/loudnorm/duck.
2. **Local speech (`adapters/whisper_local.py`, `[speech]`/`[translate]` extras):** faster-whisper (CTranslate2, int8 CPU, no torch) behind `CaptionEnginePort`, lazy import, `to_thread`, offline-first (hub forced offline without explicit consent). Diarization v1 = energy-VAD anchors + the pack's pure merge-then-stamp. Three pure pack ops registered → **caption `pending=0`, pack activates**. The `[speech]`/`[translate]` extras are unique to this PR (PR#33 has none).
3. **task-110 DEFERRED to PR#33 (no code shipped):** this session also implemented OTIO interop + a sync conv-store, then discovered PR#33 (branch 3aa) had already delivered task-110 first, green, in review — its claim was invisible because it never reached `main` and this base predated it (split-brain). Per the overlapping-PRs rule the duplicate was fully reverted (branch history pre-reset preserved the work). Forensic finding kept for a post-merge supplement (wave-3 task-121): PR#33 keeps `include_markers` but ignores it (zero marker references in its ops) — markers (Marker.2 on the Stack) remain open. Conv-store: PR#33's aiosqlite adapter (15 tests) is the keeper.

**Verification:** `tests/unit/test_rendering_lane.py` (20: 16 golden + 4 real FFmpeg 7.0.2 encodes), `test_caption_engine_adapters.py` (13), `test_rendering_lane_boundary.py` (4); `ruff check` + `format --check` + `mypy` clean; pre-push `agent_board check` zero overlap vs agent B's active lease.

### D-0005 … D-0008 — color/exposure lane ownership decisions (wave 8, ADR-lite)

**Date:** 2026-09-21; implemented on `arena/01a0c58e-nexus-ai-agent`.
**Status:** Accepted **and implemented** (D-0005/D-0006/D-0008 are deferrals —
accepted as *not now*, with the trigger that reopens each one named).

**On the identifier family.** These use a new zero-padded `D-00NN` ADR series.
They are **not** the historical `D1–D10` Phase-D schema bundle in the numbering
table above, and `D-0007` is not `D7`. The log's own rule is that an identifier
is optional and only for traceability; the padding exists precisely so the two
families cannot be confused in a grep.

---

**D-0005 — No `SplitOp`: the lane stays one-input/one-output until a
multi-output encode exists.**

*Problem.* A colour/exposure lane makes fan-out tempting: grade once, emit a
proxy *and* a master from the same pass.
*Decision.* Deferred. `CompiledLane` exposes exactly one `video_out` and one
`audio_out`, `argv()` writes exactly one `-y` destination, and the executor runs
exactly one FFmpeg process with staging + atomic publish. A split would break
the single-encode invariant that makes the artifact's provenance auditable
(one process ⇒ one set of probed evidence ⇒ one journal entry).
*Rejected alternatives.* (a) FFmpeg's multi-output argv (`-map ... out1 -map ...
out2`) — one process but *two* artifacts, so the "one artifact per encode"
evidence model needs a redesign, not a flag; (b) two sequential encodes — doubles
decode cost and makes the two outputs non-identical by construction.
*Reopens when* a real consumer needs proxy+master atomically, and the executor
gains per-output probed evidence.

**D-0006 — `.nexus/continuum.json` is refreshed only in a release cut, not per
wave.**

*Problem.* This wave adds 178 test cases; the continuum's `test_count_expected`
is already stale on `main` at `649`.
*Decision.* Do not touch it here. Two independent reasons: (1) the file is under
another task's **exclusive-path lease** — board claim `task-135-lockstep-residue`
holds `["README.md", ".nexus/continuum.json"]`, and the coordination rule is that
an exclusive path is not touched by a second branch; (2) a per-wave counter is
guaranteed to churn into merge conflicts across parallel branches for zero
safety benefit, because nothing gates on it. It is refreshed deliberately at a
release cut (next: v3.14), together with `VERSION`/`pyproject`/`CHANGELOG`.
*Rejected alternative.* Bumping it in this PR — it would collide with PR#33's
diff on the same file and steal task-135's scope.
*Consequence, stated plainly.* `test_count_expected` remains wrong after this
wave. That is pre-existing drift with an owner, not new drift; task-135 stays
queued.

**D-0007 — The EV→`eq` gamma mapping, and the source of every colour bound.**

*Problem.* Three FFmpeg filters, three different notions of "out of range", and
the popular documentation disagrees with itself (`eq` contrast is quoted as
`-2.0…2.0` in some places and `-1000.0…1000.0` in others).
*Decision.* Take the bounds from the filter **sources**, and clamp in the IR:

| Fact | Source (FFmpeg `master`) |
|---|---|
| gamma clipped to `[0.1, 10.0]`, contrast to `[-1000.0, 1000.0]`, brightness `[-1,1]`, saturation `[0,3]`, `gamma_weight` `[0,1]` | `libavfilter/vf_eq.c` — `set_gamma` / `set_contrast` / `set_brightness` / `set_saturation` all use `av_clipf` |
| `eq`'s LUT is `v → v ** (1/gamma)`, hence `gamma = 2**EV` brightens on positive EV | `vf_eq.c` `create_lut` (`double g = 1.0 / param->gamma`) |
| `eq` is a *true* no-op at neutral | `vf_eq.c` `check_values` → `param->adjust = NULL` when contrast 1.0, brightness 0.0, gamma 1.0 |
| `eq` takes the non-LUT fast path only while `\|contrast\| < 7.9` | `vf_eq.c` `check_values` |
| `temperature` = `{.dbl=6500}, 1000, 40000` | `libavfilter/vf_colortemperature.c` `colortemperature_options[]` |
| `kelvin2rgb(6500)` ≈ `(1.000, 0.997, 0.981)` — **not** an exact identity | `vf_colortemperature.c` `kelvin2rgb` |
| `eq` accepts planar YUV/gray only; `colortemperature`/`colorbalance` accept RGB only (disjoint sets) | `vf_eq.c` `pixel_fmts_eq[]` vs `vf_colortemperature.c` `pixel_fmts[]` / `vf_colorbalance.c` `pix_fmts[]` |
| `gm` = "set green midtones", `{.dbl=0}, -1, 1`, **added** to green | `libavfilter/vf_colorbalance.c` `colorbalance_options[]` + `get_component` |

*Consequences adopted.* `gamma = clamp(2**EV, 0.1, 10.0)` clamped in Python, so
the argv never claims a grade FFmpeg would have silently clipped; the unclamped
band is therefore ±log2(10) ≈ ±3.32 EV and both ends are pinned as golden
contracts. `tint/50 → gm`, positive = green. `6500 K` is neutral because it is
the filter's own default, and the RGB stages are elided there because they do
*not* short-circuit. The disjoint pixel-format sets are the reason for the single
`yuv420p → rgb24 → yuv420p` round trip, shared by both RGB filters.
*Rejected alternatives.* Reading the bounds from prose docs (they conflict);
letting FFmpeg clip (the argv would lie); `eq=brightness` instead of gamma
(brightness is a linear offset that clips highlights, gamma is the curve that
matches a stop).

**D-0008 — No filmic tone-mapping (`tonemap=hable`) in this lane.**

*Problem.* Once an exposure op exists, the obvious next step is a filmic
highlight roll-off; `hable` is the well-known style.
*Decision.* Not now. `tonemap` requires linear-light float input, which means a
`zscale` transfer-primaries/matrix chain and a working knowledge of the source's
tagged colour space. Guessing the input transfer function is worse than not
tone-mapping: it silently re-grades every clip differently depending on its
metadata. The lane currently guarantees a *predictable* transform.
*Rejected alternatives.* (a) `tonemap=hable` on assumed BT.709 input — mislabels
any HLG/PQ source; (b) `eq` with `gamma_weight < 1.0` as a cheap highlight
protect — plausible, but it changes the meaning of the EV mapping in D-0007 and
needs its own photometric contract.
*Reopens when* the lane can read the source's colour metadata via `ffprobe`
(already the executor's evidence path) and pin it in the IR, so the transfer
function is a recorded input rather than an assumption.

---

**Verification (this wave).** New: `tests/unit/test_rendering_lane_exposure.py`
(16 golden pins) and `tests/unit/test_lane_duration_algebra.py` (162 cases:
40 seeded lanes × 4 invariants + 2 structural guards). Both files were
mutation-checked — six deliberate breaks of the mapping/elision/guards and four
breaks of the duration algebra each turned the suite red, and a new op added to
the `LaneOp` union is caught by the restatement-vocabulary guard. Runbook:
`docs/ops/COLOR_LANE.md`. Full suite on this branch: **1359 passed, 20 skipped**
against a measured base-commit baseline of **1181 passed, 20 skipped** — +178
cases, no new skips, no regressions. `ruff check .` → 0 errors;
`ruff format --check .` → no files to reformat; `mypy src` → clean, 221 source
files. The exact invocations are in §3 of that runbook.

---

## 2026-09-22 — dead engines behind live handlers: wire through `bot/surface` (D-0009)

**Status:** Accepted and implemented on `arena/01a0cb38-nexus-ai-agent` (PR#54).
**Evidence:** `docs/audits/DEAD_ENGINES_2026-09-22.md`.

**D-0009 — Wiring a dead engine goes through the framework-free surface, and the surface owns
the authorisation the engine never had.**

*Problem.* `features/ads.py` (242 lines, ten methods), `features/channel_manager.py` (239 lines)
and `features/onboarding.py` (122 lines) had **no importer anywhere in `src/`** — verified by
`grep -rn` over `src/ tests/ scripts/ migrations/` — while `bot/handlers.py` answered thirteen
commands with constants (`"(simulated)"`, `"Ad campaign created successfully."`, `"Onboarding step
completed!"`). Two properties make this a design decision rather than a bug fix: (a) the fake
replies *cannot* be distinguished from working code by reading the handler list alone, and (b) the
engines' write APIs are id-only (`AdManager.pause_campaign(campaign_id)`), so a naive wiring turns
a dead module into a cross-chat IDOR.

*Decision.* Add one module per engine under `bot/surface/` and change nothing but imports in
`handlers.py`; keep every engine call behind `asyncio.to_thread`; put the chat-scope authorisation
in the surface (`_load_owned`); render only fields the schema stores; and let
`tests/unit/test_surface_registration.py` act as the ratchet — its `EXPECTED` map (now 20 commands
plus a callback-pattern map) and its forbidden-string list make a stub re-introduction a test
failure rather than a code-review miss. `features/onboarding.py`, `worker.py`, `bot/app.py` and
`README.md` were left untouched because PR#33 is actively editing them.

*Rejected alternatives.* (1) inline calls in `handlers.py` — adds ~200 lines to the highest-conflict
file and is only testable through `build_handlers`, which needs PTB; (2) extending
`bot/feature_handlers.py` — inside PR#33's diff and inside the unfixed `P0-8` double-construction
problem, so a "one owner per engine" claim could not have been made truthfully; (3) DI-first (single
`bot_data` engine registry) — correct, but it pre-empts `task-124`/`P0-8`; the accepted shape is a
step toward it (`manager_for` memoises into `application.bot_data`); (4) truth-only deletion of the
stubs with a "not wired in this build" reply — honest, but it removes thirteen working commands to
fix a lie that real code could have fixed. The full comparison table, with the collision measurements,
is §3 of the audit.

*Consequences.* Two facts are now guaranteed by tests rather than by intent: a `bot/surface` module
that needs a PTB-coupled engine must import it lazily (R12 in `docs/architecture/MODULE_MAP.md`,
enforced by `test_surface_onboarding.py::test_the_surface_package_imports_without_telegram`), and an
engine mutation reachable from a command must carry an ownership check (T14 in
`docs/architecture/SECURITY.md`). Known residuals — no ad-delivery tick yet, `/start` still not
onboarding first-time users, owner-only moderation instead of admin-aware — are enumerated in §6 of
the audit and queued as `task-159`, not left implicit.

*Reopens when* `P0-8` lands a single engine registry: at that point `manager_for` and the ad/channel
read paths should move onto it, and D-0009's surface-owns-authorisation rule should be re-examined
for whether the check belongs one layer down, in the engine, where a second caller (the delivery
tick) would otherwise have to duplicate it.

## 2026-09-24 — P0 stabilization day: legacy creative HTTP lane, studio surface wiring, verifiable backups (D-0010 … D-0012)

**Status:** Accepted and implemented on `arena/01a0d23e-nexus-ai-agent` (board claims
`task-165` / `task-166` / `task-167`, zone `p0-stabilization`).
**Evidence:** `docs/audits/P0_STABILIZATION_2026-09-24.md` (repro scripts, before/after
transcripts, merge-file measurements). Owner directive of 2026-09-24 (Persian, three P0s).

### D-0010 — Legacy `/creative/*` HTTP lane: keep+harden (strictly harden-edged) → deprecate → remove

*Problem.* `api/app.py` carries a pre-Nagar pipeline — `POST /creative/video-edit` +
`GET /creative/jobs/{job_id}` writing into `creative/job_registry.py`’s own registry,
driving `creative/video_director.py` + `creative/ffmpeg_executor.py` directly — beside the
canonical Capability/Command/Job chain. On the audited baseline the GET answered **200 to
any unsigned caller with full job data** (local paths, source URLs), uploads had no byte
cap, and the downloader followed redirects with no SSRF guard (open PR#58 owns the SSRF
fix). Two hardened parallel creative pipelines is exactly the architectural duplication
the owner forbade.

*Decision.* One canonical creative execution path: the studio chain
(`creative_surface → JobQueuePort → worker → packs registry → CommandBus → lane →
measured artifact → translated notify`). The legacy lane survives only as a
keep+hardened, deprecated, contract-frozen edge — strictly to not collide with the
in-flight SSRF repair (PR#58) and not to orphan the job-format consumers that CHANGELOG
v3.0 recorded as live — on a deprecation track whose removal task lands after PR#58
merges. Hardening shipped now: the GET runs the same fail-closed HMAC gate as the POST
(503 without `NEXUS_API_HMAC_KEY`, 401 unsigned/stale/wrong; no new auth stack), multipart
uploads over `_MAX_UPLOAD_BYTES` (500 MiB) die 413 before a job row exists with partial
temp files unlinked, and both routes are `deprecated = True` in OpenAPI.
An architecture ratchet freezes the `/creative` route set, whitelists every importer of
the three legacy modules, and requires both handlers to call the gate — so the legacy
surface can only shrink from here. GET HMAC signs `"{timestamp}:"+b""` (empty body).

*Rejected alternatives.* (1) *Immediate removal* — would orphan the in-flight PR#58 SSRF
repair and the signed consumers the v3.0 changelog documents; removal is sequenced, not
abandoned. (2) *Migrate into packs* — the legacy lane's Gemini-Vertex freedom is
incompatible with the typed-operation command bus; migration without a typed-op
substitute is a rewrite, not a stabilization. (3) *Leave as-is with a docs warning* —
the unsigned-GET data exposure is a live vulnerability, not a documentation issue.

*Reopens when* PR#58 merges: removal PR (routes + three legacy modules + registry),
evidence = ratchet suite + zero callers in `grep` + a release note.

### D-0011 — `/edit` `/caption` `/grade`: the only Telegram face of the canonical chain

*Problem.* On the audited baseline the studio surface was dead and lying: the handlers
were never registered in `bot/app.py`, the worker had no `creative_render` handler, the
command map leaked a bogus `mapper` key, idempotency used `uuid4()` (Telegram redelivery
= duplicate jobs), users saw raw `creative.not_replied`-style keys, and queued /
completion flows had no translations (`creative.*` absent from all 15 locale files).

*Decision.* One one-shot surface, one chain. The surface validates via the pure mapper,
stages the replied media into a deterministic job workspace
(`creative_<sha256(idempotency)[:12]>`), enqueues `creative_render` with an idempotency
key anchored to the Telegram message id (`creative:{user}:{chat}:{message}` — redelivery
dedupes at the durable UNIQUE key), and speaks only i18n catalog strings (16 new
`creative.*` keys × 15 locales; parity-gate enforced). The worker
(`creative/render_jobs.py`) treats the queue row as an untrusted trust boundary
(workspace containment under `creative_temp_dir`, input inside workspace, extra keys
forbidden), dispatches the canonical op through `build_runtime_registry` +
`CommandBus` with the same idempotency key, renders with the allow-listed FFmpeg, and
returns measured facts (probe + sha256). Execution honesty: the op matrix is exactly
what exists — `edit trim|speed|reverse`, `grade exposure|proxy|otio`,
`caption transcribe` — while `lut` (no shipped `.cube` assets / no lane LUT op) and
`burnin` (no subtitles instrument in the lane IR) are **refused typed at the surface**,
never accepted and faked; caption chains fail closed typed (`caption_profile_unavailable`)
when the `[speech]` engine is absent, mirroring the §7 rule of CREATIVE_STUDIO.md.
The OTIO and caption→SRT branches deliver real document artifacts. Completion notify
(translated, typed failures, workspace ownership + cleanup) lives in the grandfathered
`bot/app.py` since the frozen import-boundary bars telegram from new modules.

*Rejected alternatives.* (1) touch `bot/handlers.py` — task-106's original constraint,
kept; the registration is one dedicated block in `build_application`. (2) Ship
`lut`/`burnin` as stubs — explicitly forbidden by the anti-silent-degradation rule.
(3) A new notify module outside the grandfathered set — violates the frozen
import-boundary ratchet.

*Reopens when* a lane LUT/subtitles instrument or shipped LUT assets exist — at that
point the two refused ops re-enter the matrix with their own regression tests.

### D-0012 — Backup "success" must be measured and round-trip-verified, never asserted

*Problem.* `maintenance/backup.py` returned `uploaded=True` the moment the upload call
returned: a corrupt, truncated or wrong-key artifact in R2 was indistinguishable from a
healthy backup, and the summary carried no timestamp, so "nightly backups exist" was an
unverifiable sentence while the scheduled workflow's failures were invisible to the
success path. Separately, `sqlite3.backup()` against a 0-byte or corrupt source silently
yields a valid-looking but empty dump (measured: 4096-byte header-only artifact) — a
perfect false-success masquerade.

*Decision.* The success contract is now: artifact exists AND non-empty AND sha256-measured
AND locally verified (SQLite: restore into an isolated temp DB → `PRAGMA integrity_check`
= `ok` AND non-trivial user-table inventory; PostgreSQL: pg_dump completion footer +
non-empty — restore-into-cluster needs a live target the job has none of, recorded as the
typed limitation) AND round-trip-verified after upload (re-download remote bytes, must be
byte-identical) — else the run hard-fails (non-zero exit, never mislabeled). Success ⇒
summary + structured log carry `sha256` / `size_bytes` / `verified` / `timestamp`.
No new backup tooling was imposed: engine selection (`pg_dump` for
`NEXUS_DATABASE_URL`, sqlite online-backup otherwise) and the R2 provider chain were
kept from the repo; `R2Provider.download` already existed, so no `storage/` change was
needed. Residual (owner-side, task-164): repository secrets for R2 must be configured
before a real dispatch can prove end-to-end truth in Actions — with validity of the
pipeline now test-covered, the remaining failure would be loud and typed.

*Rejected alternatives.* (1) rclone/sqlite3 CLI/rsync tooling — rejected *after* reading
the engine: the repo's own dump primitives and provider already expose download, so new
dependencies would solve a problem the repo had already solved. (2) Treating a
successful `upload()` return as proof — that was the reported bug.

### D-0013 — Job success is a verified state, not a handler's word (canonical job lifecycle)

*Problem.* The queue completed a job the moment its handler returned a dict: a handler that claimed
`{"success": true}` with a zero-byte, truncated, stale or wrong-path artifact ended `COMPLETED`, and
nothing in the job layer ever re-measured the claim (A-side reproduced: `test_ab_...` completes a
zero-byte claim when the verifier registry is opted out). The worker adapter also deleted its
destination before re-rendering (`out_path.unlink()` + `overwrite=True`), so a failed retry destroyed
the previous bytes, and document artifacts (`.srt`/`.otio`) were written non-atomically — a crash
mid-write left a half file under the final name. Execution success and job success were conflated.

*Decision.* The canonical chain Command → Job → Runtime Execution → Artifact Verification → Result is
now explicit and enforced. `JobStatus` gains `VERIFYING` (persisted values otherwise unchanged — no
reasonless rename; canonical aliases `RUNNING≡PROCESSING`, `SUCCEEDED≡COMPLETED` documented in
`jobs/lifecycle`). The queue owns an independent verification phase for `creative_render` (default
registry, injectable): exists → size > 0 → expected path per operation → workspace containment →
sha256 recompute → probe evidence for media (the runtime's own allow-listed-binary probe) →
structural checks for documents; success requires execution success AND verification success;
anything else is terminal `failed` with `verification_failed:<code>` — including a crashing verifier
(fail-closed). Every transition is a guarded, status-conditioned UPDATE with owner+invariant
(`jobs/lifecycle.TRANSITIONS`); terminal states have no outgoing edges. Idempotency keeps
first-payload-wins with a structured conflict log; `attempt` counts executions; `get_result_chain`
assembles the Result (command/job/project/operation ids, attempt, statuses, three identities, sha,
size, probe, failure reason). The worker no longer deletes-then-renders (atomic rename replaces only
its own key-scoped previous attempt) and writes documents atomically. Runtime ownership untouched:
`creative/rendering/*`, `creative/packs/*`, `creative/studio/*`, `creative/slideshow/ffmpeg.py` are
zero-diff; the runtime's own probe/sha256 functions are the evidence source.

*Rejected alternatives.* (1) Verifier inside the handler — self-attestation, the exact
"verification uses the write response" anti-pattern. (2) A separate verification worker/queue —
over-engineered for an in-process SQLite monolith (no broker by architecture). (3) Renaming
`PROCESSING/COMPLETED` to `RUNNING/SUCCEEDED` in the persisted enum — a compatibility break with no
behavioral gain; aliases carry the canonical vocabulary instead. (4) Raising on same-key/different-
payload enqueue — would break the redelivery-collapse contract the Telegram surface depends on;
first-payload-wins + conflict log is deterministic and observable.

*Evidence.* `tests/integration/test_job_lifecycle_queue.py` (M1–M10, M10b, A/B, VERIFYING
observability, recovery, attempt accounting, §17 invariant on the real FFmpeg chain) and
`tests/unit/test_job_lifecycle.py` (transition matrix, invariants, claim dialects). Contract doc:
`docs/architecture/JOB_LIFECYCLE.md`.

---

## 2026-09-24 — Verification closure on the task-178 contract: every job type verified, gaps recorded honestly (D-0014)

### D-0014 — Gap closure rides the existing registry; verification dialects are per-artifact, never generalized guesses

*Problem.* PR#71 (task-178) proved the canonical lifecycle but shipped the built-in verifier
registry with only `creative_render`; its own board note listed the handoff GAPs: `slideshow_render`
had no verifier (a lying/truncated/zero-byte master completed the job), and the legacy `/creative`
HTTP lane can persist `done` with no artifact measurement. `pdf_extract` completed on a
`{"message": …}` result with no measurable artifact at all, and `story` returned a bare
`output_path` with no digest to cross-check.

*Decision.* Close the gaps **on** the task-178 contract — additive registration through the
existing `register_artifact_verifier` extension point, no lifecycle redesign (VERIFYING,
transition naming, `render_jobs.py`, runtime engine all untouched; this branch fast-forwards onto
PR#71's head so its commits are preserved). Each job type gets a verifier matching its **real**
artifact, measured from the tree: `slideshow_render` → the workspace-contained master `.mp4` at the
dispatched path (runtime probe); `story` → the Pillow-rendered PNG (Pillow structural decode);
`pdf_extract` → the extracted text layer, now persisted atomically at the payload-derived
`<stem>.extracted.txt` (whole-file UTF-8 decode). The RAG ingestion behind `pdf_extract` is an
external side effect and is deliberately NOT claimed as verified (absent independent evidence ⇒
not "done"). Consequence accepted: an empty text layer now FAILS (`empty_artifact`) instead of
reporting success — a removed false success. The legacy HTTP lane is NOT touched: it stays
frozen+deprecated (D-0010) and GAP-D is recorded with owner, risk, acceptance test and an
executable tripwire instead of code. An architecture ratchet
(`tests/architecture/test_verification_registry_ratchet.py`) now fails if any handler lacks a
verifier — "execution success = job success" cannot come back silently.

*Rejected alternatives.* (1) Porting PR#67's runtime `creative/artifacts.py` into the job layer —
unmerged branch, cross-ownership duplication (explicitly forbidden), and the job layer already has
its own evidence seam. (2) A content-addressed artifact store — a publication redesign against the
frozen expected-path contract and the bot's file-path notifications. (3) A generalized
"media verifier" for story/pdf — would have probed a PNG with ffprobe and a text file with nothing;
dialects follow the real artifacts. (4) Fail-closed-registering a verifier for `pdf_extract`
without persisting an artifact — would have broken a working capability instead of making it
honest.

*Evidence.* `tests/unit/test_job_verification_gaps.py` (dialects + verifier units),
`tests/integration/test_verification_gap_closure.py` (real FFmpeg encode for GAP-A; real pypdf for
GAP-B; zero-double Pillow chain for GAP-C; attacks A–H against the DEFAULT registry),
`tests/architecture/test_verification_registry_ratchet.py`,
`tests/architecture/test_legacy_lane_verification_gap.py` (GAP-D evidence, read-only). Mutation
proofs: verifier bypass (11 red), path-validation bypass (3 red), sha bypass (2 red) — all reverted.
Reports: `docs/audits/VERIFICATION_GAP_REPORT_2026-09-24.md`,
`docs/audits/CROSS_PR_TRUTH_2026-09-24.md`, `docs/audits/VERIFICATION_TRUTH_MATRIX.json`.

### D-0015 — A typed failure is a FAILURE of the job: 6-state taxonomy, classified retryability, stage→verify→publish, lifecycle-state notifier truth

*Problem.* Gate 5 reconciliation (task-181) reproduced four defects against the task-178/180 tree.
(1) A typed user failure (`{"success": false, "error_code": …}`) reached **`completed`** — the
dialect completed "so the notifier can translate the code", which conflated user-facing copy with
durable truth (`render_failed` + `success=False → completed`). (2) One undifferentiated `failed`
state erased retryability: `failed` was terminal for a transient ENOSPC exactly as for invalid
input, and nothing durable could tell the notifier or a future scheduler which failures are worth
retrying. (3) The `pdf_extract` lane published its sidecar artifact **before** verification: a
refused extraction (image-only PDF ⇒ `empty_artifact`) replaced and destroyed a previous valid
`<stem>.extracted.txt` and left the refused bytes at the destination (reproduced: old artifact
"OLD VALID EXTRACTION" → `''`). (4) Trace events belonging to a job carried **no `job_id`**
(`creative_render_start` observed with `job_id` absent — the observability pipeline supports
`structlog.contextvars` binding but nothing bound it).

*Decision.* (1) The typed dialect is a **failure status**, never `completed` (GAP-A). The queue
short-circuits it before verification (`typed_failure:<code>` persisted, typed result preserved
for the notifier), and verifiers refuse a typed result fail-closed (`typed_user_failure`) if one
ever reaches them — two independent layers (D-0013's "job success is a verified state" now also
means "job failure is a durable state"). (2) `JobStatus` splits `failed` into
`failed_retryable` / `failed_terminal` (GAP-B): `jobs/failure_semantics` classifies every failure
family by one principle — RETRYABLE iff the world can change to make the identical request
succeed — with per-code/per-errno/per-reason rows and tests (temporary IO, dependency unavailable,
worker crash → RETRYABLE; invalid input, unsupported operation, permission error, deterministic
handler defects, `render_failed` → TERMINAL; artifact-measurement disagreements → RETRYABLE;
unknown codes → TERMINAL, fail-closed toward visibility). Pre-task-181 rows spelling `"failed"`
read back as `failed_terminal` (`parse_job_status`). **No retry scheduler is built** (explicitly
outside scope): both failure states are terminal as implemented and the reserved
`failed_retryable → pending` edge stays out of the transition matrix (fail-closed). (3) The one
lane with a destination outside the job workspace (`pdf_extract`) gets queue-owned publication
(`ArtifactPublication`): stage at `<stem>.extracted.txt.staged` → verify → atomic `os.replace`
publish → **re-probe the published bytes** → persist success; any refusal retracts the staged temp
and preserves the previous published artifact. Workspace lanes map the same order to delivery-time
publication. (4) The queue binds `job_id` into `structlog.contextvars` for the whole execution and
emits explicit lifecycle lines (`job_processing`/`verifying`/`completed`/`failed`) — a lifecycle
event can never record `job_id = null`. (5) Notifier truth source = durable lifecycle state
(`completed` ⇒ success; `failed_retryable` ⇒ retryable-failure copy; `failed_terminal` ⇒
terminal-failure copy; non-terminal ⇒ silent), with `result.success` demoted to a second refusal,
never the truth source.

*Rejected alternatives.* (1) Keeping the complete-on-typed-failure dialect and only translating
differently — leaves `render_failed → completed` in the durable truth (the reproduced defect).
(2) A third "unclassified failure" state — the classifier is total; a catch-all state would hide
unclassified contract drift instead of failing closed. (3) Backup-then-replace publication — worse
crash semantics (`.prev` orphan states) than stage-then-swap with a re-probe. (4) Building the
retry scheduler to "use" `failed_retryable` — no repository requirement demands a scheduler, and
the mission explicitly forbids new retry infrastructure; classification alone is honest and
complete. (5) Renaming `completed`/`pending` spellings as well — reasonless migration (D-0013's
rule stands).

*Evidence.* Reproduction of all four defects on the pre-change tree (audit:
`docs/audits/GATE5_CLOSURE_2026-09-24.md`). `tests/unit/test_failure_semantics.py` (classification
table, one named test per failure family), `tests/integration/test_gate5_closure.py` (typed-failure
regression — never `completed`, notifier never succeeds; notification matrix; refused-publication
preserves the old artifact and cleans staging; happy-path stage→publish→re-probe; publish-failure
retraction; trace `job_id`; idempotency matrix incl. duplicates during PROCESSING and after
terminal failure; crash-after-rename recovery), reconciled M-suite assertions (M10b now pins the
observed RenderError path — repository behavior wins over its old docstring), plus mutation
harness `scripts/gate5_mutation_probes.py`: 6/6 probes (remove verification / force COMPLETED on
typed failure / skip atomic publish / drop job_id / notifier trusts result.success / bypass
failure_status) each GREEN→RED→restore→SHA-restored→GREEN.

---

## 2026-09-24 — D-0013: one canonical Nagar command + capability contract (Gate 2 reconciliation)

**Status:** Accepted on branch `arena/01a0d43c-nexus-ai-agent` (board task-179);
contract page
[`architecture/COMMAND_CAPABILITY_CONTRACT.md`](architecture/COMMAND_CAPABILITY_CONTRACT.md),
governance record
[`architecture/adr/0005-canonical-command-capability-contract.md`](architecture/adr/0005-canonical-command-capability-contract.md);
tests and CI evidence are recorded separately on the contract page.

*Problem.* Two Gate 2 reports claimed incompatible canonical contracts
(`schema_version = 2` with external id `nagar.command.v1` and a wired bus vs a
parallel `Command Envelope v2` with canonical `nagar.command.v2`, a new
package, and ADR 0005–0008), and the existing `CommandBus` returned cached
results before checking actor, project, capability, schema, references, or
payload. Neither report could be accepted without reconciliation against the
live repository.

*Decision.* Keep `nagar.command.v1` as the external protocol identifier, the
existing `TypedCommand`, registry, reference resolver, pack handlers, and pure
handler boundary. Evolve the envelope with schema `1|2` (legacy shape vs
explicit actor/project/provenance claims), inject a trusted project authorizer
at composition, and derive schema/version/permissions from the installed
registry, never from client snapshots. Canonical order: parse → envelope +
operation schema → actor/project grant → capability/version/permissions →
execution policy (mode + A/B/C/D) → project-scoped references → idempotency
reservation (project, operation, key) with fingerprint conflict → revision
preconditions on new work → pure handler and atomic commit. Same key +
different payload is a deterministic `IdempotencyConflictError`; claim-less
schema-1 commands without an authorizer keep dispatching under deprecated
implicit local trust so the runtime-owned call sites work unchanged. No
second bus, no second resolver, no parallel envelope package, no protocol
rename, no database migration. The `v2` protocol id is refused at parse and
banned from `src/` by guard.

*Limits.* The bus reservation is per-bus in-memory: no cross-process,
cross-instance, or post-restart claim. The durable SQLite queue still returns
the original job id for a reused key without comparing payloads (lifecycle
follow-up, board task-182). A project asset record is logical membership, not
physical file existence. Explicit service grants at the three runtime call
sites are the runtime owner's follow-up (board task-181); until then the
implicit local path cannot be retired. Integration with PR#67's
`required_packs`/lifecycle bus gate is NOT VERIFIED (board task-183; seam at
stage 4). The `preview` execution mode is reserved surface without an
implementation.

*Rejected alternatives.* (B) Agent 2's parallel v2 envelope (no bus
integration, duplicated models, hardcoded operation snapshot, gate
weakening, no PR); (C) a minimal additive change with unenforced
authorization; (D) a full `v2` protocol cutover across manifests and logs;
(A′) PR#68 verbatim (required claims breaking runtime-owned call sites).
Scored in ADR 0005; salvageable Agent-2 ideas (advisory snapshots,
fail-closed locality, preview surface, the matrix question) folded into this
contract, and PR#68's queue hardening plus runtime call-site migrations stay
valid follow-ups for their lanes.

*Reopens when* an authenticated multi-user project store, a durable
cross-process reservation adapter, or the PR#67 lifecycle integration lands;
publish a versioned migration plan and exercise redelivery/crash recovery
before claiming exactly-once or production-grade durability.

### D-0013 amendment (task-183): lifecycle seam integrated at stage 4b

The *Limits* line above ("Integration with PR#67's `required_packs`/lifecycle
bus gate is NOT VERIFIED … seam at stage 4") is superseded. PR#67's
`creative/studio/lifecycle.py` is consumed byte-identical (`9c3a34f`) and
called exactly once, at sub-stage **4b**: after the actor/project grant and
capability permissions, before execution policy, reference validation, the
idempotency reservation, preconditions and the handler. Refusals: unknown,
`STUB`, `RETIRED`, and `EXPERIMENTAL` without the opt-in.

*Trust boundary.* The opt-in is composition-root state
(`CommandBus(..., allow_experimental=...)`), never an envelope field and
never a queue-row field. The render worker, the only production site that
sets it, derives it from the canonical operation via
`render_jobs.EXPERIMENTAL_OPT_IN_OPERATIONS`, which is pinned to exactly the
surface operations on an `EXPERIMENTAL` pack. An earlier revision carried it
as `CreativeRenderPayload.allow_experimental`. That let whoever wrote the queue
row choose lifecycle policy, so it was replaced before merge.

*Merge with PR#67.* The merge is semantic, not just textual. Keep stage 4b,
drop PR#67's stage-3.5 call and its payload/surface flag, and add
`color.apply_lut` to the opt-in set. The architecture guards and the
opt-in-completeness test go red on any other resolution.

*Evidence.* `tests/unit/test_gate2_lifecycle_seam.py`,
`tests/unit/test_gate2_lifecycle_mutations.py`,
`tests/unit/test_capability_lifecycle.py`,
`tests/architecture/test_lifecycle_gate_boundary.py`, and the task-183
trust-boundary tests in `tests/unit/test_creative_render_jobs.py`.

## 2026-09-25 — Gate-5 FINAL REPAIR: execution ownership, publication transaction, notification truth (D-0016, D-0017)

Evidence-first record: `docs/audits/GATE5_FINAL_REPAIR_2026-09-25.md` (R1–R5
OLD-RED at `6b86633` → NEW-GREEN, T1–T15, M1–M10 16/16 killed, negative-space
scan). Research basis in its section 2 (Kleppmann fencing tokens; river
PR#1373 stale-rescue predicates; maritime-claims PR#455 lease-fingerprint
revalidation; shizzle PR#42 lease-fenced stage outcomes; running PR#470
LeaseBefore CAS reclaim; Python `os.replace` + atomic-write/backup patterns;
SQLite single-writer conditional-UPDATE CAS; microservices.io transactional
outbox — evaluated and rejected here).

### D-0016 — One fenced execution generation per job; COMMIT_CONFIRMED fan-out; truth over delivery

*Decision (options A–E compared in the repair report §5 — chosen: E hybrid).*
Every reservation mints one **execution generation**: the row's `attempt`
increments (the monotone fence — Kleppmann's ratchet; a UUID alone is
explicitly *not* a fence) and a fresh `owner_token` is stored (durable
identity). The lease fingerprint `(id, attempt, owner_token)` is validated
inside **one** shared predicate (`InProcessJobQueue._fence_update`) used by
every post-reservation transition; a stale worker's writes match zero rows.
Reservation is a strict `PENDING → PROCESSING` CAS (rowcount must be 1) — the
old `PROCESSING → PROCESSING` re-claim is retired (it was the R5 dual-claim
ownership bug, reclassified from "contract" to BUG with reproduction).
Takeover (`resume_pending`) is explicit and fenced: lease-expired rows only
(`started_at` + TTL; legacy NULL `started_at` = expired), and reclaiming
invalidates `owner_token`. `_mark_pending` is a fenced self-release (the old
bare `UPDATE … WHERE id=?` could reopen terminal and newer executions — F2).

*Notification contract (explicit mission-section-12 decision):* **truth
("never lie") enforced, delivery ("eventually notify") best-effort.** All
terminal fan-out fires only when the fenced terminal UPDATE applied
(COMMIT_CONFIRMED); a displaced generation is silent. No delivery retry and
**no transactional outbox** — the durable row already is the truth record and
outbox would only buy delivery-grade retry, a declared non-goal (evaluated,
rejected as unjustified migration). `_mark_completed`/`_mark_failed` return an
observable `bool` for exactly this gate.

*Also closed here:* bare `{"success": false}` (missing/empty `error_code`) is
a typed failure `typed_failure:untyped_failure` (fail-closed, TERMINAL) — it
can never complete (R4; previously it completed whenever verifiers were
opted out). Tests: T1–T8, T12–T15; mutations M1–M5, M9, M10.

### D-0017 — Publication is a journaled, inode-guarded transaction; the re-probe stays

*Decision (options A–E compared in the repair report §14 — chosen: hardened
A + E's transaction shape).* `ArtifactPublication` is a four-verb protocol:
*publish* = backup (`<name>.bak`, hardlink/copy) + swap journal
(`_publication{backup, published_inode}`, persisted fenced) + atomic
`os.replace`; *retract* = staged cleanup + **inode-guarded restore** of the
backup (the destination returns to its pre-swap bytes iff it is still this
swap's inode — a newer owner's publication is never overwritten);
*recover* = crash-window resolution before the next execution (journaled
uncommitted swap → restore; journal-less backup → duplicate dropped);
*retire* = post-COMMIT_CONFIRMED backup cleanup. The durable commit (fenced
`_mark_completed`) is the transaction's commit point; any refusal after the
swap rolls back.

*The post-publish re-probe is KEPT* (the mission asked this seriously,
section 15): it is the only independent measurement of a **pluggable**
publisher's claim at the final name, the T/M lists require its decision
guard (T9/M8), and its failure path is now safe — which was the only real
argument against it. "Previous artifact is never touched on refusal" now
survives **with proof** (R1 OLD-RED showed the old claim was false for the
post-replace window). Backup-orphan residuals (crash after commit before
retire; steal-convoy chains) are bounded and recorded as accepted risks in
the repair report §32. Tests: T8–T11; mutations M6–M8.
