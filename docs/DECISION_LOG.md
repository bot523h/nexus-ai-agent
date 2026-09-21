# NEXUS AI — Architecture Decision Log

**Status:** Canonical historical record; revision 6 effective 2026-09-20  
**Scope:** Architectural, operational, and roadmap decisions from Phase 0 through the released v3.11.0 baseline, the accepted Phase 6 Nagar design, the implemented Nagar Waves 1–2 (2a substrate, 2b pack, 2c render lane), and the owner decisions that sequence what comes next (Wave 2.5 bot surface first; image generation behind an adapter).  
**Main baseline for this revision:** `ebe995a` (the PR#23 merge — Wave 2c render lane). The live head may have advanced; consult `git log origin/main`.  
**Current release baseline:** `v3.11.0` (Phase 6 Waves 1–2c on `main`; cut by the housekeeping PR that carries this revision).

**Revision history**

- **r1 (2026-09-20, PR#14):** initial canonical log — phase history, core rejections, Nagar acceptance, numbering families.
- **r2 (2026-09-20):** added the Cognee and manual-Neon-keep-alive rejections, sharpened the "Nexus World" rejection (3-D world model + alleged quantum decision algorithms), converted the zombie-branch list into a per-branch disposition table with the required manual-deletion note, added the old-continuum `Phase D/E` numbering row, mapped every numbering family onto the final seven-phase roadmap, and recorded an open-PR snapshot.
- **r3 (2026-09-20, PR#19 + the v3.10.0 release commit):** recorded the implemented Nagar Wave 1 core as an accepted decision, moved the release baseline to `v3.10.0`, corrected the Phase 6 “implementation has not started” status, added the PR#18/PR#19 rows to the PR snapshot, marked the D1–D4 bundle as merged (`d9f5cf9`), and locked the Celery/Redis scan result into the record.
- **r4 (2026-09-20, PR#21 + the Wave 2 slideshow pack):** recorded Nagar Wave 2 — the capability-pack substrate and the slideshow pack — as an accepted and implemented decision, including the “evidence above the bus, pure handlers inside it” split, the additive state extension (`Project.assets` / `Clip.effects` / `AssetRecord` / `EffectLayerRef`), the level assignments of the five new operations, and the dependency verdicts (librosa deferred, Real-ESRGAN deferred, hosted image *generation* left out of the render path).
- **r5 (2026-09-20, PR#23):** recorded Nagar Wave 2c — the render lane (pure `RenderIR` → filtergraph → argv, one FFmpeg process, staging publish, measured evidence) — as an accepted and implemented decision with its rejected alternatives (agent-authored filtergraphs, `-y` against the destination, trusting the plan's duration, a second `ffprobe` binary, encoding inside a handler, a Python video library).
- **r6 (2026-09-20, v3.11.0 housekeeping PR):** moved the release baseline to `v3.11.0`; recorded two owner decisions — *image generation behind an adapter (Pollinations by default, Gemini opt-in)*, which resolves the open question left by Wave 2 item 7, and *Wave 2.5 (Telegram surface for the slideshow pack) precedes Wave 3*; corrected the Phase 6 status text to Waves 1–2c merged; updated the PR snapshot (PR#23 merged as `ebe995a`, PR#1/PR#2 closed); noted that the lifecycle PR1/PR2/PR3 line has been on `main` since PR#7 (`acdbcb7`, v3.6.0) — the roadmap file had still called it unmerged.

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

The following branches are historical, open, or abandoned proposals and are not part of the active mainline decision path. **They must be deleted manually on GitHub by the owner** — deletion is a remote administrative action and is deliberately not performed by documentation changes:

| Branch | Status | Reason / evidence |
|---|---|---|
| `trae/agent-FdzTxJ` | Rejected — duplicate | A parallel “Creative Studio MVP implementation” (`843f004`) stacked on the PR#9 merge point, superseded by the creative-studio line that actually merged through PR#10 (`62e25ce`). Never merged. |
| `feat/phase1-control-plane` | Abandoned — origin unclear | The “phase one control plane foundation” proposal (open as PR#1 historically). Never merged into `main`; its rate-limiter/control-plane ideas survive only as history. Treat as unowned. |
| `feat/phase2-local-llm` | Abandoned — stacked on an unmerged base | “Provider-agnostic local LLM engine” built **on top of the unmerged `feat/phase1-control-plane`**, so it can never merge cleanly. The underlying need (a provider seam) was satisfied properly by litellm routing in v3.7.0 (Phase 3). |
| `circleci-project-setup` | Irrelevant — CI platform cut | Only adds `.circleci/config.yml` (commits `265d6a0`, `2818d9d`). `.circleci/` does not exist on `main`; the project standardizes on GitHub Actions (`.github/workflows/ci.yml`, `maintenance.yml`). |

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
