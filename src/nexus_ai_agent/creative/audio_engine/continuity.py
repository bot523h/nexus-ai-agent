"""Episodic continuity: zero-drift comparison and lossless stitch proof.

The North-Star requirement is that episode 2 must not drift from episode 1 — in
face, in scale, in colour, in voice, in loudness and in stream shape.  Nothing
in this module *creates* identity; it compares recorded measurements and says,
in typed terms, whether two segments can be presented as one continuous
programme.

Two operations, both honest about their limits:

:func:`compare_continuity`
    Takes two :class:`~nexus_ai_agent.creative.audio_engine.verify.ArtifactPassport`
    objects (or their measured parts) and reports per-axis drift with explicit
    tolerances.  A drift is *reported*, never auto-corrected, and an
    unmeasurable axis is reported as ``unknown`` — never as ``ok``.

:func:`assert_lossless_assembly`
    Proves that a list of segments may be concatenated with ``-c copy``: same
    sample rate, channel layout, codec, sample format, and a container whose
    stream parameters do not change between segments.  If the proof fails, the
    caller must re-encode; the engine will not silently produce a master that
    drops frames at a format change.

The axis definitions are the North Star's, not invented here:

* **metric/visual** — ``color_space``, ``camera_scale_profile``, character and
  scene sets must be *identical* (a different colour space cannot be graded back
  later without a generation loss);
* **vocal** — the acoustic signature must be close: RMS within 1.5 dB, crest
  within 2.0 dB, spectral centroid within 12 % (a different microphone or room
  moves the centroid far more than that), band-energy distribution within a
  cosine distance of 0.08;
* **loudness** — both segment passports must have *passed* their loudness
  target, and the two measured integrated values must agree within 1.0 LU;
* **stream** — the canonical profile hash must match, which is exactly the
  "stitchable without re-encode" property.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from nexus_ai_agent.creative.audio_engine.models import (
    CanonicalStreamProfile,
    ContinuityLockSpec,
    MediaProbe,
    SpectralSignature,
)

__all__ = [
    "AxisVerdict",
    "ContinuityReport",
    "AssemblyProof",
    "ContinuityError",
    "TOLERANCES",
    "compare_continuity",
    "assert_lossless_assembly",
    "detect_lock_drift",
]

#: One place for every tolerance (a test asserts they are the documented values).
TOLERANCES: dict[str, float] = {
    "rms_db": 1.5,
    "crest_db": 2.0,
    "centroid_relative": 0.12,
    "band_cosine_distance": 0.08,
    "integrated_lu": 1.0,
    "duration_relative": 0.001,
}

Verdict = Literal["ok", "drift", "unknown"]


class ContinuityError(ValueError):
    """Two segments cannot be compared as the caller asked (fail-closed)."""


class AxisVerdict(BaseModel):
    """The verdict on one continuity axis, with the numbers that produced it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    axis: str
    verdict: Verdict
    reference: float | str | None = None
    candidate: float | str | None = None
    delta: float | None = None
    tolerance: float | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.verdict == "ok"


