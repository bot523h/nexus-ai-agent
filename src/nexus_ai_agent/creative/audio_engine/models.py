"""Typed contracts of the native media execution plane (OMEGA-01 / OMEGA-02).

Everything in this module is a **measurement** or a **lock**, never a promise:

* :class:`CanonicalStreamProfile` — the one stream shape every produced segment
  must have so a series can be stitched without a second encode;
* :class:`LoudnessProfile` + :class:`LOUDNESS_PROFILES` — the two broadcast
  targets the studio actually ships (EBU R128 broadcast, streaming master) plus
  a validated custom mode;
* :class:`LoudnessMeasurement` / :class:`MediaProbe` / :class:`DecodeEvidence` /
  :class:`SpectralSignature` — what a verifier read *back off the disk*, with
  ``None`` (never ``0.0``, never ``-inf``) whenever there is no program
  material to measure.  ``0.0`` for a silent file and a real ``0.0`` LUFS for a
  mastered file are different facts and must not collapse into one;
* :class:`LoudnessTargetCheck` — the explicit pass/fail of a loudness target
  with a machine-readable refusal code.

Design rules (enforced by ``tests/unit/test_omega_native_contract.py``):

1. integer microseconds for time, ``sha256:<hex>`` for content identity —
   the same conventions as :mod:`nexus_ai_agent.creative.studio.models`;
2. no I/O in this module: models describe and validate, they never measure;
3. a tolerance is stated next to the target it belongs to, so a verifier can
   never widen it silently.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 1

#: The identity of the locked stream shape (bump on any incompatible change).
CANONICAL_PROFILE_ID = "nagar.canonical_audio.v1"

_SHA256_REF = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Sample rates the canonical profile may declare.  Anything else is a typo or
#: an exotic source that must be resampled first, not silently accepted.
SUPPORTED_SAMPLE_RATES = frozenset({44_100, 48_000, 88_200, 96_000, 192_000})

# --- loudness tolerances ---------------------------------------------------- #
#: EBU R128 measurement uncertainty on a short programme is ~0.1 LU; a single
#: LU of headroom keeps a real measurement pass honest without letting a
#: half-normalized file through.  One constant, one meaning, used by every
#: caller (engine, verifier, tests).
INTEGRATED_TOLERANCE_LU = 1.0
#: True peak is a hard ceiling: we allow this much *measurement* slack below the
#: limit, and never any sample above 0 dBTP.
TRUE_PEAK_TOLERANCE_DB = 0.5
HARD_TRUE_PEAK_CEILING_DB = 0.0

#: Programme shorter than this cannot be normalized reliably (EBU R128 gating
#: blocks are 400 ms); the engine refuses instead of guessing.
MIN_PROGRAMME_SECONDS = 1.0


class MediaContractError(ValueError):
    """A media contract value is internally inconsistent (fail-closed)."""


class LoudnessProfileId(str):
    """Marker type for the locked loudness profiles (documentation only)."""


LoudnessProfileName = Literal["nagar_streaming_preset", "ebu_r128_broadcast", "custom"]

#: How a profile is allowed to reach its integrated target.
#:
#: ``STRICT_LINEAR`` — gain only.  The peak ceiling is an *absolute* refusal:
#: a programme whose crest factor needs more than ``TP - target`` dB cannot be
#: rendered, because the only linear gain that lands on the target also puts the
#: peaks over the ceiling.  A limiter would fix the peak by deleting the energy
#: the target asks for, which is a false success.
#:
#: ``DYNAMIC_ALLOWED`` — gain, then a true-peak limiter.  The file genuinely
#: reduces dynamic range, so the *measured* crest reduction is recorded in the
#: passport (``crest_reduction_db``) and the caller is told the mode is
#: ``DYNAMIC``.  A limiter that never acts is reported as ``DYNAMIC_ALLOWED``
#: saturation, not as loss.
ProcessingMode = Literal["STRICT_LINEAR", "DYNAMIC_ALLOWED"]


class LoudnessProfile(BaseModel):
    """A loudness target triple: integrated LUFS, true peak ceiling, LRA ceiling.

    ``profile_id`` is the *contract name* a caller may pin; the three numbers
    are the target itself.  A locked profile's numbers are constants — only the
    ``custom`` profile may carry caller-chosen values, and only when the caller
    states all three (see :func:`resolve_loudness_profile`).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: LoudnessProfileName
    target_lufs: float = Field(ge=-70.0, le=0.0)
    true_peak_dbtp: float = Field(ge=-20.0, le=0.0)
    target_lra_lu: float = Field(ge=0.0, le=50.0)
    standard: str = "ITU-R BS.1770-4 / EBU R128"
    #: Whether a peak limiter may be used to reach the target.  See
    #: :data:`ProcessingMode`; the default refuses dynamic processing.
    processing_mode: ProcessingMode = "STRICT_LINEAR"
    #: The most crest factor a dynamic render may remove to reach the target, in
    #: dB.  ``None`` for a linear profile (it may remove none).  This is a
    #: *destruction* bound, not a tuning knob: a per-click programme whose crest
    #: is 27.8 dB can only be pushed to -14 LUFS by removing ~15 dB of it, i.e. by
    #: turning transients into mush.  Refusing is the honest outcome — the number
    #: is reachable, the master is not.
    max_crest_reduction_db: float | None = None

    @property
    def locked(self) -> bool:
        return self.profile_id != "custom"

    def ffmpeg_loudnorm_targets(self) -> tuple[str, str, str]:
        """The ``loudnorm`` target arguments (``I``, ``TP``, ``LRA``)."""
        return (
            f"I={self.target_lufs:g}",
            f"TP={self.true_peak_dbtp:g}",
            f"LRA={self.target_lra_lu:g}",
        )


