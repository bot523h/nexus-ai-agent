# Creative Runtime (session 2: execution truth)

Owner: Creative Systems Engineer · `task-176` · branch
`arena/01a0d2a2-nexus-ai-agent` · 2026-09-24.

Session 1 registered the vision packs (77 operations, 8 packs). This document
records what session 2 proved about **execution**: which operations yield real
bytes, how multi-segment tracks render through the one canonical lane, and
which gaps remain — with every number measured from the tree, not typed by
hand. Reproduce: `python -c "from nexus_ai_agent.creative.execution import
execution_summary; print(execution_summary())"`.

## 1. Runtime truth (measured 2026-09-24)

| Operation class    | Registered | State-only | Executable¹ | Artifact² |
|---|---|---|---|---|
| Wave 1 (media/timeline/system) | 5 | 5 | 0 | 0 |
| slideshow.*        | 6 | 5 | 0 | 0 |
| caption.*          | 10 | 7 | 0 | 2 |
| timeline.* (edit pack) | 9 | 8 | 0 | 0 |
| motion.*           | 10 | 0 | 0 | 0 |
| audio.*            | 10 | 2 | 0 | 0 |
| color.*            | 4 | 0 | 1 | 0 |
| delivery.*         | 3 | 0 | 0 | 1 |
| portrait.*         | 10 | 1 | 0 | 0 |
| scene.*            | 10 | 4 | 0 | 0 |
| **Total**          | **77** | **32** | **1** | **3** |

¹ *Executable* = a lane twin exists and the plan bridge maps it
(`color.adjust_exposure → ExposureOp`). ² *Artifact* = the output carries
the complete deliverable document (`caption.generate_srt`,
`caption.generate_ass_rtl`, `delivery.export_otio`).

The remaining 41 operations are **effect descriptions**: deterministic
`EffectLayerRef` / derived-`AssetRecord` / spec derivations whose pixels need
a lane primitive that does not exist yet. Each names its
`MISSING_RENDER_PRIMITIVE` in `creative/execution.py`
(`missing_primitives()`); four of them need plan-mapping work only because
their lane twin already exists (`TitleOp`, `XfadeOp`, `DuckOp`, `LoudnormOp`).

Currently executable headline: **4 of 77** (1 lane render + 3 document
emissions). The table is a test ratchet
(`tests/unit/test_execution_semantics.py`): a new operation without a
classification fails the suite, and `EXECUTABLE` is derived from
`EFFECT_TO_LANE_OP`, never hand-claimed.

Deliberate honesty calls:

* `delivery.render_master_4k` is an **effect description** — the handler is a
  pure spec derivation (the name says *render*, the behaviour says *spec*).
* `slideshow.render` / `slideshow.upscale` are **state-only attestations**:
  they take `output_sha256` as *input* and never encode.
* `deterministic` on each record is the spec's *declared* value; only the 20
  vision operations are `determinism_pinned` (task-152/153 tests). Ops with
  `uuid` default asset ids are deterministic only when the caller pins
  `output_asset_id`.

## 2. The execution bridge (what exists, what is missing)

```
Operation ──▶ Effect / Transformation ──▶ Render Plan ──▶ Lane IR ──▶ Executor ──▶ Artifact
   │                 │                        │              │            │            │
   │                 │                   plan.py ✓      ir.py ✓    executor.py ✓  LaneArtifact ✓
   │                 │                   (multi-seg ✓ session 2)
   │                 │
   │                 ╰── GAP E1: no registered op attaches pack effect layers
   │                     to clips (only slideshow.compose attaches its own
   │                     internal layers). The bridge consumes clip.effects;
   │                     ops must learn to emit them (needs delivery files —
   │                     sequenced after PR#33).
   ╰── 32 state-only ops are terminal here by design (transport, markers,
       windows, analysis, attestations).
```

The lane itself is unchanged: `compile_assembly` emits one filtergraph with
one `concat` stage and the **same** `encode_lane` runs **one** FFmpeg process
(no second renderer, executor, or subprocess site — the lane boundary test
still enforces exactly one).

## 3. Multi-segment render (closed this session)

Session 1 proved the limit (`K > 1 → concat_required=True`: one `LaneIR`
compiles exactly one main source; the IR has no concat op). Session 2 closes
it with the minimal extension:

* `LaneAssembly` = ordered `LaneIR` segments + `AssemblyGap` pieces
  (black video + silence keep editorial timeline positions);
* `assemble_execution_plan` derives gaps from segment timeline positions
  (adjacent segments → no gap; overlap → `PlanError`);
* `compile_assembly` emits per-piece chains (shared emitters with the
  single-lane path — the untouched golden suite proves byte-identity) plus
  one `concat=n:v:a` stage; fail-closed on adjacent gaps, mixed media kinds,
  profile drift, and `loudnorm` (no per-piece measure pass).

Acceptance (`Segment A, Gap, Segment B`), measured in this sandbox with the
imageio static FFmpeg 7.0.2: one artifact, duration exactly `5000000 µs`
for a 2 s + 1 s + 2 s plan, 1280×720, audio present, `lane_ir_hash` equal to
the compiled hash. Without a binary these tests skip as **UNVERIFIED**, never
PASS.

## 4. Color / delivery handoff (blocked, prepared)

