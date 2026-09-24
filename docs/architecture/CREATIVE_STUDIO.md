# Nagar Creative Studio

**Status:** Living document (wave state + coverage matrix regenerated each release)
**Scope:** the capability model, the pack substrate, the command bus, the render lane, and the honest coverage ledger
**Verified against:** `main` @ `9ec312c` (re-measured 2026-09-24 during task-166) — numbers below were produced by executing the builders and the CLI, not by reading names

Nagar is the studio inside NEXUS: a typed, permissioned, *pure-by-default* media pipeline whose only impure step is a single FFmpeg process at the very end.

---

## 1. The shape in one picture

```mermaid
flowchart TB
    subgraph pure["Pure (stdlib + pydantic + studio) — deterministic, fast tests"]
        cmd["TypedCommand envelope<br/>protocol v1 + schema 2 + actor/project + idempotency"]
        bus["CommandBus<br/>schema → auth → capability → policy → refs → reserve → apply"]
        reg["CapabilityRegistry<br/>Domain > Capability > OperationSpec"]
        packs["7 packs: edit · motion · audio · caption · color/delivery · slideshow"]
        proj["Project state<br/>Timeline/Track/Clip/AssetRecord/EffectLayerRef<br/>derived state_hash"]
    end
    subgraph impure["Impure — one process, one file, one measurement"]
        lane["Render lane<br/>LaneIR → filtergraph → argv → FFmpeg"]
        probe["probe_video + sha256 evidence"]
    end
    cmd --> bus --> reg --> packs --> proj
    proj -->|lane_ir_from_project| lane --> probe
```

The split is the load-bearing decision: **evidence is gathered above the bus** (probing, analysis, image generation), **handlers inside the bus stay pure**, and **rendering happens in the lane**. In-memory bus idempotency applies only to its handler, not to FFmpeg: a rendered artifact can precede the recording command. See [`../DECISION_LOG.md`](../DECISION_LOG.md) Wave 2/2c entries and the [Gate 2 security contract](COMMAND_CAPABILITY_CONTRACT.md).

## 2. Capability model

| Concept | Type | Meaning |
|---|---|---|
| Domain | `Domain` | first id segment: `media`, `timeline`, `motion`, `audio`, `caption`, `color`, `delivery`, `slideshow`, `system` |
| Capability | `Capability` | a named grouping inside a domain (for example `timeline.edit`) |
| Operation | `OperationSpec` | `operation_id`, typed `input_model` (pydantic, `extra="forbid"`), handler, `permission_level`, `undoable` |
| Envelope | `TypedCommand` | Protocol `nagar.command.v1`, schema version 2; required `actor`, `target.project_id`, `provenance`; typed `input_refs`, policy, context, optional registry snapshot, idempotency key and preconditions. See [Gate 2](COMMAND_CAPABILITY_CONTRACT.md). |

**Permission ladder** (`PermissionLevel`):

| Level | Name | Behaviour | Example |
|---|---|---|---|
| A | Immediate | non-destructive, always allowed | `media.play`, `timeline.mark` |
| B | Reversible | applied as an atomic `EditTransaction`, undoable | `timeline.trim`, `motion.add_transition` |
| C | Confirmation | requires `confirmed: true` in the envelope | heavyweight/identity-changing ops (none registered yet) |
| D | Denied | refused by policy: shell, raw upload, code execution, anything unregistered | — |

**Dispatch pipeline** (`creative/studio/bus.py::_dispatch_locked`) — strict order, each step failing closed:
1. parse JSON and versioned envelope → 2. registered operation input schema → 3. independently authorized actor/project grant → 4. authoritative registry capability/version and required permissions → 5. execution mode + A/B/C/D policy → 6. project-scoped input references and pinned time references → 7. in-memory reservation/replay-or-conflict (key scoped by project and operation) → 8. revision/hash preconditions for new work → 9. pure handler + atomic state/result commit. The previous early replay path is retired; see [Gate 2](COMMAND_CAPABILITY_CONTRACT.md) for limits and negative tests.

`Project.state_hash` is **derived** from a canonical JSON serialization on every construction (revision excluded, so revision+hash preconditions survive undo cycles). A stored hash therefore cannot drift from the state it describes.

## 3. The pack substrate (data, never code-on-arrival)

A pack is a directory with a `pack.manifest.json` validated against `nexus.capability-pack.v1` plus pure handler modules:

| Field group | Examples |
|---|---|
| identity | `manifest_schema`, `package_id`, `version`, `display_name` |
| capability surface | `capabilities: ["slideshow.compose", …]` |
| execution | `runtime.native` (`adapter_id`, `sandbox: in_process`, `network_required`), `runtime.browser` (`none`) |
| power | `permissions` (allow-list strings only), `network_policy`, `hardware_requirements`, `resource_budget` |
| supply chain | `artifacts` (with hashes), `security` (signature state), `external_binaries` |