EBU_R128_BROADCAST = LoudnessProfile(
    profile_id="ebu_r128_broadcast",
    target_lufs=-23.0,
    true_peak_dbtp=-1.0,
    target_lra_lu=11.0,
    standard="EBU R128 broadcast (ITU-R BS.1770-4)",
    processing_mode="STRICT_LINEAR",
)

#: -14 LUFS / -1 dBTP is a *delivery preset*, not a broadcast standard, and it
#: is named as such: nothing in ITU-R BS.1770 or EBU R128 mandates -14 LUFS.
#: Streaming services normalise on playback, so a master louder than the service
#: target is turned back down — which is exactly why the dynamic path is allowed
#: here and refused for the broadcast profile.
NAGAR_STREAMING_PRESET = LoudnessProfile(
    profile_id="nagar_streaming_preset",
    target_lufs=-14.0,
    true_peak_dbtp=-1.0,
    target_lra_lu=11.0,
    standard="NAGAR streaming delivery preset (-14 LUFS) — not a broadcast standard",
    processing_mode="DYNAMIC_ALLOWED",
    max_crest_reduction_db=9.0,
)

#: Every *locked* profile the studio ships.  ``custom`` is deliberately absent:
#: it is constructed per request and validated by :func:`resolve_loudness_profile`.
LOUDNESS_PROFILES: dict[str, LoudnessProfile] = {
    EBU_R128_BROADCAST.profile_id: EBU_R128_BROADCAST,
    NAGAR_STREAMING_PRESET.profile_id: NAGAR_STREAMING_PRESET,
}


