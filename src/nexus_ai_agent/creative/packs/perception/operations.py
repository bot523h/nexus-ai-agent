"""Pure operations for the ``nexus.analysis.perception`` pack.

* ``analysis.mark_silences``   (Level B, REVERSIBLE): one atomic transaction that
  adds timeline markers at the silence edges measured on a clip's source media.
* ``analysis.mark_scene_cuts`` (Level B, REVERSIBLE): the same for scene changes.
* ``analysis.assess_loudness`` (Level A, IMMEDIATE): compares a measured EBU R128
  summary with a target and returns the exact arguments for
  ``audio.normalize_loudness`` -- it never edits anything.

Handlers are pure ``(project, context) -> outcome`` functions: no I/O, no clock,
no randomness.  Marker ids are *content-derived*, so replaying the same evidence
is idempotent (the second run adds nothing) instead of duplicating markers.

Time mapping.  Evidence is in **asset time**; markers live on the **timeline**.
An instant ``t`` inside the clip's half-open source window maps linearly::

    timeline = timeline.start + floor((t - source.start) * timeline.len / source.len)

so a retimed clip is respected.  Instants outside the clip's window are *counted*
(``skipped_outside_clip``), never clamped onto the edge and never silently dropped.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from nexus_ai_agent.creative.packs.perception.models import (
    DOMAIN,
    MAX_MARKERS_PER_COMMAND,
    OPERATION_ASSESS_LOUDNESS,
    OPERATION_MARK_SCENE_CUTS,
    OPERATION_MARK_SILENCES,
    PERCEPTION_PACKAGE_ID,
    AssessLoudnessInput,
    MarkSceneCutsInput,
    MarkSilencesInput,
)
from nexus_ai_agent.creative.studio.capabilities import (
    CapabilityRegistry,
    OperationContext,
    OperationOutcome,
    OperationSpec,
)
from nexus_ai_agent.creative.studio.models import (
    Clip,
    CommandValidationError,
    Marker,
    PermissionLevel,
    Project,
)

#: Gain (dB) is reported with centi-dB resolution; more digits are false precision.
_GAIN_DIGITS = 2

#: Policy ceiling for a silence claim: a segment is silence only when its RMS is
#: below this level. It is deliberately a *policy* value, not derived from the
#: measurement, so the gate checks the claim instead of restating it.
_SILENCE_CEILING_DBFS = -30.0
_SILENCE_SAMPLE_RATE = 48_000
_SILENCE_BIT_DEPTH = 16
_SILENCE_COVERAGE = 0.95

#: Scene-cut gate defaults: the surrogate histogram sample count and the coverage
#: factor used to expand the divergence interval.
_SCENE_CUT_SAMPLES = 4096
_SCENE_CUT_COVERAGE = 2.0

#: The admission port. The pack may not import the verification lane, so the
#: composition root injects a *plain-data* callable: a gate context in, a gate
#: record out (see ``MODULE_MAP.md`` R31).
PerceptionGate = Callable[[Mapping[str, Any]], Mapping[str, Any]]

_GATE_NOT_CONFIGURED: dict[str, Any] = {
    "admitted": None,
    "outcome": "not_configured",
    "refusal": None,
    "recommended_gain_db": None,
}


def _gate_record(gate: PerceptionGate | None, context: Mapping[str, Any] | None) -> dict[str, Any]:
    """Run the admission gate, or record honestly that none is bound.

    A gate that answers with something other than a mapping is a wiring defect,
    so it fails closed (:class:`CommandValidationError`) instead of admitting.
    """
    if gate is None or context is None:
        return dict(_GATE_NOT_CONFIGURED)
    record = gate(context)
    if not isinstance(record, Mapping):
        raise CommandValidationError(
            f"the perception admission gate returned {type(record).__name__}, not a mapping"
        )
    if record.get("admitted") not in (True, False):
        raise CommandValidationError(
            "the perception admission gate must answer admitted=True/False, "
            f"got {record.get('admitted')!r}"
        )
    return dict(record)


def _find_clip(project: Project, track_id: str, clip_id: str) -> Clip:
    track = next((t for t in project.timeline.tracks if t.track_id == track_id), None)
    if track is None:
        raise CommandValidationError(f"track not found: {track_id!r}")
    clip = next((c for c in track.clips if c.clip_id == clip_id), None)
    if clip is None:
        raise CommandValidationError(f"clip not found: {clip_id!r} on track {track_id!r}")
    return clip


def _require_same_content(clip: Clip, evidence_sha256: str, operation: str) -> None:
    if clip.media_ref.content_sha256 != evidence_sha256:
        raise CommandValidationError(
            f"{operation}: evidence was measured on different content than clip "
            f"{clip.clip_id!r} (refusing stale or foreign evidence)"
        )


def _to_timeline_us(clip: Clip, asset_us: int) -> int | None:
    """Map an asset instant into the timeline, or ``None`` when outside the clip window."""
    source = clip.source_range
    placed = clip.timeline_range
    if not source.start_us <= asset_us < source.end_us:
        return None
    source_len = source.end_us - source.start_us
    placed_len = placed.end_us - placed.start_us
    return placed.start_us + ((asset_us - source.start_us) * placed_len) // source_len


def _marker_id(evidence_hash: str, clip_id: str, kind: str, asset_us: int) -> str:
    seed = f"{evidence_hash}|{clip_id}|{kind}|{asset_us}".encode()
    return "mk_" + hashlib.sha256(seed).hexdigest()[:12]


def _add_markers(
    project: Project,
    candidates: list[tuple[str, int, str]],
    clip: Clip,
    evidence_hash: str,
    kind: str,
) -> tuple[Project, dict[str, Any]]:
    """Turn ``(kind, asset_us, label)`` candidates into markers; idempotent and capped."""
    if len(candidates) > MAX_MARKERS_PER_COMMAND:
        raise CommandValidationError(
            f"evidence maps to {len(candidates)} markers, above the per-command "
            f"limit of {MAX_MARKERS_PER_COMMAND}; raise the threshold and re-measure"
        )
    existing_ids = {marker.marker_id for marker in project.timeline.markers}
    added: list[Marker] = []
    skipped_outside = 0
    already_present = 0
    for edge_kind, asset_us, label in candidates:
        timeline_us = _to_timeline_us(clip, asset_us)
        if timeline_us is None:
            skipped_outside += 1
            continue
        marker_id = _marker_id(evidence_hash, clip.clip_id, edge_kind, asset_us)
        if marker_id in existing_ids:
            already_present += 1
            continue
        existing_ids.add(marker_id)
        added.append(Marker(marker_id=marker_id, timecode_us=timeline_us, label=label))
    if added:
        timeline = project.timeline.model_copy(
            update={"markers": [*project.timeline.markers, *added]}
        )
        project = project.model_copy(update={"timeline": timeline})
    return project, {
        "evidence_hash": evidence_hash,
        "evidence_kind": kind,
        "clip_id": clip.clip_id,
        "markers_added": len(added),
        "marker_ids": [marker.marker_id for marker in added],
        "skipped_outside_clip": skipped_outside,
        "already_present": already_present,
    }


def _marker_admission(gate: PerceptionGate | None, payload: Any) -> dict[str, Any]:
    """License join for a marker command, or a no-claim record when none is declared."""
    if not payload.licenses:
        return {
            "admitted": None,
            "outcome": "no_license_declared",
            "refusal": None,
            "recommended_gain_db": None,
        }
    return _gate_record(
        gate,
        {
            "kind": "license",
            "licenses": list(payload.licenses),
            "commercial_intent": payload.commercial_intent,
        },
    )


def _silence_gate_context(evidence: Any) -> dict[str, Any]:
    """GUM request for the worst (shortest) silence span in the evidence."""
    durations = [(span.end_us - span.start_us) / 1_000_000 for span in evidence.spans]
    window = min(durations, default=0.0)
    minimum = min(evidence.min_silence_us / 1_000_000, window) if window > 0.0 else 0.0
    return {
        "kind": "silence",
        "rms_db": evidence.noise_floor_db,
        "threshold_db": _SILENCE_CEILING_DBFS,
        "sample_count": max(2, int(window * _SILENCE_SAMPLE_RATE)),
        "bit_depth": _SILENCE_BIT_DEPTH,
        "window_duration_s": window,
        "min_silence_duration_s": minimum,
        "coverage_probability": _SILENCE_COVERAGE,
    }


def _scene_gate_context(evidence: Any, cut_threshold_bits: float) -> dict[str, Any]:
    """GUM request for the detector strength reported with a scene-cut evidence."""
    return {
        "kind": "scene_cut",
        "threshold": evidence.threshold,
        "cut_threshold_bits": cut_threshold_bits,
        "samples_per_histogram": _SCENE_CUT_SAMPLES,
        "coverage_factor": _SCENE_CUT_COVERAGE,
    }


def _marker_refusal(
    project: Project,
    context: OperationContext,
    license_admission: Mapping[str, Any],
    gum_admission: Mapping[str, Any],
    extra: Mapping[str, Any],
) -> OperationOutcome:
    """Publish nothing when either the licence or the GUM gate refuses."""
    return OperationOutcome(
        project,
        context.history,
        {
            "gate": dict(license_admission),
            "uncertainty": dict(gum_admission),
            "markers_added": 0,
            "markers_skipped_existing": 0,
            "skipped_outside_clip": 0,
            **extra,
            "refused": True,
        },
    )


def _mark_silences(
    project: Project,
    context: OperationContext,
    *,
    gate: PerceptionGate | None = None,
) -> OperationOutcome:
    payload = MarkSilencesInput.model_validate(context.input_data)
    clip = _find_clip(project, payload.track_id, payload.clip_id)
    evidence = payload.evidence
    _require_same_content(clip, evidence.asset_sha256, OPERATION_MARK_SILENCES)
    license_admission = _marker_admission(gate, payload)
    gum_admission = _gate_record(gate, _silence_gate_context(evidence))
    # Law R31: a gated claim is published only when the gate answers True. An
    # unbound gate (``admitted is None``) and an abstention both fail closed.
    silence_extra = {"spans_in_evidence": len(evidence.spans), "edge": payload.edge}
    license_ok = (not payload.licenses) or license_admission.get("admitted") is True
    if not license_ok:
        return _marker_refusal(project, context, license_admission, gum_admission, silence_extra)
    if gum_admission.get("admitted") is not True:
        return _marker_refusal(project, context, license_admission, gum_admission, silence_extra)

    candidates: list[tuple[str, int, str]] = []
    for index, span in enumerate(evidence.spans, start=1):
        seconds = (span.end_us - span.start_us) / 1_000_000
        if payload.edge in ("start", "both"):
            candidates.append(
                (
                    "silence_start",
                    span.start_us,
                    f"{payload.label_prefix} {index} in ({seconds:.2f}s)",
                )
            )
        if payload.edge in ("end", "both"):
            candidates.append(
                ("silence_end", span.end_us, f"{payload.label_prefix} {index} out ({seconds:.2f}s)")
            )
    new_project, output = _add_markers(project, candidates, clip, evidence.evidence_hash, "silence")
    output["gate"] = license_admission
    output["uncertainty"] = gum_admission
    output["spans_in_evidence"] = len(evidence.spans)
    output["edge"] = payload.edge
    return OperationOutcome(new_project, context.history, output)


def _mark_scene_cuts(
    project: Project,
    context: OperationContext,
    *,
    gate: PerceptionGate | None = None,
) -> OperationOutcome:
    payload = MarkSceneCutsInput.model_validate(context.input_data)
    clip = _find_clip(project, payload.track_id, payload.clip_id)
    evidence = payload.evidence
    _require_same_content(clip, evidence.asset_sha256, OPERATION_MARK_SCENE_CUTS)
    license_admission = _marker_admission(gate, payload)
    gum_admission = _gate_record(gate, _scene_gate_context(evidence, payload.cut_threshold_bits))
    scene_extra = {"cuts_in_evidence": len(evidence.cuts_us), "threshold": evidence.threshold}
    license_ok = (not payload.licenses) or license_admission.get("admitted") is True
    if not license_ok:
        return _marker_refusal(project, context, license_admission, gum_admission, scene_extra)
    if gum_admission.get("admitted") is not True:
        return _marker_refusal(project, context, license_admission, gum_admission, scene_extra)

    candidates = [
        ("scene_cut", cut_us, f"{payload.label_prefix} {index}")
        for index, cut_us in enumerate(evidence.cuts_us, start=1)
    ]
    new_project, output = _add_markers(project, candidates, clip, evidence.evidence_hash, "scenes")
    output["gate"] = license_admission
    output["uncertainty"] = gum_admission
    output["cuts_in_evidence"] = len(evidence.cuts_us)
    output["threshold"] = evidence.threshold
    return OperationOutcome(new_project, context.history, output)


def _assess_loudness(
    project: Project,
    context: OperationContext,
    *,
    gate: PerceptionGate | None = None,
) -> OperationOutcome:
    payload = AssessLoudnessInput.model_validate(context.input_data)
    asset = next((a for a in project.assets if a.asset_id == payload.asset_id), None)
    if asset is None:
        raise CommandValidationError(
            f"{OPERATION_ASSESS_LOUDNESS} references unknown asset: {payload.asset_id!r}"
        )
    if asset.media_kind not in ("audio", "video"):
        raise CommandValidationError(
            f"{OPERATION_ASSESS_LOUDNESS} requires audio or video media, got: {asset.media_kind!r}"
        )
    evidence = payload.evidence
    if asset.content_sha256 != evidence.asset_sha256:
        raise CommandValidationError(
            f"{OPERATION_ASSESS_LOUDNESS}: evidence was measured on different content "
            f"than asset {payload.asset_id!r} (refusing stale or foreign evidence)"
        )

    measured = evidence.integrated_lufs
    admission = _gate_record(
        gate,
        None
        if measured is None
        else {
            "kind": "loudness",
            "licenses": list(payload.licenses),
            "commercial_intent": payload.commercial_intent,
            "integrated_lufs": measured,
            "target_lufs": payload.target_lufs,
            "tolerance_lu": payload.tolerance_lu,
            "coverage_factor": payload.coverage_factor,
            "bit_depth": payload.bit_depth,
            "sample_count": payload.sample_count,
        },
    )
    output: dict[str, Any] = {
        "gate": admission,
        "asset_id": payload.asset_id,
        "evidence_hash": evidence.evidence_hash,
        "integrated_lufs": measured,
        "true_peak_dbfs": evidence.true_peak_dbfs,
        "loudness_range_lu": evidence.loudness_range_lu,
        "target_lufs": payload.target_lufs,
        "true_peak_ceiling_dbfs": payload.true_peak_ceiling_dbfs,
        "tolerance_lu": payload.tolerance_lu,
    }
    if measured is None:
        # No measurement means no gated claim: the honest verdict is "no signal".
        output.update(
            verdict="no_signal",
            gain_db=None,
            projected_true_peak_dbfs=None,
            limiting_required=False,
            normalize_arguments=None,
        )
        return OperationOutcome(project, context.history, output)

    if admission.get("admitted") is not True:
        # Law R31: a refused or unbound admission is terminal -- no gain, no
        # normalize arguments, no projected peak.
        output.update(
            verdict="refused",
            gain_db=None,
            projected_true_peak_dbfs=None,
            limiting_required=False,
            normalize_arguments=None,
        )
        return OperationOutcome(project, context.history, output)

    gain = round(payload.target_lufs - measured, _GAIN_DIGITS)
    projected_peak = (
        None
        if evidence.true_peak_dbfs is None
        else round(evidence.true_peak_dbfs + gain, _GAIN_DIGITS)
    )
    if abs(gain) <= payload.tolerance_lu:
        verdict = "within_tolerance"
    elif gain > 0:
        verdict = "too_quiet"
    else:
        verdict = "too_loud"
    output.update(
        verdict=verdict,
        gain_db=gain,
        projected_true_peak_dbfs=projected_peak,
        limiting_required=(
            projected_peak is not None and projected_peak > payload.true_peak_ceiling_dbfs
        ),
        # Exactly the arguments audio.normalize_loudness takes; it stays a Level-C
        # operation, so the caller still has to confirm it.
        normalize_arguments={
            "audio_asset_id": payload.asset_id,
            "target_lufs": payload.target_lufs,
            "true_peak_db": payload.true_peak_ceiling_dbfs,
        },
    )
    return OperationOutcome(project, context.history, output)


def _bind(
    handler: Callable[..., OperationOutcome], gate: PerceptionGate | None
) -> Callable[[Project, OperationContext], OperationOutcome]:
    """Bind the admission port into one handler (closure, not global state)."""

    def bound(project: Project, context: OperationContext) -> OperationOutcome:
        return handler(project, context, gate=gate)

    bound.__name__ = handler.__name__
    bound.__doc__ = handler.__doc__
    return bound


def register_perception_operations(
    registry: CapabilityRegistry, *, gate: PerceptionGate | None = None
) -> None:
    """Register the perception operations with ``registry``.

    ``gate`` is the admission port. Without it every operation still registers,
    but each record says ``gate.outcome == "not_configured"`` and claims no
    admission decision -- which is what the handler unit tests want, and what the
    production composition root (:mod:`nexus_ai_agent.creative.production_runtime`)
    never does.
    """
    registry.register_operation(
        DOMAIN,
        "silence_marking",
        OperationSpec(
            operation_id=OPERATION_MARK_SILENCES,
            description="Add markers at measured silence edges of a clip (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=MarkSilencesInput,
            handler=_bind(_mark_silences, gate),
            required_packs=(PERCEPTION_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "scene_marking",
        OperationSpec(
            operation_id=OPERATION_MARK_SCENE_CUTS,
            description="Add markers at measured scene changes of a clip (Level B).",
            permission_level=PermissionLevel.REVERSIBLE,
            input_model=MarkSceneCutsInput,
            handler=_bind(_mark_scene_cuts, gate),
            required_packs=(PERCEPTION_PACKAGE_ID,),
            deterministic=True,
        ),
    )
    registry.register_operation(
        DOMAIN,
        "loudness_assessment",
        OperationSpec(
            operation_id=OPERATION_ASSESS_LOUDNESS,
            description="Compare a measured EBU R128 summary with a target (Level A, read-only).",
            permission_level=PermissionLevel.IMMEDIATE,
            input_model=AssessLoudnessInput,
            handler=_bind(_assess_loudness, gate),
            required_packs=(PERCEPTION_PACKAGE_ID,),
            deterministic=True,
        ),
    )


def build_perception_registry(*, gate: PerceptionGate | None = None) -> CapabilityRegistry:
    """A registry pre-loaded with the perception operations only."""
    registry = CapabilityRegistry()
    register_perception_operations(registry, gate=gate)
    return registry


__all__ = [
    "PerceptionGate",
    "build_perception_registry",
    "register_perception_operations",
]
