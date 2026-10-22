# Nagar OSS Arsenal — Wave A: Sequenced Implementation Plan (Waves B–F)

> **Companion to:** [ARSENAL_WAVE_A_CENSUS_2026-10-22.md](ARSENAL_WAVE_A_CENSUS_2026-10-22.md) — read that first; it carries the evidence.
> **Base `origin/main`:** `04469ed0f46fdc66752cdea36d25ce2145db3f1e` · **Date:** 2026-10-22
> **Status of this document:** a **plan**, not a claim of work done. Nothing in Waves B–F has been
> implemented. No source, test, manifest or `.agents/board.json` entry was modified.

---

## 1. The design decision that makes this cheap

The census found that this repository has **already solved the exact architectural problem** the
mission poses in §4 ("one registry, multiple controlled execution modes"). It is the caption
engine pattern, and it is wired, tested and CI-matrixed today:

```
application/ports/caption_engine.py      CaptionEnginePort (Protocol)
    async def transcribe(audio_path, *, language) -> TranscriptRef
    def is_available() -> bool

adapters/whisper_local.py                WhisperLocalCaptionEngine(CaptionEnginePort)
    lazy: importlib.import_module("faster_whisper")   ← heavy dep never imported at module load
    is_available() probes the real import, not find_spec

creative/caption/unavailable_adapter.py  UnavailableCaptionAdapter
    fails CLOSED with a typed, translated error — never a silent stub

creative/render_jobs.py:375              _default_caption_engine()
    try the real engine; on ANY failure fall back to Unavailable…
    composition root does the choosing; the pack never sees the dependency
```

**Why this matters:** it satisfies every hard constraint at once —

| Constraint | How the pattern satisfies it |
|---|---|
| **R7** — packs import only stdlib/pydantic/studio/packs | The heavy dependency lives in `adapters/`, never in `creative/packs/**`. |
| **R1/R5** — ports never import adapters; typed async surface | `Protocol` in `application/ports/`, implementation in `adapters/`. |
| **Mission §7** — missing model/credentials must not stop startup | `is_available()` + typed-unavailable fallback. |
| **`deptry`** — every import declared | Declared in an optional extra, probed lazily (the `DEP001` ignore pattern already used for `nacl`/`gtts`/`mega`). |
| **`extras_matrix.py`** — every extra gets a CI leg + smoke test | `[speech]` already proves the leg works; a new extra copies it. |
| **R20** — no orphan modules | Wired at a composition root (`render_jobs.py`), so reachable by construction. |

> **Rule for every wave below: copy this pattern; do not invent a second one.**
> The mission's "external tools/extensions: separately managed processes or services, invoked
> via explicitly registered and typed operations" is *this*, with the adapter on the other side
> of a process boundary instead of an import boundary.

---

## 2. Wave B — the first vertical slice

The mission says: *"The likely candidates are scene detection, timeline interchange or
completion of an existing speech integration — but choose based on the actual code census."*