def resolve_loudness_profile(
    profile_id: str | None,
    *,
    target_lufs: float | None = None,
    true_peak_dbtp: float | None = None,
    target_lra_lu: float | None = None,
) -> LoudnessProfile:
    """Resolve the requested target, refusing ambiguity.

    * a locked profile id wins, and any *differing* explicit number is refused
      (a caller cannot pin EBU R128 and ask for -14 LUFS: one of them is a lie);
    * ``None``/``"custom"`` requires all three numbers explicitly — there is no
      house default to fall back on, because a default loudness target is
      exactly how "display-only" normalization starts;
    * every value is range-checked by :class:`LoudnessProfile` itself.
    """
    if profile_id not in (None, "custom"):
        try:
            locked = LOUDNESS_PROFILES[profile_id]
        except KeyError:
            raise MediaContractError(
                f"unknown loudness profile {profile_id!r} "
                f"(known: {sorted(LOUDNESS_PROFILES)} or 'custom')"
            ) from None
        for label, given, expected in (
            ("target_lufs", target_lufs, locked.target_lufs),
            ("true_peak_dbtp", true_peak_dbtp, locked.true_peak_dbtp),
            ("target_lra_lu", target_lra_lu, locked.target_lra_lu),
        ):
            if given is not None and abs(given - expected) > 1e-9:
                raise MediaContractError(
                    f"profile {profile_id!r} locks {label}={expected:g}; "
                    f"request asked for {given:g}"
                )
        return locked

    if target_lufs is None or true_peak_dbtp is None or target_lra_lu is None:
        raise MediaContractError(
            "a custom loudness target must state target_lufs, true_peak_dbtp and "
            "target_lra_lu explicitly (no implicit default)"
        )
    try:
        return LoudnessProfile(
            profile_id="custom",
            target_lufs=target_lufs,
            true_peak_dbtp=true_peak_dbtp,
            target_lra_lu=target_lra_lu,
        )
    except ValueError as error:  # pydantic range failure → one contract error
        raise MediaContractError(f"invalid custom loudness target: {error}") from error