class ContinuityReport(BaseModel):
    """Every axis, plus the one question the caller actually asks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference_id: str
    candidate_id: str
    axes: tuple[AxisVerdict, ...]
    lock_hash_match: bool
    stitchable: bool
    drift_axes: tuple[str, ...] = ()
    unknown_axes: tuple[str, ...] = ()

    @property
    def continuous(self) -> bool:
        """No axis drifted and no axis is unmeasurable."""
        return not self.drift_axes and not self.unknown_axes

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(f"{axis.axis}:{axis.verdict}" for axis in self.axes if axis.verdict != "ok")


@dataclass(frozen=True)
class AssemblyProof:
    """The evidence that a stream-copy concatenation is quality-neutral."""

    lossless: bool
    reason: str | None
    reference_profile: CanonicalStreamProfile
    segments: tuple[str, ...] = field(default=())
    probed_sample_rates: tuple[int, ...] = ()
    probed_layouts: tuple[str, ...] = ()

    @property
    def strategy(self) -> Literal["stream_copy", "re_encode_required"]:
        return "stream_copy" if self.lossless else "re_encode_required"


def _signature_of(passport_or_signature: object) -> SpectralSignature | None:
    signature = getattr(passport_or_signature, "signature", None)
    if signature is None and isinstance(passport_or_signature, SpectralSignature):
        return passport_or_signature
    return signature


def _numeric_axis(
    axis: str,
    reference: float | None,
    candidate: float | None,
    *,
    tolerance: float,
    relative: bool = False,
    unit: str = "",
) -> AxisVerdict:
    if reference is None or candidate is None:
        return AxisVerdict(
            axis=axis,
            verdict="unknown",
            reference=reference,
            candidate=candidate,
            tolerance=tolerance,
            detail="one of the two segments has no measured value for this axis",
        )
    if relative:
        scale = max(abs(reference), 1e-9)
        delta = (candidate - reference) / scale
    else:
        delta = candidate - reference
    verdict: Verdict = "ok" if abs(delta) <= tolerance else "drift"
    return AxisVerdict(
        axis=axis,
        verdict=verdict,
        reference=round(reference, 4),
        candidate=round(candidate, 4),
        delta=round(delta, 4),
        tolerance=tolerance,
        detail=None
        if verdict == "ok"
        else f"{axis} moved {delta:+.4f}{unit} (tolerance {tolerance:g})",
    )


def _band_distance(left: dict[str, float], right: dict[str, float]) -> float | None:
    keys = sorted(set(left) & set(right))
    if not keys:
        return None
    a = [left[key] for key in keys]
    b = [right[key] for key in keys]
    norm_a = sum(value * value for value in a) ** 0.5
    norm_b = sum(value * value for value in b) ** 0.5
    if norm_a <= 0 or norm_b <= 0:
        return None
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return float(1.0 - dot / (norm_a * norm_b))


def compare_continuity(
    reference: object,
    candidate: object,
    *,
    lock: ContinuityLockSpec | None = None,
) -> ContinuityReport:
    """Compare two segments' measured continuity axes.

    Accepts anything carrying ``signature``, ``loudness``, ``loudness_target``,
    ``probe`` and ``continuity_lock_hash`` attributes — in practice
    :class:`~nexus_ai_agent.creative.audio_engine.verify.ArtifactPassport`.
    """
    reference_id = str(getattr(reference, "passport_id", getattr(reference, "asset_id", "ref")))
    candidate_id = str(getattr(candidate, "passport_id", getattr(candidate, "asset_id", "cand")))
    axes: list[AxisVerdict] = []

    # -- lock identity -----------------------------------------------------
    reference_lock = getattr(reference, "continuity_lock_hash", None)
    candidate_lock = getattr(candidate, "continuity_lock_hash", None)
    if lock is not None:
        expected = lock.lock_hash
        lock_match = reference_lock == expected and candidate_lock == expected
        axes.append(
            AxisVerdict(
                axis="continuity_lock",
                verdict="ok" if lock_match else "drift",
                reference=expected,
                candidate=candidate_lock,
                detail=None
                if lock_match
                else (
                    f"segments were produced under different continuity locks "
                    f"(reference={reference_lock}, candidate={candidate_lock}, expected={expected})"
                ),
            )
        )
    else:
        if reference_lock is None and candidate_lock is None:
            axes.append(
                AxisVerdict(
                    axis="continuity_lock",
                    verdict="unknown",
                    detail="neither segment records a continuity lock hash",
                )
            )
            lock_match = False
        else:
            lock_match = reference_lock == candidate_lock
            axes.append(
                AxisVerdict(
                    axis="continuity_lock",
                    verdict="ok" if lock_match else "drift",
                    reference=reference_lock,
                    candidate=candidate_lock,
                    detail=None if lock_match else "segments were produced under different locks",
                )
            )

    # -- stream shape ------------------------------------------------------
    ref_probe = getattr(reference, "probe", None)
    cand_probe = getattr(candidate, "probe", None)
    if isinstance(ref_probe, MediaProbe) and isinstance(cand_probe, MediaProbe):
        for label, pick in (
            ("stream_sample_rate", lambda probe: probe.sample_rate),
            ("stream_channels", lambda probe: probe.channels),
            ("stream_codec", lambda probe: probe.codec),
            ("stream_sample_format", lambda probe: probe.sample_format),
        ):
            left, right = pick(ref_probe), pick(cand_probe)
            unmeasurable = left is None or right is None
            axes.append(
                AxisVerdict(
                    axis=label,
                    verdict="unknown" if unmeasurable else ("ok" if left == right else "drift"),
                    reference=left,
                    candidate=right,
                    detail=None
                    if unmeasurable or left == right
                    else f"{label} differs ({left!r} vs {right!r}); a stitch would need an encode",
                )
            )
    else:
        axes.append(
            AxisVerdict(
                axis="stream",
                verdict="unknown",
                detail="at least one segment carries no probe evidence",
            )
        )

    # -- loudness ----------------------------------------------------------
    ref_target = getattr(reference, "loudness_target", None)
    cand_target = getattr(candidate, "loudness_target", None)
    for label, record in (("reference", ref_target), ("candidate", cand_target)):
        if record is not None and not getattr(record, "passed", False):
            axes.append(
                AxisVerdict(
                    axis=f"loudness_target_{label}",
                    verdict="drift",
                    detail=f"{label} segment did not reach its loudness target "
                    f"({getattr(record, 'code', None)})",
                )
            )
    ref_loudness = getattr(reference, "loudness", None)
    cand_loudness = getattr(candidate, "loudness", None)
    axes.append(
        _numeric_axis(
            "integrated_lufs",
            getattr(ref_loudness, "integrated_lufs", None),
            getattr(cand_loudness, "integrated_lufs", None),
            tolerance=TOLERANCES["integrated_lu"],
            unit=" LU",
        )
    )

    # -- vocal / acoustic --------------------------------------------------
    ref_sig = _signature_of(reference)
    cand_sig = _signature_of(candidate)
    if ref_sig is None or cand_sig is None:
        axes.append(
            AxisVerdict(
                axis="acoustic_signature",
                verdict="unknown",
                detail="at least one segment carries no acoustic signature",
            )
        )
    else:
        axes.append(
            _numeric_axis(
                "vocal_rms_dbfs",
                ref_sig.rms_dbfs,
                cand_sig.rms_dbfs,
                tolerance=TOLERANCES["rms_db"],
                unit=" dB",
            )
        )
        axes.append(
            _numeric_axis(
                "vocal_crest_db",
                ref_sig.crest_factor_db,
                cand_sig.crest_factor_db,
                tolerance=TOLERANCES["crest_db"],
                unit=" dB",
            )
        )
        axes.append(
            _numeric_axis(
                "vocal_spectral_centroid",
                ref_sig.spectral_centroid_hz,
                cand_sig.spectral_centroid_hz,
                tolerance=TOLERANCES["centroid_relative"],
                relative=True,
            )
        )
        distance = _band_distance(ref_sig.band_energy_ratio, cand_sig.band_energy_ratio)
        axes.append(
            AxisVerdict(
                axis="vocal_band_distribution",
                verdict="unknown"
                if distance is None
                else ("ok" if distance <= TOLERANCES["band_cosine_distance"] else "drift"),
                candidate=round(distance, 6) if distance is not None else None,
                tolerance=TOLERANCES["band_cosine_distance"],
                detail=None
                if distance is None or distance <= TOLERANCES["band_cosine_distance"]
                else f"spectral band distribution moved (cosine distance {distance:.4f})",
            )
        )

    drift_axes = tuple(axis.axis for axis in axes if axis.verdict == "drift")
    unknown_axes = tuple(axis.axis for axis in axes if axis.verdict == "unknown")
    return ContinuityReport(
        reference_id=reference_id,
        candidate_id=candidate_id,
        axes=tuple(axes),
        lock_hash_match=lock_match,
        stitchable=lock_match and not drift_axes and not unknown_axes,
        drift_axes=drift_axes,
        unknown_axes=unknown_axes,
    )


def detect_lock_drift(
    reference: ContinuityLockSpec, candidate: ContinuityLockSpec
) -> tuple[str, ...]:
    """Field-by-field drift between two continuity locks (empty = identical).

    This is the cheap, pre-production check: it compares the *declared* locks
    before any media exists, so a drifted episode is refused at planning time
    rather than discovered after a 40-minute render.
    """
    findings: list[str] = []
    if reference.stream != candidate.stream:
        findings.append(
            "stream profile differs: "
            f"{reference.stream.model_dump()} vs {candidate.stream.model_dump()}"
        )
    if reference.loudness != candidate.loudness:
        findings.append(
            f"loudness profile differs: {reference.loudness.profile_id!r} vs "
            f"{candidate.loudness.profile_id!r}"
        )
    left = reference.identity
    right = candidate.identity
    for name in (
        "color_space",
        "camera_scale_profile",
        "vocal_signature_sha256",
        "series_id",
    ):
        left_value, right_value = getattr(left, name), getattr(right, name)
        if left_value != right_value:
            findings.append(f"{name} differs: {left_value!r} vs {right_value!r}")
    if left.episode_index is not None and right.episode_index is not None:
        if right.episode_index <= left.episode_index:
            findings.append(
                f"candidate episode_index {right.episode_index} does not follow "
                f"reference {left.episode_index}"
            )
    return tuple(findings)


def assert_lossless_assembly(
    probes: list[MediaProbe] | tuple[MediaProbe, ...],
    *,
    profile: CanonicalStreamProfile,
    names: tuple[str, ...] = (),
) -> AssemblyProof:
    """Prove a ``-c copy`` concatenation is lossless, or say why it is not.

    Checks the four container-level facts ``-c copy`` cannot fix: sample rate,
    channel count, codec and sample format.  Durations may differ (that is the
    point of concatenation); anything else differing forces a re-encode.
    """
    if not probes:
        raise ContinuityError("cannot assemble zero segments")
    sample_rates = tuple(sorted({probe.sample_rate for probe in probes if probe.sample_rate}))
    layouts = tuple(sorted({str(probe.channels) for probe in probes if probe.channels}))
    codecs = tuple(sorted({str(probe.codec) for probe in probes if probe.codec}))
    formats = tuple(sorted({str(probe.sample_format) for probe in probes if probe.sample_format}))
    reasons: list[str] = []
    if len(sample_rates) > 1:
        reasons.append(f"sample rates differ {sample_rates}")
    if len(layouts) > 1:
        reasons.append(f"channel counts differ {layouts}")
    if len(codecs) > 1:
        reasons.append(f"codecs differ {codecs}")
    if len(formats) > 1:
        reasons.append(f"sample formats differ {formats}")
    if sample_rates and sample_rates != (profile.sample_rate,):
        reasons.append(
            f"segments are not at the canonical rate {profile.sample_rate} (got {sample_rates})"
        )
    expected_codec = profile.codec
    if codecs and codecs != (expected_codec,):
        reasons.append(f"segments are not {expected_codec} (got {codecs})")
    return AssemblyProof(
        lossless=not reasons,
        reason="; ".join(reasons) if reasons else None,
        reference_profile=profile,
        segments=names,
        probed_sample_rates=sample_rates,
        probed_layouts=layouts,
    )


def assembly_names(paths: tuple[Path | str, ...]) -> tuple[str, ...]:
    return tuple(Path(path).name for path in paths)