The census answers: speech is **already complete and wired** (priority #3 is done). That leaves
two, and they are not the same size. So Wave B is split.

### Wave B1 — OTIO interop proof + resolve the dead `include_markers` flag

**Why first:** smallest, zero runtime risk, closes a *live defect* (census F-2), and the new
dependency has **0 core deps**.

| Item | Value |
|---|---|
| Dependency added | `opentimelineio>=0.18.1` — Apache-2.0, `requires_python >=3.9`, **0 core deps** |
| Placement | **`dev` extra** (verification only). It is *not* needed at runtime: `_export_otio()` already produces OTIO JSON without it. This keeps the production dependency graph unchanged. |
| New file | `tests/unit/test_otio_interop.py` — the file the board already names in two zones but which **does not exist** |
| Test content | Round-trip the real handler output through `opentimelineio.api` / `from_json_string`: assert the `Timeline.1` parses, track kinds survive, and every clip's frame count equals the `temporal_conversion.emitted_frames` the handler recorded. Assert lossless cases report `lossless: true`. |
| Second test | A **mutation probe**: corrupt one emitted frame count and assert the round-trip test fails. Without this, the test proves nothing (the repo's own standard — 13 existing `scripts/*_mutations.py`). |
| The dead flag | Owner decision **Q5**: either (a) implement `Marker` emission = board **task-121** (`OtioMarker`, `Marker.2` on `Stack`, point→1 frame, colour palette in metadata), or (b) **remove** `include_markers` from `ExportOtioInput`. Both are defensible; shipping neither is not, because a declared input that does nothing violates the repo's honesty standard (law R16, D-0012). |
| Governance caveat | `task-121` is recorded `available_sequenced_post_33` — prerequisite PR#33 (Sept). PR#33 is **not in the current open-PR list**, so the prerequisite appears satisfied-or-abandoned. **Verify with the owner before claiming** (census G-2: the board's sequencing fields are stale). |
| Explicitly out of scope | No change to `_export_otio()`'s output bytes except (optionally) marker emission. No new pack, no new capability id. |

**Acceptance criteria (B1)**
1. `tests/unit/test_otio_interop.py` exists and round-trips the *real handler output* through the real `opentimelineio` library — not a hand-written JSON fixture.
2. A mutation probe demonstrates the test fails when a frame count is corrupted.
3. `include_markers` is either implemented with a round-trip test, or removed. No dead flag remains.
4. `opentimelineio` appears only in the `dev` extra; `[project.dependencies]` is unchanged; `deptry` and `extras_matrix.py check` still pass.
5. `THIRD_PARTY_NOTICES` (new) records OpenTimelineIO: upstream URL, tested version 0.18.1, Apache-2.0, attribution text, "verification-only, not shipped in the runtime image".

### Wave B2 — scene detection: implement the already-specified `scene.detect_shot_boundaries`

**Why this is the high-value slice:** it is the *only* candidate that fills a capability the TDD
ledger already specifies with a typed contract (census §4), it is absent from the tree
(0 files), and PySceneDetect is BSD-3-Clause with a compatible Python floor.

**Adoption shape — mirrors the caption precedent exactly:**

```
pyproject.toml
  [project.optional-dependencies]
  scene = ["scenedetect>=0.7.2,<0.8"]        # BSD-3-Clause; brings opencv-python + numpy
                                              # (numpy already a core dep)

application/ports/scene_engine.py             SceneEnginePort (Protocol)
  async def detect_shot_boundaries(video_path, *, threshold, min_scene_len) -> ShotBoundarySet
  def is_available() -> bool

adapters/scenedetect_local.py                 PySceneDetectEngine(SceneEnginePort)
  lazy importlib.import_module("scenedetect")  ← never at module import
  runs the detection in a thread/process with a hard timeout
  returns measured boundaries + evidence (frame indices, timecodes, detector, threshold used)

creative/scene/unavailable_adapter.py         UnavailableSceneAdapter
  typed, translated failure — fail CLOSED

creative/packs/scene/models.py                ShotBoundarySet (pydantic, extra="forbid")
  pure data: boundary frame/timecode, confidence, detector id, evidence refs
creative/packs/scene/operations.py            PURE handler (R7-clean: stdlib+pydantic+studio)
  consumes an already-measured ShotBoundarySet, produces the timeline-side result
creative/packs/scene/pack.manifest.json       nexus.vision.scene, capabilities=["scene.detect_shot_boundaries"]

creative/render_jobs.py (or a new composition point)
  _default_scene_engine()  ← try real, fall back unavailable
```

**Critical correctness note.** `scene.detect_shot_boundaries` is specified as
`ClipRef + threshold → ShotBoundarySet`. The *measurement* needs OpenCV; the *pack handler*
must not import it (R7's allowlist is 14 modules and `cv2` is not among them). So the split is:
adapter measures → `ShotBoundarySet` crosses the port → pure handler consumes. This is precisely
how caption works (`WhisperLocalCaptionEngine` measures, the caption pack stays pure). Getting
this split wrong is the single most likely way to fail CI on this wave.

**Dependency-footprint honesty (measured, not estimated):**
- `scenedetect 0.7.2` declares 5 core deps: `click~=8.0`, `numpy`, `opencv-python`,
  `platformdirs`, `tqdm`. `numpy>=1.26` is already a core dependency, so the net-new are
  `click`, `opencv-python`, `platformdirs`, `tqdm`.
- **Action required in the PR:** prefer **`opencv-python-headless`** over `opencv-python` for a
  server/headless deployment (no GUI libraries). This must be an explicit, recorded decision
  because `scenedetect` pins `opencv-python`; overriding it needs a constraint or an
  install-time substitution, and `deptry` will notice. **Do not discover this in CI.**
- Install size must be **measured and recorded** in the PR (`pip install --dry-run` +
  `du -sh` of the resulting site-packages delta), per the mission §11 item 8. Not estimated.

**Acceptance criteria (B2)**
1. `[scene]` extra declared; `scripts/extras_matrix.py check` passes **after** adding the CI leg and the smoke test (the script fails closed on a missing leg — that is the point).
2. `SceneEnginePort` added to `application/ports/` **and** to `tests/architecture/test_port_signatures.py` (the test *is* the API contract, per `MODULE_MAP.md §5`).
3. `adapters/scenedetect_local.py` degrades with a typed error when the extra is absent; a unit test asserts the unavailable path on the core leg (mirroring `test_optional_extras.py:213` for `faster_whisper`).
4. `nexus.vision.scene` manifest validates; `scene.detect_shot_boundaries` is registered in the runtime composition; `nexus packs list` reports **pending=0** for it and `activate` succeeds (law R8 + `test_pack_activation_completeness.py`).
5. `tests/architecture/test_scene_pack_boundary.py` added, asserting the pack imports nothing outside the R7 allowlist (copy `test_audio_pack_boundary.py`).
6. **A real integration test on a real file**: generate a synthetic two-shot video with FFmpeg (the repo already does this — `test_slideshow_upscale.py` runs genuine FFmpeg on a generated PNG), run detection, assert the boundary is found within a stated tolerance. A mock-only test does **not** satisfy this criterion (mission §9: "Mocks do not prove live functionality").
7. Timeout + cancellation + bounded resource test: a pathological input must not hang (mission §4 requires timeout/cancellation/cleanup).
8. Mutation probe: `scripts/scene_mutations.py` following `scripts/pack_trust_mutations.py`.
9. `THIRD_PARTY_NOTICES` records PySceneDetect 0.7.2 (BSD-3-Clause) and OpenCV (Apache-2.0) with attribution text and the tested commit/version.
10. Baseline benchmark recorded per §3 below.

**Explicitly not in Wave B2:** the other nine `nexus.vision.scene` operations
(`segment_subject`, `remove_object`, `track_object`, …). They need models/ONNX and are
Level A/B/C work of their own. Shipping one operation and leaving nine declared-but-absent
would break R8, so the manifest declares **only** `detect_shot_boundaries`.

---

## 3. Measurement protocol (mission §8) — required before any "improvement" claim

The mission forbids claiming multipliers without measured evidence. The repo agrees
(D-0012: *"Backup 'success' must be measured and round-trip-verified, never asserted"*).
So Wave B ships a **baseline harness**, not a claim.

**Evaluation set (small, reproducible, committed as fixtures or generated deterministically):**

| # | Task | Measured dimensions |
|---|---|---|
| E1 | Synthetic 2-shot clip (FFmpeg-generated) → detect boundary | boundary correctness vs. ground truth (exact, because we generated it), wall time, peak RSS |
| E2 | Long-form interview proxy (generated: alternating silence/speech) → rough-cut plan | number of cuts, output duration vs. target, wall time |
| E3 | Existing slideshow render (regression baseline) | render time, peak RSS, output checksum stability |
| E4 | OTIO export → round-trip | parse success, frame-count equality, lossless flag correctness |
| E5 | Caption/transcribe a bundled short audio (existing `[speech]` leg) | segment count, wall time, model download size |

**Baseline rule:** E3 and E5 are measured **on `origin/main` @ `04469ed` before any change**, and
the exact command, environment (`python -V`, OS, CPU count, RAM) and output are recorded in the
PR. Post-change numbers are compared against that record. If a candidate adds complexity without
moving a measured number, it is rejected — per the mission §8.

**Cost classes** recorded per the mission §7 taxonomy (local / free-software-with-hardware /
free-tier-with-quota / paid-API / unknown). No component may be labelled "free" without stating
its hardware or model-download requirement.

---

## 4. Proposed board task entries (require owner authorization — census Q2)

These are drafted to satisfy `AGENTS.md` §rule 1 and `tests/unit/test_agent_board.py`
(a task without `acceptance_criteria` + `evidence_required` cannot be claimed). They are
**proposals**; I did not write them into `.agents/board.json`.

```json
{
  "id": "task-273-otio-interop-proof",
  "priority": "P1",
  "zone": "delivery-interop-markers",
  "title": "Prove the hand-rolled OTIO export round-trips through the real library; resolve the dead include_markers flag",
  "why_fa": "export_otio امروز JSON را با مدل‌های pydantic دست‌ساز تولید می‌کند و هیچ تست round-trip با کتابخانهٔ واقعی وجود ندارد؛ فایل test_otio_interop.py که در دو zone نام برده شده، اصلاً وجود ندارد. فیلد include_markers هم هیچ اثری ندارد.",
  "acceptance_criteria": [
    "tests/unit/test_otio_interop.py round-trips the real handler output through opentimelineio.api",
    "a mutation probe proves the round-trip test fails on a corrupted frame count",
    "include_markers is implemented (task-121 scope) or removed — no dead flag remains",
    "opentimelineio is added to the dev extra only; [project.dependencies] unchanged",
    "deptry and scripts/extras_matrix.py check still pass",
    "THIRD_PARTY_NOTICES records OpenTimelineIO 0.18.1 Apache-2.0 as verification-only"
  ],
  "evidence_required": [
    "exact pytest command + observed output on the head SHA",
    "mutation-probe run output showing the killed mutant",
    "pip install --dry-run proving no runtime dependency delta"
  ],
  "exclusive_paths": [
    "tests/unit/test_otio_interop.py",
    "src/nexus_ai_agent/creative/packs/delivery/models.py",
    "src/nexus_ai_agent/creative/packs/delivery/operations.py",
    "THIRD_PARTY_NOTICES.md"
  ],
  "prerequisites": ["owner ruling on Q5 (implement vs remove include_markers)"]
}
```

```json
{
  "id": "task-274-scene-shot-boundaries",
  "priority": "P1",
  "zone": "nagar-scene-engine",
  "title": "Implement the specified scene.detect_shot_boundaries behind a port, with PySceneDetect as an optional extra",
  "why_fa": "عملیات scene.detect_shot_boundaries در TDD (خط ۳۲۱) با قرارداد típusی و سطح A مشخص شده ولی پک nexus.vision.scene هرگز پیاده نشده؛ امروز صفر فایل مرتبط با تشخیص صحنه در درخت هست.",
  "acceptance_criteria": [
    "[scene] optional extra + CI matrix leg + smoke test; scripts/extras_matrix.py check passes",
    "SceneEnginePort in application/ports/ and registered in tests/architecture/test_port_signatures.py",
    "adapters/scenedetect_local.py lazy-imports scenedetect and degrades with a typed error when absent",
    "creative/packs/scene/ imports nothing outside the R7 allowlist (new boundary gate added)",
    "nexus packs list reports pending=0 for nexus.vision.scene and activate succeeds",
    "a real FFmpeg-generated two-shot fixture yields a detected boundary within a stated tolerance",
    "timeout, cancellation and bounded-resource tests exist and pass",
    "scripts/scene_mutations.py mutation campaign runs and kills its mutants",
    "measured install-size delta and baseline benchmark recorded in the PR body"
  ],
  "evidence_required": [
    "exact pytest/ruff/mypy commands + observed output on the head SHA",
    "the real-file integration test output (not a mock)",
    "pip install size delta, measured",
    "mutation campaign output"
  ],
  "exclusive_paths": [
    "src/nexus_ai_agent/application/ports/scene_engine.py",
    "src/nexus_ai_agent/adapters/scenedetect_local.py",
    "src/nexus_ai_agent/creative/scene/",
    "src/nexus_ai_agent/creative/packs/scene/",
    "tests/unit/test_scene_engine.py",
    "tests/integration/test_scene_detection_real_file.py",
    "tests/architecture/test_scene_pack_boundary.py",
    "scripts/scene_mutations.py"
  ],
  "prerequisites": [
    "owner ruling on Q4 ([scene] extra + opencv-python-headless substitution)",
    "task-273 merged or explicitly independent"
  ]
}
```

A **new zone** `nagar-scene-engine` must be declared for task-274, because
`tests/unit/test_agent_board.py::test_exclusive_paths_belong_to_a_declared_zone` fails the board
when exclusive paths do not belong to a declared zone. The census recorded a precedent where a
missing zone declaration left the board RED.

---

## 5. Waves C–F

### Wave C — Skill registry and the first native skill pack

**Census correction that reshapes this wave:** there are **no existing skills** (a repo-wide
`find` for `*skill*`/`*prompt*` returned only my own audit document). So this is greenfield
design, and the main risk is **schema proliferation** — the repo already has two
(`nexus.capability-pack.v1`, `nexus.pack-trust-root.v1`).

**Binding design rule (owner decision Q6):** a skill manifest must **not** become a third
independent authority. Two acceptable shapes:

- **(preferred) A skill is a data record that *references* registered capability ids.** It
  declares `requires_capabilities: ["scene.detect_shot_boundaries", …]`, and validation refuses
  to load a skill whose capabilities are not registered **and active**. This reuses
  `CapabilityRegistry` as the only operation allow-list (its own docstring: *"the only operation
  allow-list"*) and makes "a skill cannot grant itself authority" mechanically true rather than
  a policy statement.
- **(rejected) A skill carries its own executable contract.** That is a second registry and it
  would eventually contradict the pack trust root.

**Hard invariants for Wave C**, each needing its own gate:
1. A skill is **data** — same law as packs. `extra="forbid"`, no `entrypoint`/`shell`/`post_install`
   keys at any depth (reuse the `test_pack_manifest_is_data_only.py` forbidden-key set).
2. A skill **cannot** widen permissions: its `requires_permissions` must be a subset of the
   union of its capabilities' grants. Enforced by a test, not by prose.
3. A skill **cannot** bypass `CommandBus`: no skill field is ever executed; skills select and
   parameterise registered operations only (law R13 — in the creative tree only `CommandBus`
   calls an operation handler).
4. **On-demand loading** (mission §6): the selector returns at most N skills for a given intent,
   and a test asserts that a prompt built for intent X does **not** contain skill Y's text.
   Otherwise context waste is guaranteed.
5. Unbacked skills are **not loadable**: a skill referencing an unregistered/inactive capability
   fails validation at load, with a typed error naming the missing capability.

**Scope discipline:** the mission suggests 20–30 skills. Start with **6–8 that are fully backed
by capabilities that exist today** (verified in census §2.1): transcription+subtitles,
loudness normalization, beat-synced cutting, OTIO export, upscale, slideshow composition,
render-spec validation, artifact-passport/evidence generation. Every one of those maps to a
*shipped* capability. The remaining categories (scene/narrative/OCR/visual-QA) unlock as their
capabilities land. **A skill without a real capability is a lie with a filename.**

### Wave D — Optional external engine adapter (Kinocut, or nothing)

Gate before any work: **all** of the following must be true, else the wave does not start.
1. The candidate's license is permissive (Kinocut: Apache-2.0 ✅; ComfyUI GPL-3.0 ❌ for core;
   Blender GPL ❌ for core; OpenMontage AGPL-3.0 ❌ blocked).
2. An owner ruling exists on adding **MCP** to the dependency graph (Kinocut is an MCP server),
   and it lands as an **optional extra with a CI leg**, never a core dependency.
3. The invocation goes through **one** legislated execution site. Law R9 currently permits
   exactly one `subprocess` site in the render lane; an external engine needs either an
   extension of that site or a **new ADR + a new boundary law** in `MODULE_MAP.md §3`. This is
   the wave's real cost, and it is architectural, not code volume.
4. Controlled workspace via the existing `tools/filesystem_policy.py` (`O_NOFOLLOW`, `dir_fd`)
   and `execution/staging.py` — **no second filesystem boundary** (the same invariant
   `task-255` states).
5. Credentials isolation: the external process must not receive the NEXUS database URL or any
   provider key it does not need. Verified by a test that inspects the spawned environment.

Deliverable if all five hold: **one** bounded workflow (e.g. "repurpose a long video into a
short vertical clip"), with typed request/response, cancellation, timeout, and independent
artifact verification through the existing Artifact Passport. Not a general bridge.

### Wave E — Extension Hub foundation

The prior audit ([EXTENSION_HUB_PROMPT_AUDIT_2026-10-22.md](EXTENSION_HUB_PROMPT_AUDIT_2026-10-22.md))
established that most of this already exists. Wave E is therefore **unlocking**, not building:

| Step | What | Why |
|---|---|---|
| E1 | **License & commercial-use gate** on `ArtifactSpec.license`: an SPDX allowlist (data), placeholder detection reusing the existing `digest_is_placeholder` idiom, a new `commercial_use: allowed\|restricted\|forbidden` field, and three new finding codes in `verify_manifest()`. Activation refuses an unlicensed artifact — same shape as the existing `PACK-SEC-001` signature gate. | Today `ArtifactSpec.license` is `Field(min_length=1)` and `verify.py` performs **no** license validation. Two shipped artifacts carry `license="model-license-reference"`, a placeholder. This is the highest-value, lowest-risk hardening available. |
| E2 | **Permission-vocabulary drift gate**: a fitness function asserting every shipped manifest declares only `KNOWN_PERMISSIONS` tokens. | Measured: 5 of 6 manifests declare 6 tokens outside the 8-token vocabulary, reported as *warnings* only. |
| E3 | **Publisher onboarding**: `nexus packs trust add-publisher` writing a *public* key into `trust_root.json`, plus a fixture publisher for an end-to-end test. | `trust_root.json` ships `"publishers": {}`, so **no external pack can activate at all today**. One command turns a built-but-unreachable subsystem into a usable path — with zero new architecture. |
| E4 | **Resource preflight at activation**: a `ResourceProbe` port measuring RAM/disk/CPU threads (`psutil` is already a core dependency), yielding `resource_state: satisfied \| insufficient \| unknown`. `insufficient` blocks; `unknown` reports honestly without blocking. | `hardware_requirements` is declared in all 6 manifests and measured **nowhere**. GPU/VRAM stays `unknown` rather than guessed — the repo already models honest unavailability (`creative/caption/unavailable_adapter.py`). |

**Wave E must not** build a marketplace UI, a container-per-extension runtime (law R4 forbids
distributed dispatch and pins compose to `{bot, dashboard}`), or a second registry.

### Wave F — Expand on evidence

Triggered only by measured user value. Candidates held open, with their gate:

| Candidate | Gate that must clear first |
|---|---|
| Tesseract OCR (Apache-2.0; 2 tiny deps) | A **named product requirement** for on-screen text, plus an owner ruling on declaring a **second external binary** beside FFmpeg. |
| OpenCV direct use (Apache-2.0) | Only for a use case the transitive `scenedetect` dependency does not already cover, and only in `adapters/` — `cv2` is banned by 9 architecture gates from packs/studio/lane/spine. |
| ComfyUI (GPL-3.0) / Blender (GPL) | Isolated-service deployment model + owner/legal ruling; never vendored into the MIT core. |
| auto-editor (Unlicense) | Upstream republishes current versions to PyPI **with real dependency metadata** (census F-3), or a native FFmpeg `silencedetect` implementation is benchmarked against it. |
| Real-ESRGAN (BSD-3 code) | A measured quality win over the shipped FFmpeg Lanczos stage **and** separate clearance of model-weight terms **and** upstream maintenance resuming (last push 2026-10-22 vs. repo 2024-08-06). |
| OpenMontage (AGPL-3.0) | **Blocked.** Requires an owner/legal theory that survives AGPL §13 for a hosted service. Absent that, ideas only — no code, no vendored skills. |

---

## 6. Governance execution protocol for every wave

Non-negotiable, in order, per `AGENTS.md`:

```bash
# 1. verify live truth first (never trust a cached board)
git ls-remote origin refs/heads/main
python scripts/agent_board.py show

# 2. claim (task must exist with acceptance_criteria + evidence_required)
python scripts/agent_board.py claim <task-id> --branch arena/f6713d82-nexus-ai-agent
git add .agents/board.json && git commit -m "board: claim <task-id>" && git push origin arena/f6713d82-nexus-ai-agent
#    an unpushed claim does not exist for other sandboxes

# 3. non-overlap check BEFORE pushing work
python scripts/agent_board.py check --files <changed,files> --branch arena/f6713d82-nexus-ai-agent \
       --repo bot523h/nexus-ai-agent          # --repo is REQUIRED: without it the open-PR
                                              # branch source is unverifiable (census G-2b)
#    exit 0 = pass · exit 1 = proven overlap, DO NOT PUSH · exit 2 = source unreadable, NOT a pass
#
#    MEASURED 2026-10-22: this check currently returns exit 2 for everyone, because the referee
#    requires exactly one active gates_owner and the board has zero (census G-1/G-2b). Until the
#    owner rules on PR #211 or appoints a gates owner, record the exit-2 output verbatim in the
#    PR body as evidence of the blocker — never paraphrase it as "no overlap found".

# 4. local diagnostics (permitted for everyone; these are NOT "the gates")
ruff check src tests && ruff format --check src tests
mypy src
pytest -q -m "not slow" <targeted paths>
pytest -q tests/unit/test_docs_integrity.py
pytest -q tests/architecture/

# 5. PR body must state: task id, zone, acceptance criteria met,
#    exact commands run, observed results, and "deferred to gates owner"
#    unless this branch holds gates_owner (census G-1: it does not).
```

**Also expect a write from a read command:** `agent_board.py show` runs lease GC and rewrites
`.agents/board.json` (census G-2c). Decide deliberately whether that delta belongs in your commit;
if you hold no claim on the `coordination` zone, revert it.

**Documentation contract (per `AGENTS.md` §7), in the same PR as the code:**
- new doc indexed in `docs/README.md`; every relative link resolves; every Mermaid fence balanced
  (`tests/unit/test_docs_integrity.py`);
- every new boundary law named with its enforcing test in `docs/architecture/MODULE_MAP.md §3`;
- behaviour-changing decisions recorded in `docs/DECISION_LOG.md`; doc-layer decisions in
  `docs/architecture/adr/`;
- `docs/architecture/CREATIVE_STUDIO.md` §5 coverage table and `docs/NAGAR_70_OPERATIONS_TDD.md`
  ledger updated when an operation ships.

---

## 7. Rejected approaches, with reasons (so they are not re-proposed)

| Rejected | Reason |
|---|---|
| Adding `scenedetect`/`cv2` imports inside `creative/packs/**` | R7 allowlist is 14 modules; `cv2` is banned. Instant CI failure. |
| A second subprocess site for external tools without an ADR | R9 pins the render lane to one site; an undocumented second site is architectural drift, not a feature. |
| Making `opentimelineio` a **runtime** dependency | The exporter already works without it; a runtime dep buys nothing and costs footprint. Dev-extra only. |
| WhisperX for alignment/diarization | 14 core deps incl. `torch~=2.8.0`, `triton`, `pyannote-audio`; incompatible with local/offline-first and already banned from the caption substrate. Native `energy_anchors` exists. |
| librosa for tempo analysis | `requires_python >=3.12` vs repo floor `>=3.10` — measured, hard conflict. Already rejected in `DECISION_LOG.md:182`. |
| MoviePy | Already rejected (`DECISION_LOG.md:202`); duplicates the FFmpeg lane. |
| Editly | Second language runtime (Node), upstream stale since 2025-05-12. |
| Vendoring OpenMontage skills | AGPL-3.0 (census F-1). Mission §3 also forbids removing upstream notices or presenting third-party assets as Nagar work. |
| A new `extension_hub/` package with its own manifest schema | Prior audit: duplicates `creative/packs/`, violates R20 unless fully wired, and creates a second trust authority. |
| Shipping a 25-skill catalogue up front | Most would reference capabilities that do not exist — unbacked skills that fail the mission's own §6 rule ("every skill must map to real capabilities"). |
| Claiming "10× better" anywhere | Mission §8 forbids it; repo D-0012 requires measurement. Baseline harness first (§3). |

---

## 8. Recommended immediate next step

**The single smallest, non-overlapping, verifiable step is Wave B1, and within B1 the smallest
unit is one test file:**

> Create `tests/unit/test_otio_interop.py` that round-trips the *existing* `_export_otio()`
> handler output through the real `opentimelineio` library (dev extra), plus a mutation probe
> that proves the test can fail.

It requires **no production code change**, adds **zero runtime dependencies**, closes a
**measured defect** (census F-2), creates a file the board already expects to exist, and
produces a hard fact the owner needs before deciding Q5: *is the OTIO we emit actually valid?*

Everything else in this plan is sequenced behind either that answer or an owner ruling on
Q1–Q6.

---

## 9. Honest status of this document

- **VERIFIED** — census §1 live repo truth, §2 capability census, §3 findings F-1…F-4, §5
  candidate matrix: all fetched from git/`gh`/the board/GitHub API/PyPI on 2026-10-22,
  reproducible via census §9.
- **VERIFIED** — the governance blocker: `agent_board.py check` returns **exit 2** (not a pass)
  on this wave's own four paths, both with and without `--repo`; output recorded verbatim in
  census G-2b. `agent_board.py show` was observed to rewrite `.agents/board.json` via lease GC
  (census G-2c); that delta was **reverted**, and `.agents/board.json` is byte-identical to
  `origin/main`.
- **VERIFIED** — the docs-integrity invariants this wave's own files must satisfy were checked
  locally and pass: zero unindexed documents under `docs/`, zero broken relative links in the
  three new documents plus `docs/README.md`, all code fences balanced, no `TODO:`/`FIXME:`/
  `TBD:`-form placeholder markers. This repairs a real defect: the previously committed
  `EXTENSION_HUB_PROMPT_AUDIT_2026-10-22.md` was **unindexed**, which
  `tests/unit/test_docs_integrity.py::test_every_document_is_indexed_in_docs_readme` fails on.
- **NOT DONE** — Waves B, C, D, E, F. Zero lines of production code, zero tests, zero manifest
  changes, zero board edits. This is a plan.
- **VERIFIED (executed, not inferred)** — local diagnostic test runs on this head:
  `tests/architecture/` **249 passed**, `tests/unit/test_docs_integrity.py` **77 passed**,
  `test_repo_truth_consistency.py` **8 passed**, `test_truth_doctor.py` **29 passed**, six
  pack-relevant gates **41 passed**. Environment and the exact dependency set are recorded in
  census §1.3. These are **diagnostics** under `AGENTS.md` §rule 4, not "the full gates", and
  they are not a claim of merge-readiness.
- **REPAIRED** — this branch had introduced a real docs-integrity defect (an unindexed audit
  document); it is fixed and the gate is green (census §1.3).
- **NOT DONE** — Waves B, C, D, E, F. Zero lines of production code, zero tests added, zero
  manifest changes, zero board edits. This is a plan.
- **NOT RUN** — the full `tests/unit/` and `tests/integration/` suites, and `ruff`/`mypy`. They
  require the complete dependency set (`chromadb`, `sentence-transformers`, `llama-cpp-python`,
  `litellm`, `boto3`, …) and services this sandbox does not provide. **No result is claimed for
  them either way.** The heavy install was deliberately avoided so that this wave stayed
  read-only with respect to the runtime environment.
- **BLOCKED pending owner** — Q1 (gates ownership / PR #211), Q2 (board task registration),
  Q3 (OpenMontage AGPL), Q4 (`[scene]` extra), Q5 (`include_markers`), Q6 (skill schema shape).
  Plus the measured referee blocker: `agent_board.py check` returns **exit 2** for everyone
  until Q1 is resolved (census G-2b), so a clean overlap verdict is currently unobtainable.