class CanonicalStreamProfile(BaseModel):
    """The locked stream shape of every segment the studio produces.

    A series is only stitchable without a second encode when *all* segments
    share the same sample rate, channel layout, codec and sample format.  This
    profile is that lock; the engine enforces it on every output, and the
    verifier re-reads it back from the produced file.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str = CANONICAL_PROFILE_ID
    sample_rate: int = Field(default=48_000, ge=8_000, le=192_000)
    channel_layout: Literal["mono", "stereo"] = "stereo"
    codec: Literal["flac", "pcm_s24le"] = "flac"
    sample_format: Literal["s16", "s32"] = "s32"
    #: ``True`` (default): sources are converted to this shape.  ``False``: the
    #: source layout is preserved and only the sample rate/codec are locked —
    #: for callers mixing mono interviews into a stereo series deliberately.
    enforce_channel_layout: bool = True

    @model_validator(mode="after")
    def _supported_sample_rate(self) -> CanonicalStreamProfile:
        if self.sample_rate not in SUPPORTED_SAMPLE_RATES:
            raise ValueError(
                f"sample_rate {self.sample_rate} is not a supported canonical rate "
                f"({sorted(SUPPORTED_SAMPLE_RATES)})"
            )
        return self

    @property
    def channels(self) -> int:
        return 1 if self.channel_layout == "mono" else 2

    @property
    def container(self) -> str:
        """File suffix for this profile (``.flac`` / ``.wav``)."""
        return ".flac" if self.codec == "flac" else ".wav"

    def format_arguments(self) -> tuple[str, ...]:
        """The argv fragment that forces this shape on an encoder.

        Ordered: resample first, then channel layout, then rate/format.  The
        loudness pass must run **after** this (an up-mix changes integrated
        loudness by ~3 dB, so measuring the source layout and then changing it
        would target the wrong number).
        """
        args = ["-ar", str(self.sample_rate)]
        if self.enforce_channel_layout:
            args += ["-ac", str(self.channels)]
        args += ["-c:a", self.codec, "-sample_fmt", self.sample_format]
        return tuple(args)

    #: FFmpeg's ``loudnorm`` upsamples its input to 192 kHz internally to run its
    #: true-peak detector, and in older builds leaves the stream there.  Anything
    #: chained *after* it, and the muxer, therefore see 192 kHz unless the
    #: canonical rate is re-asserted afterwards.  This constant names the rate so
    #: the guard is a checked contract rather than a comment.
    LOUDNORM_INTERNAL_RATE: ClassVar[int] = 192_000

    def loudnorm_rate_guard(self) -> tuple[str, ...]:
        """Filters that restore the canonical rate after a ``loudnorm`` stage.

        A no-op when the canonical rate is already 192 kHz (nothing to restore),
        which keeps the graph honest instead of inserting a redundant resampler
        and claiming credit for it.
        """
        return (
            ()
            if self.sample_rate == self.LOUDNORM_INTERNAL_RATE
            else (f"aresample={self.sample_rate}",)
        )

    def filter_arguments(self) -> tuple[str, ...]:
        """The filter fragment mirroring :meth:`format_arguments`."""
        filters = [f"aresample={self.sample_rate}"]
        if self.enforce_channel_layout:
            filters.append(
                "aformat=channel_layouts=mono"
                if self.channel_layout == "mono"
                else "aformat=channel_layouts=stereo"
            )
        return tuple(filters)


CANONICAL_PROFILE = CanonicalStreamProfile()


class IdentityLockSpec(BaseModel):
    """The episodic identity fields a segment is produced *under*.

    This is the *base* of the visual/vocal continuity lock, deliberately typed
    as references (ids/hashes), not as pixels: the identity registries are
    owned elsewhere and this plane must not become a second one.  A segment
    passport carries the hash of these fields, so a later episode that drifts
    (different character registry, different colour space, different camera
    scale profile) is detectable by comparison, not by memory.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    series_id: str | None = Field(default=None, min_length=1, max_length=128)
    episode_id: str | None = Field(default=None, min_length=1, max_length=128)
    episode_index: int | None = Field(default=None, ge=0)
    character_ids: tuple[str, ...] = ()
    scene_ids: tuple[str, ...] = ()
    #: Colour space the visual lane must grade to (e.g. ``bt709``).
    color_space: str | None = Field(default=None, max_length=64)
    #: The metric camera/scale profile id locked for the series.
    camera_scale_profile: str | None = Field(default=None, max_length=128)
    #: ``sha256:`` of the vocal acoustic fingerprint bound to this segment.
    vocal_signature_sha256: str | None = None

    @model_validator(mode="after")
    def _validate_hashes(self) -> IdentityLockSpec:
        if self.vocal_signature_sha256 is not None and not _SHA256_REF.match(
            self.vocal_signature_sha256
        ):
            raise ValueError("vocal_signature_sha256 must be 'sha256:<64 hex>'")
        if len(set(self.character_ids)) != len(self.character_ids):
            raise ValueError("character_ids must be unique")
        if len(set(self.scene_ids)) != len(self.scene_ids):
            raise ValueError("scene_ids must be unique")
        return self


