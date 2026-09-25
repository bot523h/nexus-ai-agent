# Operation Truth — Live-Repo Audit & Machine-Verifiable Evidence Chain (2026-09-25)

**Owner branch:** `arena/01a0d708-nexus-ai-agent` (board task `task-190-operation-truth`,
zone `operation-truth`) · **base:** `main @ fe95cf0fdb03742016655e012f9397fda1e46fd7`
**Engine:** `python -m nexus_ai_agent.nagar` (`src/nexus_ai_agent/nagar/`)
**Machine artifact:** `OPERATION_TRUTH.json` → generated pages: `OPERATION_CONTRACT_MATRIX.md`,
`L0_L4_MATURITY.md`, `RECONCILIATION.md`, `OPERATION_WAVE_PLAN.md`
**Mutation evidence:** `OPERATION_TRUTH_MUTATIONS_2026-09-25.md`

> **Purpose of this report:** after this change, no agent can claim an operation or an
> operation-set is "implemented" by citing a Markdown page or a registry snapshot. The
> claim must survive a fresh recomputation (`--check`), the structural rules
> (`invariants`), byte-exact regeneration of every generated page, and the adversarial
> mutation probes. Everything below is *computed or measured*; where a number could not
> be measured, it is recorded as `NOT_AVAILABLE`, not estimated.

---

## 1. LIVE TRUTH