**Rules that are enforced, not requested**
- No executable key at any depth of any manifest (`test_pack_manifest_is_data_only.py`).
- A pack's declared capabilities must equal the operations its registration function adds, and it must activate against its own manifest (`test_slideshow_adapter_boundary.py`).
- An **external** pack that declares an operation the runtime does not know is rejected at registration; a **builtin** pack may register with *pending* capabilities but cannot be activated until they resolve (`creative/packs/registry.py`).
- Verification reports every finding; signature state is reported honestly (`placeholder`, `format_only_unverified`) — no pack claims a verified signature today.

## 4. Pack inventory and the activation gap (re-measured session 3, 2026-09-24)

`nexus packs list` output on this branch (executed, not paraphrased):

| Pack | Version | Capabilities | Pending | Signature | Binaries |
|---|---|---:|---:|---|---|
| `nexus.slideshow.compose` | 0.2.0 | 6 | 0 | placeholder | ffmpeg |
| `nexus.language.caption` | 1.0.0 | 10 | 0 | placeholder | — |
| `nexus.edit.timeline` | 1.0.0 | 9 | 0 | format_only_unverified | — |
| `nexus.motion.graphics` | 1.0.0 | 10 | 0 | format_only_unverified | — |
| `nexus.audio.studio` | 1.0.0 | 10 | 0 | format_only_unverified | — |
| `nexus.color.delivery` | 1.0.0 | 7 | 0 | format_only_unverified | — |
| `nexus.vision.portrait` | 1.0.0 | 10 | 0 | format_only_unverified | — |
| `nexus.vision.scene` | 1.0.0 | 10 | 0 | format_only_unverified | — |

The activation gap is **closed** (board task-126, landed): `cli.py::_packs_registry`
now composes `creative.packs.runtime.build_pack_registry()` — the runtime and the
CLI see the same **77** registered operation ids (72 pack ops + 5 coreless Wave 1
ops), and every builtin manifest verifies clean. Activation remains an explicit,
auditable step (`nexus packs activate`), which is why `packs list` still reports
each pack's `active` flag as false until an operator activates it.

## 5. Coverage ledger vs. the TDD catalogue

Re-measured session 3 (2026-09-24) by diffing the backtick-quoted dotted ids in
[`../NAGAR_70_OPERATIONS_TDD.md`](../NAGAR_70_OPERATIONS_TDD.md) against
`build_runtime_registry().list_operations()`:

| Metric | Value |
|---|---:|
| Operation ids catalogued by the TDD (backtick parse) | 72 |
| Registered at runtime (CLI == builders) | **77 unique** |
| TDD ids implemented | 69 |
| TDD ids remaining | **3** |
| Registered beyond the TDD catalogue (slideshow 6, `media.pause`, `timeline.mark`) | 8 |

Remaining, by family (only the `color.*` AI lane is left):

| Family | Remaining ids |
|---|---|
| `color.*` (3) | `white_balance`, `deband_denoise`, `hdr_tonemap` |

All `portrait.*`, `scene.*`, `motion.*`, `audio.*`, and `timeline.*` TDD ids are
now registered. (Method note: session 2 counted 69 catalogued ids with a
family filter; session 3 parses all backtick-quoted dotted ids — 72 — which
also classifies `system.undo`, `delivery.make_proxy_480p`, and
`delivery.render_master_4k` as implemented TDD ids rather than beyond-catalogue
additions. Either way the remaining set is the same three `color.*` ids.)

## 6. The render lane

| Property | Implementation |
|---|---|
| Typed ops | 11 (`TrimOp`, `SpeedOp`, `ReverseOp`, `FreezeOp`, `XfadeOp`, `TitleOp`, `LoudnormOp`, `DuckOp`, `ExposureOp`, `LutOp`, `SubtitleOp`) in `creative/rendering/ir.py` |
| Duration algebra | integer microseconds — trim/speed/reverse/freeze/xfade have explicit formulas; exposure/lut/subtitle/title are duration-neutral (pinned by `test_lane_duration_algebra.py` + `test_time_algebra.py`); no float drift |
| Purity | `compile_lane`, `compile_measure`, `compile_assembly` are pure and golden-tested; `lane_ir_from_project` bridges packs' `Project` IR to lane IR |
| Multi-segment | `LaneAssembly` (segments + editorial gaps) compiles to one filtergraph with one `concat` and executes through the same `encode_lane` — no second pipeline |
| Process policy | one process per call, no shell, one timeout, binary allow-list (`override → NEXUS_FFMPEG_BIN → PATH → imageio-ffmpeg`) |
| Publication | `.<name>.part.<ext>` → atomic rename; `overwrite=False` by default |
| Evidence | `probe_video` + `sha256_file` on the published artifact; a run that cannot probe fails with `LaneExecutionError`; `creative/artifacts.py` re-measures bytes independently and separates spec hash / file hash / content identity (NAG-003) |
| Gates | `test_rendering_lane_boundary.py`: no ML imports, import allow-list, no zone cross-over, **exactly one** `subprocess` site, never `shell=True` |
| Engine truth | each lane twin declares its FFmpeg filters (`execution.TWIN_ENGINE_FILTERS`): `lut3d`/`subtitles`/`eq` ship in the imageio wheel, but `drawtext` (titles) needs a libfreetype-enabled binary — the title proof skips loudly where it is absent |

