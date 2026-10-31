# NAGAR-IBPC-LIVE-EDITING-01 — Live Architecture Audit (Milestone 0)

**Mission ID:** `NAGAR-IBPC-LIVE-EDITING-01`
**Date:** 2026-10-31 · **Branch:** `arena/7622c61d-nexus-ai-agent` (session-fixed branch name; the mission-suggested name `arena/nagar-proof-carrying-live-editing-20261031` cannot be used because a session is bound to exactly one branch)
**Base:** `main` @ `ab56beccc8817ddde1cb005f197c172e132de878` (PR #293 merge)
**Board:** claim `task-ibpc-live-editing-01`, zone `nagar-ibpc-live-editing`, generation 1, pushed before any code; referee `check` exit 0 across 45 sources on the exact claimed file list.

This audit was produced by reading the live tree, not by trusting prior reports. Every claim below names the file that proves it. Status vocabulary: **EXISTS / PARTIAL / MISSING / PROPOSED / UNVERIFIED**.

---

## 1. Component inventory (live, code-verified)

| # | Component | Status | Evidence in tree |
|---|---|---|---|
| 1 | Creative Intent (typed) | **EXISTS (narrow)** | `src/nexus_ai_agent/application/creative_intent.py` — `CanonicalCreativeIntent`, schema `nexus.creative_intent.v1`, content-addressed `intent_id`, **trim-only** (`operation: Literal["timeline.trim"]`), raw text stored only as `source_text_sha256` |
| 2 | Typed Creative IR / AST | **EXISTS (studio-local)** | `src/nexus_ai_agent/creative/spine/models.py` — `Intent`, `CompiledIntent`, `PlannedOperation`, `CreativeGraph`, `ArtifactRecord`, `EvidenceRecord`; pure pydantic, no I/O |
| 3 | NEL / Semantic Time | **PROPOSED — open PR #273, NOT on main** | branch `feat/nagar-p0-nel-semantic-time-causal-memory` adds `src/nexus_ai_agent/nagar/p0/{nel,semantic_time,causal,commit,intent,runtime}.py` + `docs/engineering/NAGAR_P0_*.md`. PR body states explicitly: *"D2/D6/D7: nonzero-revision mutation safety, durable revision fencing, and intent-to-NEL generation remain explicit follow-up contracts rather than unverified claims"* — so this mission treats D2/D6/D7 as **NOT implemented** and must not build on them. This mission touches none of PR #273's files. |
| 4 | CapabilityRegistry + real op schemas | **EXISTS** | `src/nexus_ai_agent/creative/studio/capabilities.py` (`CapabilityRegistry`, `OperationSpec` with `input_model`, `permission_level`, `execution_modes`, `required_packs`, `preview_semantics`) composed with packs by `src/nexus_ai_agent/creative/packs/runtime.py::build_runtime_registry` (57 ops, 6 packs, `pending=0` since wave 5) |
| 5 | Compiler / Command DAG | **EXISTS (two layers)** | spine: `creative/spine/compiler.py::RulesCapabilityCompiler` (deterministic keyword rules, validates against the registry, refuses to invent inputs); render lane: `creative/rendering/compiler.py::compile_lane` (LaneIR → filtergraph → argv) |
| 6 | Policy / Authority boundary | **EXISTS** | `creative/studio/bus.py::CommandBus.dispatch` — 9-stage fail-closed pipeline (parse → envelope/schema → actor+project authorization → capability+permissions → pack lifecycle → execution policy A/B/C/D → reference pinning → idempotency reservation → revision precondition → atomic apply); `creative/studio/authorization.py` `ProjectAuthorizer`/`ProjectAccess` |
| 7 | CommandBus (canonical single write path) | **EXISTS** | `creative/studio/bus.py` — docstring: "the *only* write path into the central in-memory project state"; in-memory, per-bus `Project` with `state_revision`/`state_hash` CAS (`creative/studio/models.py::compute_state_hash`, `Preconditions`) |
| 8 | Job Queue + attempt fencing | **EXISTS** | `src/nexus_ai_agent/adapters/in_process_job_queue.py::InProcessJobQueue` — durable SQLite rows, UNIQUE `idempotency_key`, attempt tokens, CAS transitions, optional `CausalObserver`. **PR #283 (heartbeat/recovery) is open and out of scope — none of its files are touched.** |
| 9 | Worker | **EXISTS** | `src/nexus_ai_agent/worker.py::default_job_handlers` registers `creative_render` → `creative/render_jobs.py::creative_render_job` |
| 10 | Canonical creative execution chain | **EXISTS (proven e2e)** | `creative/render_jobs.py` — payload validation (`CreativeRenderPayload`, `extra="forbid"`) → guarded workspace (`_guarded_workspace`, `creative_` prefix under `creative_temp_dir`) → `build_job_bus` (runtime registry, server-derived experimental opt-in) → `CommandBus.dispatch` → render lane → measured artifact (`probe_video`, `sha256_file`) → queue-side independent verifier; `nagar/creative/handoff.py::run_free_text_intent_durable` proves free text → durable `creative_render` row (see `tests/integration/test_nagar_durable_handoff_e2e.py`) |
| 11 | Artifact / asset identity | **EXISTS / PARTIAL** | artifact: `jobs/verification.py::ArtifactClaim` (path+sha256+kind, re-measured). Asset library: `creative/assets/` content-addressed blob store landed as PR #205 (open) — not required for this slice; the worker's self-contained project uses `AssetRecord(asset_id="src", content_sha256=…)` (`render_jobs._build_project`) |
| 12 | Project Revision / CAS | **EXISTS (in-memory per bus)** | `Project.state_revision` monotonic, `state_hash` content hash, bus `Preconditions` stale-rejection (`bus.py::_check_preconditions`); schema-2 worker path pins `state_revision`+`state_hash` (`render_jobs._dispatch`). Durable project revisions: PR #290 (task-282 durable project state) is open — **not on main, not assumed** |
| 13 | Creative Graph / lineage | **EXISTS (in-memory)** | `creative/spine/models.py::CreativeGraph` append-only nodes+parents; durable lineage = provenance journal (next row) |
| 14 | Provenance + Artifact Passport | **EXISTS (durable)** | `src/nexus_ai_agent/provenance/{models,journal,passport,observer,paths}.py` — hash-chained `CausalJournal` sidecar (`<queue>.causal.sqlite3`), `verify_chain`, `PassportBuilder` reconciling ledger vs queue authority (`JobFactsProvider`), re-measuring artifacts. Queue records transitions post-commit when a `CausalObserver` is injected |
| 15 | Verifiers | **EXISTS (artifact-level) / MISSING (footprint-level)** | `jobs/verification.py::verify_artifact`, `jobs/creative_verification.py::{creative_render_verifier, slideshow_render_verifier}` — verify the artifact claim exists, path contained, sha256 re-measured, media probed. **No content-level footprint verifier exists** (intended-change + preservation of out-of-scope regions) — this is Milestone 2 of this mission |
| 16 | Studio API / surface boundaries | **PARTIAL** | `bot/creative_surface.py` (Telegram commands), `api/app.py` (HTTP). No approval/preview session API exists — Milestone 3/4 build the session contract in `nagar/ibpc/`, no fake frontend |
| 17 | Voice / Speech / Transcript | **EXISTS (engine) / MISSING (session)** | `adapters/whisper_local.py::WhisperLocalCaptionEngine` behind `application/ports/caption_engine.py::CaptionEnginePort` (optional `[speech]` extra, fail-closed `UnavailableCaptionAdapter`). No live session/turn/transcript model exists — Milestone 4 |
| 18 | FFmpeg / rendering | **EXISTS (real)** | `creative/rendering/{ir,compiler,executor}.py` + `creative/slideshow/ffmpeg.py` — allow-listed binary resolution (`resolve_ffmpeg_bin`: env → PATH → imageio-ffmpeg wheel), no shell, one process, `.part`+atomic rename, `probe_video`, `sha256_file`. Sandbox check 2026-10-31: ffmpeg 7.0.2 via the dev wheel |
| 19 | Preview / approval binding | **MISSING** | bus has `execution_modes`/`preview_semantics` (`capabilities.py::OperationSpec.authoritative_for`) but no preview artifact pipeline and no approval binding anywhere — Milestone 3 |
| 20 | IBPC capsule | **MISSING** | built by this mission as a *contract* on the existing chain — not a new authority, queue, or ledger |
| 21 | Privacy modes | **PARTIAL** | LLM egress consent exists (`core/ssrf_guard.py`, LLM gateway consent); no `LOCAL_VAULT/TRUSTED_CLOUD/EXPERIMENTAL_CLOUD` session privacy contract — Milestone 6 of the mission (implemented in `nagar/ibpc/privacy.py`) |

## 2. The real execution path (as proven by existing e2e tests)

```
free text ──► nagar/creative/handoff.py (normalize, route, propose)          [EXISTS]
          ──► typed CreativeRenderPayload dict (closed constants)            [EXISTS]
          ──► JobQueuePort.enqueue("creative_render", idempotency_key, …)    [EXISTS — durable row]
          ──► InProcessJobQueue: claim → attempt token → handler             [EXISTS — fencing]
          ──► creative_render_job: guarded workspace, build_job_bus,
              CommandBus.dispatch(TypedCommand)                              [EXISTS — authority]
          ──► render lane (LaneIR → filtergraph → one FFmpeg) → output.mp4   [EXISTS — real artifact]
          ──► queue-side verifier (artifact claim, sha256, probe)            [EXISTS — independent of renderer]
          ──► CausalJournal records (enqueued/reserved/verifying/completed)   [EXISTS — evidence]
          ──► PassportBuilder.build(job_id) reconciles + re-measures          [EXISTS — projection]
```

Evidence: `tests/integration/test_nagar_durable_handoff_e2e.py` (artifact-proven, durable, tamper-refusing), `tests/integration/test_provenance_passport_e2e.py`, `tests/integration/test_canonical_creative_execution.py`.

**Enqueue ≠ render success ≠ verification** is already enforced: typed `{"success": False}` results short-circuit to `FAILED_*`, and no verifier `ok` → no `COMPLETED` (`jobs/lifecycle.py`, `jobs/creative_verification.py`).

## 3. Reusable code (this mission imports, never edits)

- `nexus_ai_agent.provenance.models` — `canonical_json`, `digest_of`, `sha256_hex` (the one canonical spelling), `CausalJournal`, `verify_chain`, `CausalEvent`/`EventKind`
- `nexus_ai_agent.provenance.passport` — `PassportBuilder`, `ArtifactPassport`
- `nexus_ai_agent.provenance.paths` — `causal_journal_db_path`, `try_open_causal_journal`
- `nexus_ai_agent.application.ports.job_queue` — `JobQueuePort`, `JobStatus`
- `nexus_ai_agent.application.ports.caption_engine` — `CaptionEnginePort`
- `nexus_ai_agent.creative.studio.models` — `Project`, `AssetRecord`, `TimeRangeUS`, `frame_number_for`, `new_project` (data only)
- `nexus_ai_agent.creative.studio.capabilities` — `CapabilityRegistry` (injected, not constructed, in `nagar/ibpc`)
- `nexus_ai_agent.creative.slideshow.ffmpeg` — `resolve_ffmpeg_bin`, `probe_video`, `sha256_file` (allow-listed measurement binary)
- `nexus_ai_agent.creative.rendering` — lane IR/compiler/executor (used only inside the existing worker; `nagar/ibpc` never imports it)

## 4. Real gaps this mission fills (and how it stays inside the lines)

1. **Text→artifact slice with explicit scope** — exists for trim via the durable handoff; this mission adds the **typed AST + scope + preview + approval** layer around the *same* queue row (no new job type, no direct dispatch).
2. **Footprint verifier** — new `nagar/ibpc/footprint.py`: decodes source and artifact with the same allow-listed binary (read-only, `-f rawvideo`), discovers the frame mapping by content, and rules PASS (bit-exact preservation + intended change) / FAIL (provable out-of-scope change or missing intended change) / INCONCLUSIVE (lossy encoding makes exact preservation unprovable — never a fake PASS; a perceptual score is never accepted as preservation proof). Calibration measured 2026-10-31 with ffmpeg 7.0.2 on `testsrc`: unchanged-content re-encode (crf 20) mean|Δ| ≤ 0.47 (max 63); a real global brightness change mean|Δ| ≈ 31 — the FAIL threshold is set at mean|Δ| > 4.0 per preserved frame, documented in `NAGAR_IBPC.md`.
3. **IBPC capsule** — new typed, versioned, immutable contract in `nagar/ibpc/models.py`, canonical serialization reusing `provenance.models.canonical_json`; evidence references point at the **existing** journal chain + passport (`provenance_reference`), not a parallel store.
4. **Approval binding** — single-use, principal-bound, revision-bound, digest-bound; consume-on-execute; replay/stale/tamper refused. Approval is **not** a permission grant — the bus/queue still authorize.
5. **Live session + voice turn fencing** — `nagar/ibpc/session.py`: session/turn identity, sequence numbers, transcript states, interruption/cancellation semantics; transcript is untrusted input and never execution authority; audio goes through the injected `CaptionEnginePort` only.
6. **Privacy modes** — `nagar/ibpc/privacy.py`: `LOCAL_VAULT` (default) / `TRUSTED_CLOUD` / `EXPERIMENTAL_CLOUD` with explicit `ConsentRecord`s; no provider is wired, so any non-local egress is a typed refusal; raw transcripts/audio appear only as digests in any record.

## 5. Forbidden paths (ownership) — verified against the live board + open PRs

- PR #273 (`feat/nagar-p0-nel-semantic-time-causal-memory`): `src/nexus_ai_agent/nagar/p0/**`, `docs/engineering/NAGAR_P0_*.md`, `docs/engineering/ADR-NAGAR-P0-NEL-SEMANTIC-CAUSAL.md`, `docs/architecture/MODULE_MAP.md`, `docs/README.md` (on that branch), `OPERATION_TRUTH.json`, `continuum/pack_coverage.py` — not touched. Its D2/D6/D7 follow-ups are treated as **not implemented**.
- PR #283 (`arena/1f7a74db-nexus-ai-agent`, queue heartbeat): `adapters/in_process_job_queue.py`, `creative/render_jobs.py`, `bot/app.py`, its tests/scripts/docs — not touched.
- task-281 (PR #293, merged with a post-merge security finding): `scripts/cognition_quality_eval.py`, `tests/unit/test_cognition_quality_eval.py`, `tests/fixtures/cognition_quality_eval/v2_baseline.json` — not touched.
- Active claims: task-267 (`diagnostics/truth.py`, `tests/unit/test_truth_doctor.py`), task-281 (`cognition-quality-evaluation` zone) — not touched.
- Boundary laws that also bind this package: `tests/architecture/test_cognition_isolation.py` (nagar/cognition stays authority-free), `test_nagar_durable_handoff_boundary.py` (nagar/creative owns no execution primitive), `test_import_boundaries.py` (no new langgraph/sqlmodel/telegram imports), `test_no_orphan_modules.py` (new modules must be imported — the new tests import them).
- `docs/README.md` is edited **only** to index the two new docs (referee exit 0 verified 2026-10-31, 45 sources); insertion point is chosen disjoint from PR #273's hunks. `MODULE_MAP.md` is fenced by PR #273, so the boundary→test register entry is recorded as NEXT (below), not edited here.

## 6. NOW / NEXT / LATER

- **NOW (this mission):** M0 audit (this file) · M1 vertical slice text→typed AST→canonical queue→real artifact · M2 footprint verifier · M3 IBPC capsule + readback + approval binding · M4 session + voice turn fencing · M5 privacy modes + adversarial/architecture tests + docs + draft PR.
- **NEXT (registered, not built here):** MODULE_MAP.md §3 register rows for the ibpc boundary (blocked by PR #273's fence on that file); durable (SQLite) approval/session store if the live-editing session must survive restarts (in-memory now, honestly documented); wiring the IBPC service into `bot/creative_surface.py`/`api/app.py` (owned by other zones); PR #273 D2/D6/D7 closure by its owner.
- **LATER:** multi-provider voice benchmark (WebRTC/WebGPU/cloud ASR) — no new provider is added without necessity + consent; multi-asset/multi-track scopes; image/audio footprint scopes (the verifier core is media-kind generic, video-time scope is the proven slice).