class ContinuityLockSpec(BaseModel):
    """The full episodic lock: stream shape + loudness target + identity.

    One spec is produced per series/episode and hashed; every artifact passport
    records that hash.  :func:`nexus_ai_agent.creative.audio_engine.continuity.detect_drift`
    is what turns two segments' passports into a "these can be stitched
    losslessly / these cannot" verdict.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    spec_id: str = Field(min_length=1, max_length=128)
    stream: CanonicalStreamProfile = CANONICAL_PROFILE
    loudness: LoudnessProfile = NAGAR_STREAMING_PRESET
    identity: IdentityLockSpec = Field(default_factory=IdentityLockSpec)

    def canonical_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @property
    def lock_hash(self) -> str:
        return canonical_hash(self.canonical_payload())


def canonical_json(value: Any) -> str:
    """Canonical JSON: sorted keys, no whitespace, no NaN/inf, UTF-8 safe."""
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:  # pragma: no cover - contract guard
        raise MediaContractError(f"value is not canonically serialisable: {error}") from error


def canonical_hash(value: Any) -> str:
    """``sha256:<hex>`` over :func:`canonical_json` (the repo's identity form)."""
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_of_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# measurements — all read back from disk, never derived from an intent
# --------------------------------------------------------------------------- #


class MediaProbe(BaseModel):
    """Container/stream facts read by the allow-listed binary.

    The fields from ``profile`` down are the typed contract of a *machine*
    probe (``ffprobe -of json``): they exist so a verdict about a stream's
    shape is made from a parsed document with required keys rather than from a
    human-readable ``ffmpeg -i`` banner.  They are optional because a probe
    taken by a binary that cannot emit JSON (or a caller that only needs the
    container facts) leaves them ``None`` — and ``None`` is honestly "not
    established here", never a guessed value.

    ``source`` records exactly which probe produced the record, so downstream
    evidence can tell a JSON probe from a banner parse without consulting the
    code version that happened to be deployed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    container_duration_us: int | None = Field(default=None, ge=0)
    sample_rate: int | None = Field(default=None, gt=0)
    channels: int | None = Field(default=None, gt=0)
    channel_layout: str | None = None
    codec: str | None = None
    sample_format: str | None = None
    size_bytes: int = Field(ge=0)
    sha256: str
    # -- machine-probe contract (ffprobe -of json) ----------------------- #
    profile: str | None = None
    pix_fmt: str | None = None
    time_base: str | None = None
    start_pts: int | None = None
    duration_ts: int | None = None
    extradata_hash: str | None = None
    source: Literal["ffprobe_json", "banner"] = "banner"

    @property
    def is_stereo(self) -> bool:
        return self.channels == 2

    @property
    def is_mono(self) -> bool:
        return self.channels == 1


class LoudnessMeasurement(BaseModel):
    """An EBU R128 measurement.

    ``silent=True`` is the only representation of ``-inf``: every numeric field
    is then ``None``.  That is what lets the engine *refuse* a silent file
    instead of "successfully normalizing" it to a meaningless target.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    integrated_lufs: float | None = None
    true_peak_dbtp: float | None = None
    loudness_range_lu: float | None = None
    relative_threshold_lufs: float | None = None
    silent: bool = False
    measured_by: str = "ffmpeg.ebur128"
    measurement_seconds: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def _silence_is_all_or_nothing(self) -> LoudnessMeasurement:
        numeric = (
            self.integrated_lufs,
            self.true_peak_dbtp,
            self.loudness_range_lu,
        )
        if self.silent and any(value is not None for value in numeric):
            raise ValueError("a silent measurement cannot carry numeric loudness")
        if not self.silent and self.integrated_lufs is None:
            raise ValueError("a non-silent measurement must carry integrated_lufs")
        return self


class RenderOutcome(BaseModel):
    """What a render builder hands back to the execution plane.

    The artifact path alone is not enough: a loudness render also *knows* what
    it did to the programme's dynamics.  Carrying that number back is the only
    way the passport can state a measured crest reduction instead of reciting
    the profile it was asked for.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    path: Path
    #: Peak-to-RMS of the programme the gain was computed from, in dB.
    source_crest_factor_db: float | None = None
    #: The mode the render actually used (``None`` when the operation has no
    #: loudness target at all, e.g. denoise).
    processing_mode: ProcessingMode | None = None


class LoudnessTargetCheck(BaseModel):
    """Did the produced file actually land on the target?  Explicit verdict."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str
    target_lufs: float
    true_peak_limit_dbtp: float
    #: The mode the profile allowed (``STRICT_LINEAR`` gain only, or
    #: ``DYNAMIC_ALLOWED`` gain + peak limiting).
    processing_mode: ProcessingMode = "STRICT_LINEAR"
    #: How much crest factor the render actually removed, in dB, measured on the
    #: published file against the source.  ``None`` when no source crest factor
    #: was supplied — a missing measurement, never an assumed zero.
    crest_reduction_db: float | None = None
    measured_integrated_lufs: float | None
    measured_true_peak_dbtp: float | None
    integrated_error_lu: float | None
    integrated_tolerance_lu: float = INTEGRATED_TOLERANCE_LU
    true_peak_tolerance_db: float = TRUE_PEAK_TOLERANCE_DB
    integrated_ok: bool
    true_peak_ok: bool
    passed: bool
    code: str | None = None
    detail: str | None = None


class DecodeEvidence(BaseModel):
    """A full-file decode of the produced artifact (not the encoder's word)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decode_ok: bool
    decoded_samples: int | None = Field(default=None, ge=0)
    decoded_duration_us: int | None = Field(default=None, ge=0)
    stderr_sha256: str | None = None
    elapsed_seconds: float = Field(default=0.0, ge=0.0)
    code: str | None = None
    detail: str | None = None


class SpectralSignature(BaseModel):
    """The acoustic identity of a decoded signal (vocal continuity evidence).

    Every number is computed from the **decoded PCM of the final file**, over a
    bounded analysis window; ``analysis_seconds`` states exactly how much of the
    file was covered so a short read is never mistaken for a stable signature.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    samples_analyzed: int = Field(ge=0)
    analysis_seconds: float = Field(ge=0.0)
    truncated: bool = False
    rms_dbfs: float | None = None
    peak_dbfs: float | None = None
    crest_factor_db: float | None = None
    dynamic_range_db: float | None = None
    spectral_centroid_hz: float | None = None
    zero_crossing_rate: float | None = None
    band_energy_ratio: dict[str, float] = Field(default_factory=dict)
    integrated_lufs: float | None = None

    @property
    def signature_payload(self) -> dict[str, Any]:
        """The subset that defines identity (no window bookkeeping)."""
        return {
            "rms_dbfs": _round(self.rms_dbfs),
            "peak_dbfs": _round(self.peak_dbfs),
            "crest_factor_db": _round(self.crest_factor_db),
            "dynamic_range_db": _round(self.dynamic_range_db),
            "spectral_centroid_hz": _round(self.spectral_centroid_hz),
            "zero_crossing_rate": _round(self.zero_crossing_rate),
            "band_energy_ratio": {
                band: _round(value) for band, value in sorted(self.band_energy_ratio.items())
            },
            "integrated_lufs": _round(self.integrated_lufs),
        }

    @property
    def signature_hash(self) -> str:
        """Content identity of the acoustic signature (stable across re-reads)."""
        return canonical_hash(self.signature_payload)


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


class ResourceBudget(BaseModel):
    """The resource ceiling a bounded run was given, plus what it actually used.

    Every ``peak_*``/``*_bytes`` field here is a **measurement**, not an
    estimate: input size comes from ``stat``, decoded samples from the decoder's
    own frame count, wall clock and captured bytes from the process governor's
    accounting, and peak RSS from the kernel's ``VmHWM``.

    :attr:`peak_rss_bytes` is the one field that can legitimately be ``None``:
    a host without ``/proc`` cannot answer, and the honest answer there is
    ``NOT_MEASURED`` (:attr:`rss_measured`), never ``0``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    deadline_seconds: float = Field(gt=0.0)
    max_captured_bytes: int = Field(gt=0)
    max_analysis_bytes: int = Field(gt=0)
    elapsed_seconds: float = Field(ge=0.0)
    peak_captured_bytes: int = Field(ge=0, default=0)
    peak_analysis_bytes: int = Field(ge=0, default=0)
    processes_started: int = Field(ge=0, default=0)
    # -- the measurements §4.8 requires ---------------------------------- #
    input_size_bytes: int = Field(ge=0, default=0)
    input_duration_us: int = Field(ge=0, default=0)
    decoded_samples: int = Field(ge=0, default=0)
    output_size_bytes: int = Field(ge=0, default=0)
    staging_quota_bytes: int = Field(gt=0, default=8 * 1024**3)
    staging_peak_bytes: int = Field(ge=0, default=0)
    threads: int | None = Field(default=None, ge=1)
    peak_rss_bytes: int | None = Field(default=None, ge=0)
    ffmpeg_version: str | None = None

    @property
    def rss_measured(self) -> bool:
        """``False`` means ``NOT_MEASURED`` on this host — not "used no memory"."""
        return self.peak_rss_bytes is not None

    @property
    def within_deadline(self) -> bool:
        return self.elapsed_seconds <= self.deadline_seconds

    @property
    def within_captured_budget(self) -> bool:
        return self.peak_captured_bytes <= self.max_captured_bytes

    @property
    def within_analysis_budget(self) -> bool:
        return self.peak_analysis_bytes <= self.max_analysis_bytes

    @property
    def within_staging_quota(self) -> bool:
        return self.staging_peak_bytes <= self.staging_quota_bytes

    @property
    def within_budget(self) -> bool:
        return (
            self.within_deadline
            and self.within_captured_budget
            and self.within_analysis_budget
            and self.within_staging_quota
        )

    def report(self) -> dict[str, object]:
        """The budget as evidence: explicit about what was *not* measured."""
        return {
            "deadline_seconds": self.deadline_seconds,
            "elapsed_seconds": round(self.elapsed_seconds, 4),
            "input_size_bytes": self.input_size_bytes,
            "input_duration_us": self.input_duration_us,
            "decoded_samples": self.decoded_samples,
            "output_size_bytes": self.output_size_bytes,
            "peak_captured_bytes": self.peak_captured_bytes,
            "peak_analysis_bytes": self.peak_analysis_bytes,
            "staging_peak_bytes": self.staging_peak_bytes,
            "staging_quota_bytes": self.staging_quota_bytes,
            "processes_started": self.processes_started,
            "threads": self.threads,
            "peak_rss_bytes": self.peak_rss_bytes,
            "rss": "MEASURED" if self.rss_measured else "NOT_MEASURED",
            "ffmpeg_version": self.ffmpeg_version,
            "within_budget": self.within_budget,
        }


class ProcessOutcome(BaseModel):
    """One bounded subprocess: how it ended, how long it took, what it wrote."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    argv: tuple[str, ...]
    returncode: int | None = None
    termination: Literal["exited", "timeout", "cancelled", "launch_failed"]
    elapsed_seconds: float = Field(ge=0.0)
    stdout_tail: str = ""
    stderr_tail: str = ""
    captured_bytes: int = Field(ge=0, default=0)
    #: The kernel's own peak RSS for the child (``VmHWM``), or ``None`` when the
    #: host does not expose it.  ``None`` is ``NOT_MEASURED`` — never zero.
    peak_rss_bytes: int | None = Field(default=None, ge=0)
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.termination == "exited" and self.returncode == 0


__all__ = [
    "CANONICAL_PROFILE",
    "CANONICAL_PROFILE_ID",
    "EBU_R128_BROADCAST",
    "HARD_TRUE_PEAK_CEILING_DB",
    "INTEGRATED_TOLERANCE_LU",
    "LOUDNESS_PROFILES",
    "MIN_PROGRAMME_SECONDS",
    "NAGAR_STREAMING_PRESET",
    "SCHEMA_VERSION",
    "SUPPORTED_SAMPLE_RATES",
    "TRUE_PEAK_TOLERANCE_DB",
    "CanonicalStreamProfile",
    "ContinuityLockSpec",
    "DecodeEvidence",
    "IdentityLockSpec",
    "LoudnessMeasurement",
    "LoudnessProfile",
    "LoudnessProfileId",
    "LoudnessTargetCheck",
    "MediaContractError",
    "MediaProbe",
    "ProcessOutcome",
    "ProcessingMode",
    "RenderOutcome",
    "ResourceBudget",
    "SpectralSignature",
    "canonical_hash",
    "canonical_json",
    "resolve_loudness_profile",
    "sha256_of_bytes",
]