The lane is deliberately the *only* place in the creative tree that spawns a process (`test_slideshow_adapter_boundary.py::test_only_the_render_lane_spawns_a_process`). Subtitle fonts resolve via `NEXUS_FONTS_DIR` (else libass system fonts); the repo ships Vazirmatn under `assets/fonts/` for Persian burn-in.

## 7. Speech, captions, and offline translation

- `CaptionEnginePort` is the seam; `creative/caption/unavailable_adapter.py` is the fail-closed default (typed `caption_profile_unavailable`, never silent degradation).
- `WhisperLocalCaptionEngine` (extra `[speech]`, CTranslate2 int8, no torch) implements `transcribe`, `align_words`, `diarize`, `translate_local`; offline translation is the `[translate]` extra (argos-translate).
- Pure formatters (`srt`, `vtt`, `ass`) live in the caption pack, including RTL/bidi handling with Vazirmatn styling for Persian — text layout is data, not a rendering process.

## 8. How to verify this document

```bash
# inventory (expect: 8 packs, 77 unique registered ops; CLI == builders)
python - <<'PY'
from nexus_ai_agent.cli import _packs_registry
print(len(_packs_registry().runtime_registry.list_operations()))
PY
from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
print(len(build_runtime_registry().list_operations()))   # also 77
nexus packs list          # pending counts per pack (§4 table — all 0)
nexus packs verify <id>   # every finding, not a summary
pytest -q tests/architecture tests/unit/test_caption_pack.py tests/unit/test_rendering_lane.py
pytest -q tests/unit/test_creative_render_jobs.py tests/unit/test_creative_surface.py
```

## 9. The one-shot Telegram surface (task-166, 2026-09-24)

`/edit`, `/caption`, `/grade` are live commands on the canonical chain:

`Telegram → creative_surface (mapper + staging) → JobQueuePort (idempotency
key = message identity) → worker ``creative_render`` → packs runtime registry
→ CommandBus (typed op, idempotency-keyed) → render lane (allow-listed FFmpeg)
→ measured artifact → translated completion notify.`

- Surface ops are exactly the honestly-executable matrix: `edit trim|speed|reverse`,
  `grade exposure|lut|proxy|otio`, `caption transcribe|burnin`. Session 3 opened
  `lut` (shipped `.cube` looks in `creative/luts/` + the `lut3d` lane instrument)
  and `burnin` (worker-staged SRT + the `subtitles` lane instrument) after the
  task-166 refusal; both were refused typed while no honest path existed, and
  both render real pixels now (see ADR 0006 for the same anti-silent-degradation
  rule as §7).
- The bus enforces the 4-state pack lifecycle at dispatch (step 3.5):
  `slideshow`/`caption`/`edit` are `AVAILABLE`, the other five packs are
  `EXPERIMENTAL` — so the surface opts `grade/*` jobs into experimental
  explicitly (`allow_experimental` on the queue payload), and unknown pack ids
  fail closed (`studio/lifecycle.py`, `test_capability_lifecycle.py`).
- Every user-visible string comes from the i18n catalog (`creative.*` keys in
  all 15 locales); a raw key never reaches Telegram.
- Verification: `tests/unit/test_creative_render_jobs.py` renders real
  two-second clips through the whole chain with the imageio-ffmpeg binary;
  `tests/architecture/test_creative_channels.py` ratchets the wiring.

## 10. Session 3: the artifact-producing runtime (task-177, 2026-09-24)

- **Canonical path, one only**: `Project → compile_execution_plan →
  assemble_execution_plan → compile_assembly → encode_lane →
  verify_lane_artifact`. Effect twins live in `rendering/plan.EFFECT_TO_LANE_OP`
  (`exposure`, `title`, `lut`, `subtitle`); `EXECUTABLE` in `execution.py` is
  *derived* from that map, never hand-claimed (7 currently-executable ops).
- **A→B→C proof**: `test_assembly_render_proof.py` renders three colored
  segments with the warm LUT + Persian burn-in through one assembly encode and
  proves order, grading, and glyph pixels from sampled frames.
- **Artifact truth** (`creative/artifacts.py`): `render_spec_hash` (recipe) ≠
  `physical_artifact_sha256` (bytes) ≠ `logical_content_identity`; `ffprobe`
  preferred with a named `ffmpeg-stderr` fallback; verification failures raise
  and can never report success.
- **OTIO interop** (`creative/interop/otio.py`, extra `[otio]`): the real
  OpenTimelineIO library parses/emits; transitions import as honestly-unmapped
  layers; markers and gaps survive; round-trip is exact at integer rates.
- **Anti-vacuity**: `test_antivacity_mutations.py` (M1–M7) breaks one thing per
  test and asserts the suite catches it; `test_time_algebra.py` pins the
  integer-microsecond clock end to end.

If a number here disagrees with the output, the document is wrong — fix it in the same PR that changed the code (rule in [`../README.md`](../README.md)).
