# NEXUS AI — Architecture Decision Log

**Status:** Canonical historical record; revision 4 effective 2026-09-20  
**Scope:** Architectural, operational, and roadmap decisions from Phase 0 through the released v3.10.0 baseline, the accepted Phase 6 Nagar design, and the implemented Nagar Waves 1–2.  
**Main baseline for this revision:** `3c8d1de0` (the PR#14 merge — modular monolith + canonical decision log). The live head may have advanced; consult `git log origin/main`.  
**Current release baseline:** `v3.10.0` (Phase 6 Wave 1); Phase 6 Wave 2 lands on the unreleased line (`main` after PR#21).

**Revision history**

- **r1 (2026-09-20, PR#14):** initial canonical log — phase history, core rejections, Nagar acceptance, numbering families.
- **r2 (2026-09-20):** added the Cognee and manual-Neon-keep-alive rejections, sharpened the "Nexus World" rejection (3-D world model + alleged quantum decision algorithms), converted the zombie-branch list into a per-branch disposition table with the required manual-deletion note, added the old-continuum `Phase D/E` numbering row, mapped every numbering family onto the final seven-phase roadmap, and recorded an open-PR snapshot.
- **r3 (2026-09-20, PR#19 + the v3.10.0 release commit):** recorded the implemented Nagar Wave 1 core as an accepted decision, moved the release baseline to `v3.10.0`, corrected the Phase 6 “implementation has not started” status, added the PR#18/PR#19 rows to the PR snapshot, marked the D1–D4 bundle as merged (`d9f5cf9`), and locked the Celery/Redis scan result into the record.
- **r4 (2026-09-20, PR#21 + the Wave 2 slideshow pack):** recorded Nagar Wave 2 — the capability-pack substrate and the slideshow pack — as an accepted and implemented decision, including the “evidence above the bus, pure handlers inside it” split, the additive state extension (`Project.assets` / `Clip.effects` / `AssetRecord` / `EffectLayerRef`), the level assignments of the five new operations, and the dependency verdicts (librosa deferred, Real-ESRGAN deferred, hosted image *generation* left out of the render path).

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
**Status:** Architecture accepted; **Wave 1 (“Green Cockpit” core) implemented and merged** through PR#19 (`ac6c25b`). The remaining operations are not implemented.

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
