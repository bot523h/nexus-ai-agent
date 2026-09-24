"""Creative execution semantics: what each registered operation can really do.

Session 2 (task-176) answers the mission's P0 question — *state-only* versus
*real execution* — with a measured table instead of prose.  For every operation
in the runtime registry this module reports:

* :class:`ExecutionClass` — exactly one of ``STATE_ONLY``,
  ``EFFECT_DESCRIPTION``, ``EXECUTABLE``, ``RENDERED_ARTIFACT``;
* ``artifact_producing`` — dispatch (+ its documented follow-on path, if any)
  yields real media or document bytes, never just records;
* ``requires_media_engine`` — realisation needs FFmpeg or a media model;
* ``requires_pack`` — the pack ids the spec declares (read off the spec);
* ``deterministic`` — as *declared* on the spec, plus ``determinism_pinned``
  when a test actually pins same-input → same-output;
* ``currently_executable`` — dispatch through the documented path yields real
  bytes **today** (a lane twin or direct document emission).

The four classes are deliberately narrow:

* ``STATE_ONLY`` mutates project state (timeline windows, markers, playhead),
  returns analysis evidence, or records an *attestation* of work done elsewhere
  (``slideshow.render`` takes ``output_sha256`` as *input* — it never encodes).
* ``EFFECT_DESCRIPTION`` derives a media derivation (an :class:`EffectLayerRef`,
  a graded/composited/DSP ``AssetRecord``, a render spec) whose pixels still
  need a lane twin.  Each entry names its ``MISSING_RENDER_PRIMITIVE``.
* ``EXECUTABLE`` has a lane twin in
  :data:`~nexus_ai_agent.creative.rendering.plan.EFFECT_TO_LANE_OP` and renders
  through the one canonical lane (``plan → LaneIR → filtergraph → FFmpeg``).
* ``RENDERED_ARTIFACT`` carries the complete deliverable content in its output
  (SRT/ASS/OTIO text) — no further render step exists.  Persistence is the
  caller's job; the bytes are real either way.

Honesty rules enforced by ``tests/unit/test_execution_semantics.py``:

* the table covers *exactly* the runtime registry — a new operation without a
  classification fails the suite (no silent drift);
* ``EXECUTABLE`` is *derived* from ``EFFECT_TO_LANE_OP``, never hand-claimed;
* every ``RENDERED_ARTIFACT`` is dispatched in the test and its document text
  asserted — a placeholder would fail;
* ``delivery.render_master_4k`` stays ``EFFECT_DESCRIPTION``: the handler is a
  pure spec derivation (the name says *render*, the behaviour says *spec*).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ExecutionClass(str, Enum):
    """The four execution semantics.  See the module docstring."""

    STATE_ONLY = "state_only"
    EFFECT_DESCRIPTION = "effect_description"
    EXECUTABLE = "executable"
    RENDERED_ARTIFACT = "rendered_artifact"


@dataclass(frozen=True)
class ExecutionRecord:
    """The measured execution truth for one registered operation."""

    operation_id: str
    execution_class: ExecutionClass
    artifact_producing: bool
    requires_media_engine: bool
    requires_pack: tuple[str, ...]
    deterministic: bool
    determinism_pinned: bool
    currently_executable: bool
    lane_twin: str | None
    missing_primitive: str | None
    notes: str


#: Operations whose output carries the complete deliverable document text.
#: Proven by dispatch in tests/unit/test_execution_semantics.py.
RENDERED_ARTIFACT_OPS: tuple[str, ...] = (
    "caption.generate_srt",
    "caption.generate_ass_rtl",
    "delivery.export_otio",
)

#: Operations with a same-input → same-output test pinning their determinism.
#: Every other operation reports the spec's *declared* determinism only.
DETERMINISM_PINNED_OPS: tuple[str, ...] = (
    # task-152/153: "deterministic derived assets/evidence (same input → same hash)"
    "portrait.background_blur",
    "portrait.correct_gaze",
    "portrait.detect_landmarks",
    "portrait.enhance_eyes",
    "portrait.mask_hair",
    "portrait.relight_face",
    "portrait.retouch_blemish",
    "portrait.smooth_skin",
    "portrait.stabilize_face",
    "portrait.whiten_teeth",
    "scene.auto_reframe_subject",
    "scene.detect_shot_boundaries",
    "scene.find_subject_moment",
    "scene.remove_background",
    "scene.remove_logo",
    "scene.remove_object",
    "scene.replace_sky",
    "scene.segment_subject",
    "scene.track_face",
    "scene.track_object",
)

#: ``operation_id → (MISSING_RENDER_PRIMITIVE, notes)`` for every effect
#: description.  Twins marked "lane twin exists" need plan-mapping work only
#: (no new LaneOp); everything else needs a new lane primitive or an engine
#: the lane does not have.
MISSING_RENDER_PRIMITIVES: dict[str, tuple[str, str]] = {
    # -- portrait (task-152): masks are MaskRef hashes, never rasters ---------
    "portrait.background_blur": (
        "mask-aware blur LaneOp",
        "needs the SubjectMask as a raster plus a gblur/boxblur stage; plan reports unmapped today",
    ),
    "portrait.correct_gaze": (
        "identity-sensitive warp LaneOp",
        "no ffmpeg primitive; confirmation-gated by design (Level C)",
    ),
    "portrait.enhance_eyes": (
        "eye-region clarity LaneOp",
        "needs eye landmarks as a raster mask plus an eq/curves stage",
    ),
    "portrait.mask_hair": (
        "hair-mask raster LaneOp",
        "MaskRef asset is a hash; no segmentation raster path exists",
    ),
    "portrait.relight_face": (
        "LightModel relight LaneOp",
        "typed relight layer; no lane stage consumes LightModel parameters",
    ),
    "portrait.retouch_blemish": (
        "local inpaint LaneOp",
        "inpaint plan over a FaceMask; ffmpeg delogo/inpaint is region-only, not mask-driven",
    ),
    "portrait.smooth_skin": (
        "skin-tone smoothing LaneOp",
        "needs a FaceMask raster plus a smoothing stage (e.g. hqdn3d masked)",
    ),
    "portrait.stabilize_face": (
        "face-track stabilizer LaneOp",
        "TransformCurve plan exists; lane has no deshake/vidstab stage wired to a curve",
    ),
    "portrait.whiten_teeth": (
        "teeth-region lift LaneOp",
        "needs a TeethMask raster plus a masked eq stage",
    ),
    # -- scene (task-153) ------------------------------------------------------
    "scene.auto_reframe_subject": (
        "reframe LaneOp",
        "TransformCurve plan exists; lane has no crop/scale stage wired to a curve",
    ),
    "scene.remove_background": (
        "alpha-matte composite LaneOp",
        "SubjectMask alpha layer is a description; needs a segmentation raster + overlay stage",
    ),
    "scene.remove_logo": (
        "logo-region inpaint LaneOp",
        "legal-policy op (Level C); needs region inpaint, no lane stage",
    ),
    "scene.remove_object": (
        "temporal inpaint engine",
        "no ffmpeg primitive; requires an external inpainting model",
    ),
    "scene.replace_sky": (
        "sky-matte composite LaneOp",
        "segmentation + composite; needs a sky-mask raster stage",
    ),
    "scene.segment_subject": (
        "subject-mask raster LaneOp",
        "semantic MaskRef is a hash; no segmentation raster path exists",
    ),
    # -- motion ----------------------------------------------------------------
    "motion.add_glow": (
        "glow filter LaneOp",
        "needs a gblur/additive composite stage",
    ),
    "motion.add_motion_blur": (
        "motion-blur filter LaneOp",
        "needs a minterpolate/tblend stage",
    ),
    "motion.add_parallax": (
        "parallax composite LaneOp",
        "needs layered depth composite; no lane stage",
    ),
    "motion.add_particles": (
        "particle overlay LaneOp",
        "needs a procedural overlay stage",
    ),
    "motion.add_title": (
        "plan mapping to TitleOp (lane twin exists)",
        "TitleOp exists in the lane; the plan must thread text/style + an explicit fontfile",
    ),
    "motion.add_transition": (
        "plan mapping to XfadeOp (lane twin exists)",
        "XfadeOp exists; the plan needs transition kind/offset/duration derivation from clip adjacency",
    ),
    "motion.apply_mask": (
        "mask composite LaneOp",
        "mask asset is a hash; needs a raster mask + overlay stage",
    ),
    "motion.keyframe_transform": (
        "keyframed transform LaneOp",
        "animation record exists; lane has no scale/rotate/crop tween stage",
    ),
    "motion.stabilize": (
        "stabilizer LaneOp",
        "no deshake/vidstab stage in the lane",
    ),
    "motion.warp": (
        "mesh-warp filter LaneOp",
        "mesh plan exists; lane has no perspective/remap stage wired to a mesh",
    ),
    # -- audio -----------------------------------------------------------------
    "audio.align_music": (
        "music-alignment LaneOp",
        "alignment derivation; needs a time-shift/trim stage wired to the alignment",
    ),
    "audio.deess": (
        "de-esser filter LaneOp",
        "needs an afftdn/adeesser-class stage",
    ),
    "audio.duck_music": (
        "plan mapping to DuckOp (lane twin exists)",
        "DuckOp exists; the plan must thread the voice asset id + duck parameters",
    ),
    "audio.eq_voice": (
        "voice-EQ filter LaneOp",
        "preset table exists; lane has no equalizer stage",
    ),
    "audio.normalize_loudness": (
        "plan mapping to LoudnormOp (lane twin exists)",
        "LoudnormOp exists; the plan must wire the two-pass measure → apply flow",
    ),
    "audio.remove_noise": (
        "denoise filter LaneOp",
        "needs an afftdn-class stage",
    ),
    "audio.remove_vocal": (
        "vocal-removal engine",
        "stem table is a derivation; needs a source-separation model",
    ),
    "audio.time_stretch": (
        "time-stretch LaneOp",
        "needs an atempo/rubberband stage with pitch policy",
    ),
    # -- color (delivery pack) -------------------------------------------------
    "color.apply_lut": (
        "lut3d LaneOp",
        "needs a LUT file stage (lut3d) plus file staging",
    ),
    "color.auto_balance": (
        "auto-balance LaneOp",
        "needs analysis + colorbalance derivation; no lane stage",
    ),
    "color.match_shot": (
        "shot-match LaneOp",
        "needs cross-clip analysis + derived grade; no lane stage",
    ),
    # -- delivery --------------------------------------------------------------
    "delivery.make_proxy_480p": (
        "proxy encode path",
        "proxy record only; no plan → scale+encode mapping exists",
    ),
    "delivery.render_master_4k": (
        "master encode orchestration",
        "Level C spec derivation only — the handler never encodes (see module docstring)",
    ),
    # -- timeline --------------------------------------------------------------
    "timeline.attach_b_roll": (
        "overlay LaneOp",
        "b-roll overlay record; lane has no overlay stage",
    ),
    # -- caption ---------------------------------------------------------------
    "caption.burn_in": (
        "subtitle burn-in LaneOp",
        "needs a libass/subtitles stage plus staged caption media",
    ),
    # -- slideshow -------------------------------------------------------------
    "slideshow.compose": (
        "slideshow service path (not op-dispatched)",
        "compose builds timeline + layers; pixels come from creative/slideshow/service.py via bot/CLI, not via this op",
    ),
}


def _lane_twins() -> dict[str, str]:
    from nexus_ai_agent.creative.rendering.plan import EFFECT_TO_LANE_OP

    return dict(EFFECT_TO_LANE_OP)


def classify_operation(operation_id: str, *, registry: Any = None) -> ExecutionRecord:
    """Classify one registered operation.  Raises for unknown operations."""
    if registry is None:
        from nexus_ai_agent.creative.packs.runtime import build_runtime_registry

        registry = build_runtime_registry()
    spec = registry.get_spec(operation_id)  # UnknownOperationError when unregistered
    twins = _lane_twins()
    twin = twins.get(operation_id)
    if twin is not None:
        return ExecutionRecord(
            operation_id=operation_id,
            execution_class=ExecutionClass.EXECUTABLE,
            artifact_producing=True,
            requires_media_engine=True,
            requires_pack=tuple(spec.required_packs),
            deterministic=bool(spec.deterministic),
            determinism_pinned=operation_id in DETERMINISM_PINNED_OPS,
            currently_executable=True,
            lane_twin=twin,
            missing_primitive=None,
            notes=f"lane twin {twin!r}: plan → LaneIR → FFmpeg renders real pixels",
        )
    if operation_id in RENDERED_ARTIFACT_OPS:
        return ExecutionRecord(
            operation_id=operation_id,
            execution_class=ExecutionClass.RENDERED_ARTIFACT,
            artifact_producing=True,
            requires_media_engine=False,
            requires_pack=tuple(spec.required_packs),
            deterministic=bool(spec.deterministic),
            determinism_pinned=operation_id in DETERMINISM_PINNED_OPS,
            currently_executable=True,
            lane_twin=None,
            missing_primitive=None,
            notes="output carries the complete deliverable document text; no render step remains",
        )
    missing = MISSING_RENDER_PRIMITIVES.get(operation_id)
    if missing is not None:
        primitive, detail = missing
        return ExecutionRecord(
            operation_id=operation_id,
            execution_class=ExecutionClass.EFFECT_DESCRIPTION,
            artifact_producing=False,
            requires_media_engine=True,
            requires_pack=tuple(spec.required_packs),
            deterministic=bool(spec.deterministic),
            determinism_pinned=operation_id in DETERMINISM_PINNED_OPS,
            currently_executable=False,
            lane_twin=None,
            missing_primitive=primitive,
            notes=detail,
        )
    return ExecutionRecord(
        operation_id=operation_id,
        execution_class=ExecutionClass.STATE_ONLY,
        artifact_producing=False,
        requires_media_engine=False,
        requires_pack=tuple(spec.required_packs),
        deterministic=bool(spec.deterministic),
        determinism_pinned=operation_id in DETERMINISM_PINNED_OPS,
        currently_executable=False,
        lane_twin=None,
        missing_primitive=None,
        notes="state mutation, analysis evidence, or attestation of external work — no media path",
    )


def execution_table(*, registry: Any = None) -> tuple[ExecutionRecord, ...]:
    """Classify every registered operation, sorted by operation id."""
    if registry is None:
        from nexus_ai_agent.creative.packs.runtime import build_runtime_registry

        registry = build_runtime_registry()
    return tuple(
        classify_operation(operation_id, registry=registry)
        for operation_id in registry.list_operations()
    )


def execution_summary(*, registry: Any = None) -> dict[str, int]:
    """Counts per class plus the headline currently-executable total."""
    table = execution_table(registry=registry)
    summary = {member.value: 0 for member in ExecutionClass}
    for record in table:
        summary[record.execution_class.value] += 1
    summary["currently_executable"] = sum(1 for r in table if r.currently_executable)
    summary["registered"] = len(table)
    return summary


def missing_primitives() -> tuple[tuple[str, str], ...]:
    """``(operation_id, MISSING_RENDER_PRIMITIVE)`` for every effect description."""
    return tuple(
        sorted(
            (operation_id, primitive)
            for operation_id, (primitive, _) in MISSING_RENDER_PRIMITIVES.items()
        )
    )


__all__ = [
    "DETERMINISM_PINNED_OPS",
    "MISSING_RENDER_PRIMITIVES",
    "RENDERED_ARTIFACT_OPS",
    "ExecutionClass",
    "ExecutionRecord",
    "classify_operation",
    "execution_summary",
    "execution_table",
    "missing_primitives",
]