PR#33 is OPEN and CONFLICTING and still holds
`packs/delivery/{models,operations}.py` → **BLOCKED_SHARED_CONTRACT**.
Session 2 touched neither file. Ready to apply after unblock:
`docs/ops/COLOR_DELIVERY_HANDOFF_2026-09-24.md` (exact patches for
`color.white_balance` / `color.hdr_tonemap` / `color.deband_denoise` per TDD
§1.8, the `delivery.export_otio → creative/otio` delegation, manifest + test
deltas, integration checklist). Registered total becomes 80 on landing.

## 5. OTIO (two implementations; canonical round-trip verified)

* **Single canonical implementation? Not yet.** `delivery.export_otio` is the
  old asset-derived implementation (ignores `include_markers` and
  `timeline_id`); `creative/otio` is the canonical timeline bridge. The
  one-call-site delegation is prepared (§4) but blocked on PR#33.
* **Real-world round-trip? Verified where the library exists.** With
  `opentimelineio 0.18.1` installed, all 11
  `tests/unit/test_otio_roundtrip.py` tests pass, including Nagar → OTIO →
  official parser and official output → parse → Nagar. CI installs `.[dev]`
  without the library, so the 2 cross-checks skip there (UNVERIFIED).
* **ADR recommendation (no dependency added — STOP-4):** add an optional
  `otio` extra (`opentimelineio>=0.18,<0.19`) plus a non-blocking CI matrix
  leg that installs it and runs the round-trip file; flip to blocking once
  green twice. Rationale: the interchange is pure JSON without the library,
  so `pyproject` stays lean while CI proves real-parser compatibility.
  Owner: gates owner / the agent that owns `pyproject.toml` + workflows.

## 6. Capability availability (registered ≠ runnable)

`PackRuntime.availability()` reports per pack: `REGISTERED / AVAILABLE /
MISSING_DEPENDENCY / MISSING_BINARY / DISABLED / FAILED`
(`creative/packs/availability.py`; `status()` keeps answering the composition
question untouched). Binary resolution is injected — wire the one canonical
resolver, never a second `PATH` search:

```python
from nexus_ai_agent.creative.slideshow.ffmpeg import (
    FfmpegUnavailableError,
    resolve_ffmpeg_bin,
)

def _resolve_binary(name: str) -> str | None:
    if name == "ffmpeg":
        try:
            return resolve_ffmpeg_bin()
        except FfmpegUnavailableError:
            return None
    import shutil

    return shutil.which(name)

rows = build_pack_runtime(activate=True).availability(resolve_binary=_resolve_binary)
```

Measured today: the only declared binary repo-wide is `ffmpeg`
(`nexus.slideshow.compose`); no pack declares Python dependencies (pure ops
need none). All six states are pinned with fake resolvers
(`tests/unit/test_pack_status_availability.py`).

## 7. Test pyramid (where each level lives)

* **L1 pure operation logic** — `test_{portrait,scene,edit,motion,audio,
  caption,delivery,slideshow}_pack.py`, `test_opgap_audio_motion.py`.
* **L2 operation → plan** — `test_render_plan.py` (trim/effect mapping,
  unmapped reporting), `test_execution_semantics.py` (class truth table).
* **L3 plan → executor input** — `test_render_plan_assembly.py` (golden
  assembly argv, duration algebra, fail-closed matrix),
  `test_rendering_lane*.py` (single-lane goldens).
* **L4 real media artifact** — the `lavfi` legs in
  `test_render_plan_assembly.py` + `test_creative_runtime_e2e.py`
  (UNVERIFIED-skipped without FFmpeg).
* **L5 full creative integration** — `test_creative_runtime_e2e.py`
  (bus → plan → assembly → artifact), `test_pack_runtime_composition.py`,
  `test_pack_activation_completeness.py`.

## 8. Performance baseline (BASELINE ONLY — no optimisation claimed)

Measured 2026-09-24 in this sandbox (4-clip graded track for compiles;
2×4 s lavfi fixtures + 5 s 720p assembly for encode):

| Step | Mean | Method |
|---|---|---|
| `compile_execution_plan` | 0.054 ms/iter (N=200) | `perf_counter` |
| `assemble_execution_plan` | 0.024 ms/iter (N=200) | `perf_counter` |
| `compile_assembly` | 0.095 ms/iter (N=200) | `perf_counter` |
| plan+assembly+compile peak | 22.6 KiB | `tracemalloc` |
| fixture generation (2×4 s) | 0.25 s | wall clock |
| 5 s 720p assembly encode | 0.97 s → 219,037 bytes | wall clock |

No `before` exists (new code paths) — these numbers are the baseline future
work compares against.

## 9. Remaining creative debt (real items only)

1. **E1 — op→effect link.** No registered op attaches pack effect layers to
   clips. Minimal bridge: ops emit `EffectLayerRef` into clip/track effects
   (touches every pack incl. locked delivery — sequence after PR#33).
2. **Task-168 twins.** Four plan mappings need no new `LaneOp`
   (transition→`XfadeOp`, title→`TitleOp` + fontfile threading,
   duck→`DuckOp`, loudness→`LoudnormOp` + measure wiring).
3. **Lane primitives.** `lut3d`, tonemap, hqdn3d/gradfun, overlay, subtitles
   burn-in, mask-raster stages, stabilizer (`missing_primitives()` lists all
   41 with specs).
4. **OTIO delegation + markers** (§4 + task-121 residue).
5. **color.3 landing** (§4; 80 ops on landing).
6. **Determinism pins** beyond the 20 vision ops (uuid-default ids need
   caller-pinned `output_asset_id` for repeatability).
7. **Single-lane audio assumption.** Every lane input must carry an audio
   stream (`[0:a]` is unconditional) — pre-existing, unchanged this session.