| Fact | Value (measured 2026-09-25) |
|---|---|
| GitHub identity | authenticated via the sandbox's configured GitHub integration (user endpoint returns HTTP 403 to this token — identity by repo access, not profile read) |
| Repository | `bot523h/nexus-ai-agent` (public), remote `https://github.com/bot523h/nexus-ai-agent.git` |
| Default branch | `main` |
| Live main SHA | `fe95cf0fdb03742016655e012f9397fda1e46fd7` = merge of PR #79 |
| Session branch | `arena/01a0d708-nexus-ai-agent`, branched from `fe95cf0`, pushed |
| Open PRs | 19 (18 OPEN + 1 DRAFT: #58) |
| Recent merged PRs | #79, #77, #76, #72, #65, #62, #61, #55, #54, #53, #52, #51, #50, #49, #48, #47, #46, #45, #44, #43 |
| Remote branches | only `main` was fetched at session start; PR heads exist on the remote (`arena/*`), leases recorded on the board |
| Active leases (board) | `task-179-gate2-command-reconciliation` (zone `nagar-contract-gate`, **gates_owner=true**, owner `arena/01a0d43c`, claimed 09-24T16:32Z, TTL 48h → live until 09-26T16:32Z, fences `docs/README.md`, `docs/DECISION_LOG.md`, `docs/architecture/**`, `creative/studio/**`); `coordination` (active_in_review); `security-boundary` (active_in_review); `task-190-operation-truth` (mine) |
| AGENTS.md | protocol v2: claim-before-you-code, one owner per file-zone, branch = identity, one gates owner, 24 h leases; evidence rules require reproducible numbers — this change implements that rule for operations |

### Operation-Truth-related PRs, classified

| PR | Title | State | Classification |
|---|---|---|---|
| #70 | Operation Matrix + Evidence Gate (70↔57, 23 gaps) | OPEN, **CONFLICTING** (stale base) | **SUPERSEDED as authority / SOURCE OF EVIDENCE.** Its `OPERATION_MATRIX.json` is hand-written: `maturity_level`, `runtime_proven`, `tested` columns are not recomputed from anything (measurement during Gate 2.2 showed deleting a catalogue row or rewriting every `L4` to `L0` left its guard green). |
| #73 | Gate 2.2 — Operation Truth → Executable Evidence Gate | OPEN, **CONFLICTING** | **SOURCE OF EVIDENCE, audited.** Design adopted (sources → recompute → projection; mutation probes; eight evidence layers; refusal to emit L4). Not mergeable: hard-depends on `docs/audits/GATE4_TRUTH_MATRIX.json`, which is absent from main, and edits CI + fenced docs. Its lessons are implemented here against live main. |
| #68 | Nagar Gate 2: versioned command + capability boundary | OPEN, **CONFLICTING** | **SOURCE OF EVIDENCE** for contract semantics; its L0–L4 wording conflicts with #70's (see §7). Contract text itself landed on main via **#72 (MERGED)**. |
| #69 | Gate 4: cross-layer vertical slice + Truth Matrix | OPEN, **CONFLICTING** | SOURCE OF EVIDENCE for the `GATE4_TRUTH_MATRIX.json` evidence shape; the engine consumes that file *when present* and reports its absence honestly. |
| #74 | Verification closure + registry ratchet + cross-PR truth gate | OPEN, **CONFLICTING** | Adjacent (verification/verifier ownership); not a merge candidate for this work. |
| #72 | Gate 2: canonical command + capability contract (task-179, D-0013) | **MERGED** | The binding contract authority on main (`docs/architecture/COMMAND_CAPABILITY_CONTRACT.md`, ADR 0005). |
| #77 | Gate 2 × lifecycle seam (task-183) | **MERGED** | Pack lifecycle gate wired into dispatch (stage 4b). |

No PR was blindly updated. This session's vehicle is a **new branch + new PR** against
live main, exactly as required.

---

## 2. CURRENT OPERATION UNIVERSE (recomputed, never assumed)

From `OPERATION_TRUTH.json` (every figure reproducible via `python -m nexus_ai_agent.nagar --check`):

| Quantity | Value | Rule |
|---|---:|---|
| Product catalogue | **70** | rows of the seven pack tables in `docs/NAGAR_70_OPERATIONS_TDD.md` (strictly table-scoped; prose ids excluded; duplicates refused) |
| Runtime registry | **57** | `build_runtime_registry()` live measurement |
| Overlap | **47** | catalogue ∩ registry |
| Missing | **23** | catalogue − registry (portrait 10, scene 10, color 3) |
| Runtime-only | **10** | registry − catalogue (slideshow 6, media 2, system 1, timeline.mark 1) |
| Universe | **80** | catalogue ∪ registry |
| Surface-reachable | **12** | 7 worker-mapped (`/edit` trim/speed/reverse, `/caption` transcribe, `/grade` exposure/proxy/otio) + 5 `/slideshow` dispatch sites |
| Declared symbols | **57** | studio `operation_id=` literals ∪ pack `OPERATION_*` constants — **parity with the registry is empty in both directions** |
| Suite-executed (proof) | **57 / 57** | AST probe: id in a test module that *calls* `.dispatch`/`.handler`/slideshow-service entrypoints |
| Render-lane branches | **7** | `canonical_id == …` branches in `creative/render_jobs.py` |
| Registered commands | **4** | `edit`, `caption`, `grade` (creative handler builder) + `slideshow` (`bot/handlers.py` `CommandHandler("slideshow", …)`) |

If an older report said something else, **this report supersedes it**: the numbers were
re-derived from live main, not copied from PR #70/#73 projections.

---

## 3. PRODUCT vs REGISTRY vs SURFACE (three independent sources)

* **Source A — Product contract (TDD):** 70 rows × (id, Input→Output, engine cell, permission
  level A/B/C, pack id measured from the enclosing section header). The document itself
  states it is a *design* ("نه ادعای پیاده‌سازی‌شدن همهٔ ۷۰ موتور") — the engine treats it as
  contract-only evidence: `contract = PARTIAL` until a machine contract (registered
  `OperationSpec`) exists.
* **Source B — Live runtime registry:** 57 operations with per-op measured facts
  (`extra=forbid` input model, named non-lambda handler, `deterministic=True`, registrar).
  Composition issues: none (`composition_issues() == ()`).
* **Source C — Executable surface:** three probes that must agree —
  `CreativeSurfaceMapper.ALLOWED`, `SURFACE_TO_CANONICAL` (worker), slideshow service
  dispatch sites — plus command registration. **They agree; drift findings = none.**
* **Source D — Declarations:** bidirectional parity `declared == registered == 57` (empty
  both ways) — the anti-fake-registration guard.
* **Source E — Proof:** suite-execution AST probe (57/57) + append-only
  `docs/audits/OPERATION_PROOF_REGISTRY.json` (schema-validated; currently **0 recorded
  proofs — honest absence**, consumed when a harness records entries) + optional
  `GATE4_TRUTH_MATRIX.json` (absent on main; consumed as-is if PR #69-style evidence lands).

No single source is authoritative: an operation is only "real" through the chain
PRODUCT → CONTRACT → REGISTRY → CAPABILITY → SURFACE → COMMAND → EXECUTION → ARTIFACT → PROOF.

---

## 4. THE MATRIX (70-op pipeline)

Full table: **`OPERATION_CONTRACT_MATRIX.md`** (generated; hand edits turn the gate red).
Stage distribution over the 80-op universe:

| Stage | PROVEN | PRESENT | PARTIAL | MISSING |
|---|---:|---:|---:|---:|
| product | — | 70 | 0 | 10 |
| contract | — | 57 | 23 | 0 |
| registry | — | 57 | 0 | 23 |
| capability | — | 57 | 0 | 23 |
| surface | — | 12 | 0 | 68 |
| command | — | 12 | 0 | 68 |
| execution | 12 | 0 | 0 | 68 |
| artifact | 0 | 9 | 3 | 68 |
| proof | 57 | 0 | 0 | 23 |

Notes:
* `artifact` PROVEN = 0 because **no recorded artifact proof exists on main** — the three
  PARTIAL rows (`timeline.reverse_segment`, `delivery.make_proxy_480p`,
  `delivery.export_otio`) have a materialization path but no recorded artifact proof; the
  9 PRESENT rows are state-revision producers with a live path.
* `execution` MISSING for 68 = no *production* call site reaches them (most registered ops
  have no surface yet); their suite execution is recorded under `proof`, deliberately a
  different layer.
* Status flags: 7 OPERATIONAL (catalogue ∧ L3), 63 REGISTERED, 10 RUNTIME_ONLY,
  23 MISSING.

---

## 5. MISSING SET (canonical, 23 operations)

Machine-computed in `OPERATION_TRUTH.json → missing_set` (catalogue − registry); a
documentation edit cannot remove an entry (probe #12 + membership invariants). Per-op
fields: id, capability/pack, reason, dependency class, engine evidence, execution lane,
test requirement, pack requirement, locality, security, complexity, dependency edges, wave.
Summary by family:

| Family | Pack (measured) | Count | Dependency class |
|---|---|---:|---|
| color | `nexus.color.delivery` (**ships today**) | 3 | operation_registration (pack exists, op not registered) |
| portrait | `nexus.vision.portrait` (**absent**) | 10 | 1 pack+model primitives, 8 model-dependent, 1 non-model blocked on primitives |
| scene | `nexus.vision.scene` (**absent**) | 10 | 2 pack-only (detect_shot_boundaries, find_subject_moment… measured non-model), rest model-dependent |

Security: levels come from the catalogue cells (portrait `correct_gaze` = **C**;
scene `remove_logo` = C after measurement — see `missing_set[*].permission_level`);
portrait rows carry the identity-locality note; level C rows carry the confirmation note.

---

## 6. L0–L4 MATRIX

Canonical ladder — one definition, owned by `truth.py::LADDER`, rendered verbatim into
`L0_L4_MATURITY.md`, fingerprinted in the projection (rewrite ⇒ gate red):

* **L0 design** — catalogue row or registered spec
* **L1 code** — registered + domain-ready (typed, named, deterministic)
* **L2 reachable** — a live user-facing entrypoint names it
* **L3 operational** — L2 **and** executable proof
* **L4 production-like** — L3 **and** artifact proof **and** a production-like measurement
  ⇒ **no source measures production-like ⇒ L4 is refused for every operation, with the
  reason recorded** (`l4_unavailable_reason`)

Distribution (recomputed): **L0 = 23**, **L1 = 45**, **L2 = 0** (every surface-reachable op
is also suite-executed, so it passes straight to L3), **L3 = 12**, **L4 = 0**.

L3 set: `timeline.trim`, `timeline.speed_ramp`, `timeline.reverse_segment`,
`caption.transcribe`, `color.adjust_exposure`, `delivery.make_proxy_480p`,
`delivery.export_otio`, `slideshow.scan_assets`, `slideshow.score_images`,
`slideshow.suggest_tone`, `slideshow.compose`, `slideshow.render`.

---

## 7. DRIFT FINDINGS

**On live main: zero behavioural drift.** `contract_drift` contains only the ladder
fingerprint record. Surface probes agree; declaration parity is empty; composition is
clean.

Historical/claimed drift handled by reconciliation, not silence:

| Claim | Where | Disposition |
|---|---|---|
| Hand-written `maturity_level`/`runtime_proven` columns (e.g. L3 asserted per op) | PR #70 `OPERATION_MATRIX.json` | **SUPERSEDED** — replaced by recomputed pipeline + maturity; the old columns were mutation-dead (green under deletion/fabrication) |
| Two incompatible L0–L4 definitions (L2 reachable vs L4 surface-reachable) | PR #68 vs PR #70 | **RESOLVED** — single canonical ladder in `truth.py::LADDER`; `ladder_fingerprint` + byte-exact docs enforce it; competing ladders now go red |
| `L4 = production-like` | any doc | **RETRACTED as achievable today** — recorded `NOT_AVAILABLE` with reason; no op may carry it |
| "71 operations" phrasing | test prose (`test_opgap_wave5.py` docstring) | Noted: the *measured* catalogue is 70 rows; prose numbers elsewhere must follow the recompute rule (AGENTS.md §2) |
| Older counts in PR projections (70/57/47/23/10/80) | PR #70/#73 artifacts | **Confirmed** by independent recomputation on live main — but they are now *outputs of a gate*, not inputs of a claim |

---

## 8. MUTATION EVIDENCE

Full log: **`OPERATION_TRUTH_MUTATIONS_2026-09-25.md`**. 23 probes, all
GREEN → MUTANT → RED → RESTORE → GREEN, covering every error class the mission lists:
hard-coded counts (static AST scan + probes 1–2), fake registration (2), docs count drift
(19–22), runtime-only hidden (15), missing marked implemented (12), L4 without proof (13),
stale matrix (18 + freshness), duplicated id (8, 16), capability mismatch (3, 4), surface
without contract (4, 5), contract without implementation (6, 7), implementation without
registry (1), registry without executable evidence (10, 11), renamed stale id (7).

```text
pytest tests/unit/test_operation_truth_mutations.py -v   → 23 passed
pytest tests/architecture/… + tests/unit/…truth_sources… → 35 passed
python -m nexus_ai_agent.nagar --check                   → TRUTH GATE: GREEN
```

---

## 9. EXTERNAL GITHUB RESEARCH

### 9.1 Truth/registry/contract-drift corpus (Phase 1, ≥5)

| # | URL / repo | commit-ref (HEAD at 2026-09-25) | Date | Claim | Why relevant to Nexus | Limitation |
|---|---|---|---|---|---|---|
| 1 | https://github.com/open-telemetry/weaver | `2ff5ed95c554` | 2026-09-23 | Registry-first tooling: `check/resolve/diff/generate/update-markdown/live-check` over one schema; docs are generated from the registry | Exactly the JSON⇒Markdown one-way projection used here (`--docs`); `registry diff` ≈ my `compare` | Telemetry semantics, not operations; no maturity ladder |
| 2 | https://github.com/open-telemetry/semantic-conventions (+ `open-telemetry/opentelemetry-go-compile-instrumentation` local-registry pattern) | `838e414e11e3` | 2026-09-22 | Machine-readable registry is the source; **CI fails when committed generated output differs from the model**; naming/stability/back-compat checks in CI | My docs byte-equality gate + fingerprint + no-hard-coded-counts scan are the same discipline | Their back-compat checker needs released versions; my ops have no release history |
| 3 | https://github.com/backstage/backstage (`docs/features/software-catalog/descriptor-format.md`) | `b9c9dd2dddfe` | 2026-09-24 | Descriptor YAML with `kind/apiVersion/spec`, relations (`dependsOn`, `providesApis`), lifecycle as data | Capability registry shape: identity + relations as *data*, dependency edges computed from declared relations (my type-token edges) | Catalog describes components, not executable evidence of operations |
| 4 | https://github.com/in-toto/in-toto (+ slsa-framework/slsa `618f5b2192aa`) | `e352b43ad7cb` | 2026-08-27 | Link metadata per step: materials/products hashes; **"products of one step are the materials of the next"**; SLSA provenance as predicate | Proof registry entries (operation → method → revision → artifact) are an in-toto-style chain; the reopen-digest idea from PR #73 matches | Requires signing infrastructure we do not have; mine is unsigned recorded evidence |
| 5 | https://github.com/xregistry/spec (CloudEvents registry/schema groups) | `989efc6cd81f` | 2026-09-23 | Versioned schema registry: ids, versions, groups, counts as registry facts | Versioned command schemas (`nagar.command.v1`, per-op `operation_schema_version` in the Gate-2 contract) align with registry-versioned groups | Spec-level; no evidence-chain concept |
| 6 | https://github.com/ahungry/golden-master + understandlegacycode.com characterization/approval-tests write-ups (secondary) | repo HEAD unverified | 2021– | Golden master / characterization / approval tests capture *actual* behaviour to detect drift | My byte-exact generated-page tests are approval tests over the projection | Blog-tier secondary evidence; snapshots must be re-approved consciously (mine auto-regenerate from sources instead) |

### 9.2 Product-family research (Phase 8) — what repeated, what's contract-only

Depth: single-pass GitHub/docs scans (depth-1); families below with fewer than five
deep-read sources are marked — honesty over inflation.

| Family | Sources examined (official/mature first) | Recurring primitives | Contract-without-execution pattern | Test practice | Local vs model |
|---|---|---|---|---|---|
| timeline/edit | AcademySoftwareFoundation/OpenTimelineIO (architecture docs), kkroening/ffmpeg-python, Zulko/moviepy (via lists), FFmpeg filter docs | Timeline→Track→Clip→MediaReference, non-destructive source_range, typed time (RationalTime) | Adapters/plugins declared but lazy; API surface ≫ executed paths | Round-trip serialization equivalence (`write→read→assertEquivalent`) | **Local-first** (OTIO pure; FFmpeg master lane) |
| image/portrait | Remove-Background-ai org (rembg, rembg.js, rembg-webgpu — official), google mediapipe (mentioned, **<5 deep sources**) | landmark → mask → retouch chain; model card + hash | WebGPU/WASM ports declared before parity (rembg-webgpu capability probing) | Model-free unit tests + fixture images | **Model packs** (download-on-first-use, offline after) |
| scene/composition | Breakthrough/PySceneDetect v0.7.1 (official, incl. PyPI provenance attestation), FFmpeg `scdet`/scene filter | shot-boundary detectors (content/hash/histogram/threshold) are **classical, model-free**; segmentation/inpainting are model | CLIs split video (FFmpeg) while analysis APIs stay optional | Detector fixtures + threshold tables | detect_shot_boundaries **local/no-model**; remove/inpaint **model** |
| color | AcademySoftwareFoundation/OpenColorIO (+ config repos), FFmpeg zscale/tonemap | config-driven LUTs, explicit color space/transfer/range | Config compatibility promises across majors | Golden LUT fixtures | **Local** (GPU/native) |
| audio | librosa/librosa, facebookresearch/audiocraft, spotify/pedalboard (via curated awesome-lists — secondary) | beats/onset → grid → sync; loudness EBU-R128; stems | Libraries ship analysis while *integration* into editors stays undone | Deterministic DSP fixtures (fixed seed) | **Local** DSP; heavy separation models optional |
| subtitles | m-bain/whisperX (cited by the repo TDD), libass/libass (+ forks), Whisper forced-alignment docs | transcript → word timings → SRT/ASS → styled burn-in | WhisperX itself documented as **not** browser-runnable ⇒ worker/process reality check | Byte-identical golden SRT/VTT (repo already has `test_golden_srt_formatting_is_byte_identical`) | **LOCAL_PREFERRED** (local whisper; optional extras) |
| slideshow | repo's own slideshow lane + moviepy/ffmpeg-python patterns | scan → score → tone → compose → render, template library as JSON | Templates declared as JSON contract; render needs FFmpeg present (docker gap found earlier: PR #61) | Synthesized-media fixtures (`slideshow_media.py`) — no binary media in git | **Local** (FFmpeg) |
| AI-assisted workflows | openai/openai-agents-python (`function_schema`, `strict_json_schema=True` default), docs.langchain.com structured-output (ProviderStrategy/ToolStrategy), OpenAI strict tool schemas | typed JSON-schema tools, strict mode, approval/guardrail hooks | Schemas are declared broadly; execution gated by `needs_approval`/guardrails | Schema-validity + selection evals need fixed datasets (**METRIC UNPROVEN without one**) | schema local; model inference provider-bound |

**Cross-family answers the mission asks for:** (a) primitives that repeat: typed
clip/time/refs, non-destructive revisions, beat/transcript/landmark *sets* as shared
currencies; (b) contract-without-execution: lazy packs, WebGPU parity claims, adapter
registries; (c) golden/fixture discipline: byte-identical small artifacts (SRT, LUT,
template JSON) rather than whole videos; (d) local-first: timeline/color/audio/slideshow;
(e) model/pack needed: portrait, scene segmentation/inpainting, speech alignment.

---

## 10. DEPENDENCY GRAPH

31 type-level edges computed from the catalogue's Input→Output cells
(`OPERATION_WAVE_PLAN.md`, `dependency_edges` in the JSON). Backbone:

* `portrait.detect_landmarks → {smooth_skin, relight_face, stabilize_face, correct_gaze, enhance_eyes, mask_hair}` via `FaceTrackSet`
* `scene.track_face →` same portrait family (shared `FaceTrackSet`) — cross-family edge, measured not curated
* `scene.segment_subject → {apply_mask, smooth_skin}` via `MaskRef`; `scene.track_object → scene.remove_object` via `ObjectTrack`
* `caption.transcribe → {align_words, translate_local, search_transcript, generate_ass_rtl}`; `align_words → {generate_srt, highlight_words}`; `style_vazirmatn → burn_in`
* `audio.detect_beats → {beat_sync_cut, align_music, retime_to_music}` via `BeatGrid`
* `timeline.retime_to_music → timeline.speed_ramp` via `RateCurve`; `motion.stabilize → keyframe_transform` via `TransformCurve`

Renaming a type rewrites the graph — nothing is hand-curated (probe: type rename ⇒ docs/JSON drift).

---

## 11. WAVES (engineering order — explicitly NOT product priority)

Computed per missing op (`missing_set[*].wave`); rules and full tables in
**`OPERATION_WAVE_PLAN.md`**. Universal prerequisite chain per wave:
`contract → runtime → verifier → surface → E2E` (with recorded proof to graduate).

| Wave | n | Members | Prerequisite / blocker |
|---|---:|---|---|
| **NOW** | 3 | `color.white_balance`, `color.hdr_tonemap`, `color.deband_denoise` | none — pack ships, no model, no missing dependency |
| **NEXT** | 4 | `scene.detect_shot_boundaries`, `scene.find_subject_moment`, `scene.track_object`, `portrait.background_blur`* | pack manifests must ship first (*`background_blur` engine cell reads mask-based; measured `model=False` — recorded with engine evidence; revisit if implementation proves otherwise*) |
| **AFTER VERTICAL SLICE** | 5 | `portrait.detect_landmarks`, `portrait.mask_hair`, `scene.segment_subject`, `scene.track_face`, `portrait.enhance_eyes` | model lane (fetch→hash→license→cache→offline) + primitive slice proven E2E; `enhance_eyes` blocked on primitives although its own engine is non-model |
| **POST-MVP** | 9 | portrait `relight/retouch/smooth/stabilize/whiten` + scene `auto_reframe/remove_background/remove_object/replace_sky` | family primitives done; level < C |
| **LATER** | 2 | `portrait.correct_gaze` (C), `scene.remove_logo` (C) | model lane **and** confirmation contract proven |

`IMPLEMENTATION-BLOCKED` entries are marked in the wave table with their named blocker —
they cannot be pulled earlier by a documentation edit (membership is recomputed).

---

## 12. FILES CHANGED

New (all in my zone `operation-truth`):
* `src/nexus_ai_agent/nagar/{__init__,sources,truth,docs_projection,__main__}.py` — engine
* `OPERATION_TRUTH.json` — generated projection (only via the generator)
* `OPERATION_CONTRACT_MATRIX.md`, `L0_L4_MATURITY.md`, `RECONCILIATION.md`, `OPERATION_WAVE_PLAN.md` — generated pages (byte-exact checked)
* `docs/audits/OPERATION_PROOF_REGISTRY.json` — append-only proof contract (0 entries, honest)
* `tests/architecture/test_operation_truth_gate.py` (18 tests), `tests/unit/test_operation_truth_sources.py` (17), `tests/unit/test_operation_truth_mutations.py` (23)
* `OPERATION_TRUTH_AUDIT_2026-09-25.md` (this file), `OPERATION_TRUTH_MUTATIONS_2026-09-25.md`
* `.agents/board.json` — minimal: new zone `operation-truth` + claim `task-190` (protocol-required)

**Not touched (per ownership lock):** security (S3/S5), Gate-5 lifecycle, backup/restore,
creative rendering/FFmpeg/pack/job-queue/API/Telegram/memory implementations, database
migrations, `.github/workflows/`, `AGENTS.md`, `docs/README.md`, `docs/DECISION_LOG.md`,
ADR directory. Generated pages live at the **repo root** (next to `OPERATION_TRUTH.json`,
consistent with `REQUIREMENTS_LEDGER.md`/`ROADMAP_STATUS.md`) because `docs/README.md` is
leased — see §14.

## 13. CI

* `.github/workflows/` is outside my ownership — **no workflow edits**.
* Local gate reproduction (all green here): `ruff check .` ✓, `ruff format --check .` ✓,
  `mypy src` ✓ (239 files), `pytest tests/unit tests/architecture` ✓ (2062+51),
  `pytest tests/integration` ✓ (81 passed, 19 skipped), `python -m nexus_ai_agent.nagar --check` ✓.
* The PR will run the existing CI; **gates execution deferred to the gates owner**
  (`task-179…`, `gates_owner=true`) per AGENTS.md rule 4.
* Recommended (for the gates owner, not done by me): add
  `python -m nexus_ai_agent.nagar --check` as a CI step — one command, no new deps.

## 14. OPEN QUESTIONS

1. **Index rows for the five new documents in `docs/README.md`** — referee check returns
   exit 1 (`docs/README.md` + `docs/DECISION_LOG.md` leased by
   `task-179-gate2-command-reconciliation` until 2026-09-26T16:32Z). Deferred, not
   breached: add four index rows + one DECISION_LOG entry (D-00xx: "operation truth is
   recomputed, docs are projections") when the lease frees. *This is why the generated
   pages sit at the repo root — moving them under `docs/` before the lease frees would
   turn `test_docs_integrity` red.*
2. **Recorded artifact proofs**: `OPERATION_PROOF_REGISTRY.json` is empty by design; the
   first honest entries should come from an execution harness (Gate-4-style run on CI)
   recording `runtime_execution`/`artifact_evidence` with method + revision. Until then
   no artifact PROVEN and no L3 graduation beyond suite execution.
3. **`GATE4_TRUTH_MATRIX.json`**: engine consumes it if PR #69-style evidence lands;
   until then `gate4_evidence.present=false` is visible in the projection.
4. **Family research depth**: portrait and AI-workflow families have <5 deep-read GitHub
   sources (marked in §9.2) — next owner should deepen before any *product* decisions.
5. **`slideshow.upscale` surface reachability**: dispatched by `slideshow/upscale.py`
   (bus) but not parsed from `service.py` — if a `/slideshow upscale` entrypoint exists
   through another handler, the probe set should be extended deliberately.
6. **Wave NEXT × `portrait.background_blur`**: engine cell measured as non-model; if the
   implementation needs matting, re-run the recompute after editing the catalogue cell —
   the wave assignment will move itself.

## 15. EXACT NEXT OWNER

| Work | Owner |
|---|---|
| Run full gates on the PR, add `nagar --check` CI step, release `task-179` lease | **gates owner**: `task-179-gate2-command-reconciliation` (`arena/01a0d43c-nexus-ai-agent`) |
| Index rows in `docs/README.md` + one `DECISION_LOG` entry (after lease release) | docs-architecture/gates owner (referee: `python scripts/agent_board.py check --files "docs/README.md" …`) |
| First recorded proof entries (execution harness) | verification-closure owner (PR #74 lineage) or gates owner |
| NOW wave implementation (color ×3) | `nagar-creative-edit` / color lane owner — **implementation, not truth** |
| NEXT wave (scene pack first shipments) | `nagar-scene` queued task owner |
| AFTER-VERTICAL-SLICE (model lane + primitives) | `nagar-portrait` queued task owner + pack/model infra owner |
| This zone (`operation-truth`) | `arena/01a0d708-nexus-ai-agent` (lease renewed; extend via `agent_board.py claim … --branch …`) |

---

# TRUTH GATE = GREEN

*Measured by `python -m nexus_ai_agent.nagar --check` on 2026-09-25: stored projection ==
fresh recomputation, all structural invariants hold, all generated pages byte-identical,
58 truth tests + 23 mutation probes green, repo-wide ruff/mypy/test suites green.
PARTIAL items are the six open questions above (all lease- or evidence-availability
issues, none inside this zone); nothing in the Operation Truth, Matrix, reconciliation,
maturity, or wave-planning scope is blocked.*
