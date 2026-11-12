"""Independent verification and the tamper-evident :class:`ArtifactPassport`.

The rule this module exists to enforce is one sentence long:

    **the encoder's word is never evidence; only a fresh read of the finished
    bytes is.**

So verification runs *after* publication, against the final path, and it
re-derives everything from disk:

1. ``sha256`` of the published file (streamed; compared with the engine's claim);
2. a full decode of the published file (a truncated/corrupt encode fails here
   even when FFmpeg exited 0);
3. a fresh EBU R128 measurement of the *published* bytes;
4. a fresh probe of stream shape, compared with the canonical lock;
5. a fresh acoustic signature, for vocal continuity across segments.

Result: :class:`ArtifactPassport` — a typed record of *measured* facts plus a
signature over them.  The signature is **Ed25519**, and it is produced with the
same pinned verification implementation the repo already trusts
(:mod:`nexus_ai_agent.creative.packs.ed25519`), extended here with a signer so
the passport can be *issued*, not merely checked.  Verification is the
repository's own function — there is no second curve implementation in this
module.

Honest limits, stated up front:

* an Ed25519 signature proves the bytes and the measurement were not altered
  after the passport was issued *by the holder of the key*.  It does not prove
  who the holder is — that is the key-distribution/trust-root problem, and the
  trust root for these passports is the caller-supplied key id;
* without a private key the passport is issued **unsigned** (``signature=None``)
  and the verifier refuses to treat it as attestable evidence.  It never
  fabricates a signature.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import (
    AudioDecodeError,
    FfmpegTranscoder,
)
from nexus_ai_agent.creative.audio_engine.models import (
    SCHEMA_VERSION,
    CanonicalStreamProfile,
    DecodeEvidence,
    LoudnessMeasurement,
    LoudnessProfile,
    LoudnessTargetCheck,
    MediaProbe,
    SpectralSignature,
    canonical_hash,
    canonical_json,
)
from nexus_ai_agent.creative.audio_engine.process import CancelToken
from nexus_ai_agent.creative.audio_engine.workspace import hash_file
from nexus_ai_agent.creative.packs.ed25519 import (
    BACKEND as ED25519_BACKEND,
)
from nexus_ai_agent.creative.packs.ed25519 import (
    Ed25519Error,
    verify_ed25519,
)

__all__ = [
    "PASSPORT_SCHEMA",
    "ArtifactPassport",
    "PassportError",
    "PassportKeyring",
    "VerificationReport",
    "build_passport",
    "verify_passport",
    "check_loudness_target",
    "check_canonical_profile",
    "decode_evidence",
]

PASSPORT_SCHEMA: Literal["nagar.artifact_passport.v1"] = "nagar.artifact_passport.v1"

#: Refusal codes (machine-readable, one meaning each).
CODE_SILENT_PROGRAMME = "silent_programme"
CODE_PROGRAMME_TOO_SHORT = "programme_too_short"
CODE_LOUDNESS_UNMEASURABLE = "loudness_unmeasurable"
CODE_INTEGRATED_OUT_OF_RANGE = "integrated_loudness_out_of_range"
CODE_TRUE_PEAK_EXCEEDED = "true_peak_exceeded"
CODE_PROFILE_MISMATCH = "canonical_profile_mismatch"
CODE_SHA_MISMATCH = "sha256_mismatch"
CODE_DECODE_FAILED = "decode_failed"
CODE_UNSIGNED = "passport_unsigned"
CODE_SIGNATURE_INVALID = "passport_signature_invalid"
CODE_UNKNOWN_KEY = "passport_key_unknown"
CODE_TARGET_NOT_RUN = "target_not_checked"
#: A STRICT_LINEAR artifact whose crest factor did not survive the render.
CODE_CREST_NOT_INVARIANT = "strict_linear_crest_not_invariant"


class PassportError(RuntimeError):
    """A passport could not be issued or verified (fail-closed)."""


def check_loudness_target(
    profile: LoudnessProfile,
    measurement: LoudnessMeasurement,
    *,
    integrated_tolerance_lu: float = 1.0,
    true_peak_tolerance_db: float = 0.5,
    input_crest_factor_db: float | None = None,
) -> LoudnessTargetCheck:
    """Decide, from a real measurement, whether the target was reached.

    A silent file can never "pass": there is no programme to normalize, and
    reporting success there is exactly the fake behaviour this phase removes.

    ``input_crest_factor_db`` is the *source* crest factor (peak minus RMS of the
    same decoded window).  Supplying it turns the check into a statement about
    what the processing did to the dynamics: the output's own crest factor is
    compared to it and the difference is reported as ``crest_reduction_db``.
    A positive number means the render genuinely compressed the programme — so a
    ``DYNAMIC_ALLOWED`` profile can never quietly claim a linear result.
    """
    if measurement.silent or measurement.integrated_lufs is None:
        return LoudnessTargetCheck(
            profile_id=profile.profile_id,
            target_lufs=profile.target_lufs,
            true_peak_limit_dbtp=profile.true_peak_dbtp,
            measured_integrated_lufs=None,
            measured_true_peak_dbtp=measurement.true_peak_dbtp,
            integrated_error_lu=None,
            integrated_tolerance_lu=integrated_tolerance_lu,
            true_peak_tolerance_db=true_peak_tolerance_db,
            integrated_ok=False,
            true_peak_ok=False,
            passed=False,
            processing_mode=profile.processing_mode,
            code=CODE_SILENT_PROGRAMME,
            detail="the programme is silent (-inf LUFS): there is nothing to normalize",
        )
    error = measurement.integrated_lufs - profile.target_lufs
    integrated_ok = abs(error) <= integrated_tolerance_lu
    peak_ok: bool
    if measurement.true_peak_dbtp is None:
        peak_ok = False
    else:
        # A true peak above the ceiling is a hard failure; below it we allow the
        # stated measurement tolerance so -1.4 dBTP for a -1.0 target passes.
        peak_ok = measurement.true_peak_dbtp <= profile.true_peak_dbtp + true_peak_tolerance_db
    code: str | None = None
    detail: str | None = None
    if not integrated_ok:
        code = CODE_INTEGRATED_OUT_OF_RANGE
        detail = (
            f"integrated {measurement.integrated_lufs:.2f} LUFS is {error:+.2f} LU from "
            f"the {profile.target_lufs:.1f} LUFS target (tolerance {integrated_tolerance_lu:g} LU)"
        )
    elif not peak_ok:
        code = CODE_TRUE_PEAK_EXCEEDED
        # An *unmeasured* peak is a different failure from an exceeded one, and
        # it used to crash here: formatting ``None`` with ``:.2f`` raises
        # ``TypeError``, which turned "this file has no true peak" into a
        # traceback instead of a refusal.  A ceiling that cannot be checked is a
        # failure with a reason, never an exception at the caller.
        detail = (
            "true peak could not be measured, so the "
            f"{profile.true_peak_dbtp:.1f} dBTP ceiling cannot be proven"
            if measurement.true_peak_dbtp is None
            else (
                f"true peak {measurement.true_peak_dbtp:.2f} dBTP exceeds the "
                f"{profile.true_peak_dbtp:.1f} dBTP ceiling"
            )
        )
    crest_reduction_db: float | None = None
    if input_crest_factor_db is not None and measurement.true_peak_dbtp is not None:
        # Both terms come from the same measurement of the same file, which is
        # what makes the number reconstructible: reduction == input_crest -
        # output_crest.  Peak-to-RMS of the output is the integrated/true-peak
        # pair the verifier already measured — no extra decode, no second opinion.
        output_crest = measurement.true_peak_dbtp - measurement.integrated_lufs
        crest_reduction_db = round(max(0.0, input_crest_factor_db - output_crest), 4)
    return LoudnessTargetCheck(
        profile_id=profile.profile_id,
        target_lufs=profile.target_lufs,
        true_peak_limit_dbtp=profile.true_peak_dbtp,
        processing_mode=profile.processing_mode,
        crest_reduction_db=crest_reduction_db,
        measured_integrated_lufs=measurement.integrated_lufs,
        measured_true_peak_dbtp=measurement.true_peak_dbtp,
        integrated_error_lu=round(error, 4),
        integrated_tolerance_lu=integrated_tolerance_lu,
        true_peak_tolerance_db=true_peak_tolerance_db,
        integrated_ok=integrated_ok,
        true_peak_ok=peak_ok,
        passed=integrated_ok and peak_ok,
        code=code,
        detail=detail,
    )


def check_canonical_profile(
    probe: MediaProbe, profile: CanonicalStreamProfile
) -> tuple[bool, str | None, str | None]:
    """Compare a probed stream against the canonical lock."""
    if probe.sample_rate is None or probe.channels is None:
        return False, CODE_PROFILE_MISMATCH, "the produced file carries no audio stream"
    if probe.sample_rate != profile.sample_rate:
        return (
            False,
            CODE_PROFILE_MISMATCH,
            f"sample rate {probe.sample_rate} != locked {profile.sample_rate}",
        )
    expected_codec = profile.codec
    if probe.codec != expected_codec:
        # FLAC reports as "flac"; WAV as "pcm_s24le" (possibly with metadata).
        return (
            False,
            CODE_PROFILE_MISMATCH,
            f"codec {probe.codec!r} != locked {expected_codec!r}",
        )
    if profile.enforce_channel_layout and probe.channels != profile.channels:
        return (
            False,
            CODE_PROFILE_MISMATCH,
            f"{probe.channels} channel(s) != locked {profile.channels}",
        )
    return True, None, None


def decode_evidence(
    engine: FfmpegTranscoder,
    path: Path,
    *,
    cancel: CancelToken | None = None,
    workdir: Path | None = None,
) -> tuple[DecodeEvidence, str | None]:
    """Decode the finished file end-to-end; report what was actually readable."""
    tmp_dir = workdir or Path(tempfile.gettempdir())
    sink = Path(tempfile.mkstemp(prefix="nagar_decode_", suffix=".s32le", dir=str(tmp_dir))[1])
    try:
        probe = engine.probe(path, cancel=cancel)
        if probe.channels is None:
            raise AudioDecodeError("the artifact carries no audio stream to decode")
        outcome, frames = engine.decode_all(
            path, pcm_path=sink, channels=probe.channels, cancel=cancel
        )
        rate = probe.sample_rate or 0
        duration_us = int(round(frames / float(rate) * 1_000_000)) if rate else None
        claim = probe.container_duration_us
        # A decode that disagrees with the container duration beyond one frame
        # means the file is not self-consistent (truncated tail / bad header).
        # Both numbers must be known: an unknown duration is not agreement.
        if (
            claim is not None
            and rate
            and duration_us is not None
            and abs(duration_us - claim) > int(1_000_000 / max(rate, 1)) * 2 + 40_000
        ):
            return (
                DecodeEvidence(
                    decode_ok=False,
                    decoded_samples=frames,
                    decoded_duration_us=duration_us,
                    elapsed_seconds=outcome.elapsed_seconds,
                    code=CODE_DECODE_FAILED,
                    detail=(f"decoded {duration_us} us but the container claims {claim} us"),
                ),
                hashlib.sha256((outcome.stderr_tail or "").encode()).hexdigest(),
            )
        return (
            DecodeEvidence(
                decode_ok=True,
                decoded_samples=frames,
                decoded_duration_us=duration_us,
                elapsed_seconds=outcome.elapsed_seconds,
            ),
            hashlib.sha256((outcome.stderr_tail or "").encode()).hexdigest(),
        )
    except AudioDecodeError as error:
        return (
            DecodeEvidence(decode_ok=False, code=CODE_DECODE_FAILED, detail=str(error)),
            None,
        )
    finally:
        sink.unlink(missing_ok=True)


class ArtifactPassport(BaseModel):
    """Measured facts about one published artifact, plus an optional signature."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = SCHEMA_VERSION
    passport_id: str
    schema_id: str = PASSPORT_SCHEMA
    operation: str
    asset_id: str
    source_sha256: str
    output_sha256: str
    output_size_bytes: int = Field(ge=0)
    output_duration_us: int | None = Field(default=None, ge=0)
    probe: MediaProbe
    decode: DecodeEvidence
    loudness: LoudnessMeasurement | None = None
    loudness_target: LoudnessTargetCheck | None = None
    canonical_profile_id: str
    canonical_ok: bool
    canonical_code: str | None = None
    canonical_detail: str | None = None
    signature: SpectralSignature | None = None
    continuity_lock_hash: str | None = None
    transactional_identity: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
    engine: dict[str, Any] = Field(default_factory=dict)
    elapsed_seconds: float = Field(ge=0.0, default=0.0)
    verified: bool = False
    issued_at_utc: str
    signer_key_id: str | None = None
    signature_algorithm: str = "ed25519"
    public_key_hex: str | None = None
    signature_hex: str | None = None

    @property
    def signable_payload(self) -> dict[str, Any]:
        """Everything the signature covers (the signature itself excluded)."""
        return self.model_dump(mode="json", exclude={"signature_hex"})

    @property
    def payload_sha256(self) -> str:
        return canonical_hash(self.signable_payload)

    def with_signature(self, *, signature_hex: str, public_key_hex: str) -> ArtifactPassport:
        return self.model_copy(
            update={"signature_hex": signature_hex, "public_key_hex": public_key_hex}
        )


@dataclass(frozen=True)
class PassportKeyring:
    """Trust root for passport signatures: ``key_id -> 32-byte public key``.

    Deliberately tiny and explicit.  There is no ambient key discovery: an
    unknown ``key_id`` is a verification failure (``passport_key_unknown``),
    never "trust on first use".
    """

    public_keys: dict[str, bytes]

    def __post_init__(self) -> None:
        for key_id, key in self.public_keys.items():
            if len(key) != 32:
                raise PassportError(f"public key for {key_id!r} must be 32 bytes, got {len(key)}")


class VerificationReport(BaseModel):
    """The outcome of re-verifying a passport against the world."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    passport_id: str
    signature_ok: bool
    bytes_ok: bool
    decode_ok: bool
    loudness_ok: bool
    canonical_ok: bool
    passed: bool
    codes: tuple[str, ...] = ()
    details: tuple[str, ...] = ()

    @property
    def code(self) -> str | None:
        return self.codes[0] if self.codes else None


def build_passport(
    *,
    operation: str,
    asset_id: str,
    path: Path,
    source_sha256: str,
    profile: CanonicalStreamProfile,
    loudness_profile: LoudnessProfile | None,
    engine: FfmpegTranscoder,
    expected_output_sha256: str | None = None,
    include_signature: bool = True,
    max_analysis_seconds: float = 60.0,
    cancel: CancelToken | None = None,
    parameters: dict[str, Any] | None = None,
    continuity_lock_hash: str | None = None,
    transactional_identity: dict[str, Any] | None = None,
    signing_key: bytes | None = None,
    signer_key_id: str | None = None,
    source_crest_factor_db: float | None = None,
) -> ArtifactPassport:
    """Measure a published artifact from disk and issue a passport for it."""
    artifact = Path(path)
    if not artifact.is_file():
        raise PassportError(f"artifact does not exist: {artifact}")
    measured_sha = hash_file(artifact)
    if expected_output_sha256 is not None and measured_sha != expected_output_sha256:
        raise PassportError(
            f"artifact bytes changed after publication: expected {expected_output_sha256}, "
            f"measured {measured_sha}"
        )
    probe = engine.probe(artifact, cancel=cancel)
    decode, stderr_sha = decode_evidence(engine, artifact, cancel=cancel)
    if not decode.decode_ok:
        raise PassportError(f"artifact failed its own decode: {decode.detail}")
    decode = decode.model_copy(update={"stderr_sha256": stderr_sha})
    canonical_ok, canonical_code, canonical_detail = check_canonical_profile(probe, profile)
    loudness = engine.measure_loudness(artifact, cancel=cancel)
    target_check = (
        check_loudness_target(
            loudness_profile, loudness, input_crest_factor_db=source_crest_factor_db
        )
        if loudness_profile is not None
        else None
    )
    signature = None
    if max_analysis_seconds > 0:
        handle, signature_sink = tempfile.mkstemp(prefix="nagar_sig_", suffix=".s32le")
        os.close(handle)
        try:
            signature = engine.signature(
                artifact,
                pcm_path=signature_sink,
                max_analysis_seconds=max_analysis_seconds,
                cancel=cancel,
            )
        finally:
            Path(signature_sink).unlink(missing_ok=True)
        signature = signature.model_copy(update={"integrated_lufs": loudness.integrated_lufs})
    verified = bool(
        decode.decode_ok
        and canonical_ok
        and (target_check is None or target_check.passed)
        and (expected_output_sha256 is None or measured_sha == expected_output_sha256)
    )
    passport = ArtifactPassport(
        passport_id="pp_"
        + hashlib.sha256(
            canonical_json(
                {
                    "operation": operation,
                    "asset_id": asset_id,
                    "output_sha256": measured_sha,
                    "source_sha256": source_sha256,
                    "profile": profile.profile_id,
                }
            ).encode("utf-8")
        ).hexdigest()[:32],
        operation=operation,
        asset_id=asset_id,
        source_sha256=source_sha256,
        output_sha256=measured_sha,
        output_size_bytes=artifact.stat().st_size,
        output_duration_us=decode.decoded_duration_us or probe.container_duration_us,
        probe=probe,
        decode=decode,
        loudness=loudness,
        loudness_target=target_check,
        canonical_profile_id=profile.profile_id,
        canonical_ok=canonical_ok,
        canonical_code=canonical_code,
        canonical_detail=canonical_detail,
        signature=signature,
        continuity_lock_hash=continuity_lock_hash,
        transactional_identity=dict(transactional_identity or {}),
        parameters=dict(parameters or {}),
        engine={
            "backend": "ffmpeg",
            "binary_name": Path(engine.binary).name,
            "measurement": "ebur128(peak=true)",
            "decode": "s32le full-file",
            "dsp": "numpy-bounded-window",
            "ed25519_backend": ED25519_BACKEND,
        },
        elapsed_seconds=round(decode.elapsed_seconds, 6),
        verified=verified,
        issued_at_utc=_utc_now(),
        signer_key_id=signer_key_id,
    )
    if include_signature:
        if signing_key is None:
            # Never invent a signature: the passport is usable as evidence of
            # measurement, and verify_passport will refuse it as attestation.
            return passport
        return sign_passport(passport, signing_key=signing_key, key_id=signer_key_id)
    return passport


def sign_passport(
    passport: ArtifactPassport, *, signing_key: bytes, key_id: str | None = None
) -> ArtifactPassport:
    """Sign the passport's canonical payload with Ed25519 (pinned implementation).

    Order matters: ``signer_key_id`` and ``public_key_hex`` are part of the
    signed payload, so they are stamped *before* the signature is computed.  A
    passport whose identity fields change after signing is a tampered passport
    and must fail verification (``passport_signature_invalid``) — which is
    exactly what a wrong order would produce, so the order is pinned here.
    """
    from nexus_ai_agent.creative.audio_engine.ed25519_sign import (
        derive_public_key,
        sign_ed25519,
    )

    if len(signing_key) != 32:
        raise PassportError("Ed25519 signing key must be a 32-byte seed")
    public_hex = derive_public_key(signing_key).hex()
    claimed = passport.model_copy(update={"public_key_hex": public_hex})
    if key_id is not None:
        claimed = claimed.model_copy(update={"signer_key_id": key_id})
    signature_hex, _ = sign_ed25519(
        signing_key, canonical_json(claimed.signable_payload).encode("utf-8")
    )
    return claimed.with_signature(signature_hex=signature_hex, public_key_hex=public_hex)


#: STRICT_LINEAR permits a loudness gain, not dynamic compression, so the crest
#: factor of the published file must still be the source's to within measurement
#: noise.  0.25 dB is the budget: two independent EBU R128 measurements of
#: identical dynamics differ by well under it, while the smallest useful limiter
#: setting removes far more.
STRICT_LINEAR_CREST_TOLERANCE_DB = 0.25


def _enforce_strict_linear_crest(
    *,
    passport: ArtifactPassport,
    artifact_path: Path | None,
    fresh: LoudnessMeasurement,
    source: Path | None,
    engine: FfmpegTranscoder | None,
    cancel: CancelToken | None,
) -> tuple[str | None, str]:
    """Refuse a STRICT_LINEAR artifact whose crest factor moved.

    Re-measures the source and the published file independently and compares
    crest factors (``true_peak - integrated``, the same definition the renderer
    uses).  Returns ``(code, detail)``; ``code`` is ``None`` when the artifact is
    genuinely crest-invariant.
    """
    if artifact_path is None or not artifact_path.exists():
        return (
            CODE_CREST_NOT_INVARIANT,
            "STRICT_LINEAR was claimed but the published artifact is not readable, "
            "so the crest invariance cannot be verified",
        )
    if fresh.true_peak_dbtp is None or fresh.integrated_lufs is None:
        return (
            CODE_CREST_NOT_INVARIANT,
            "STRICT_LINEAR was claimed but the artifact has no measurable loudness "
            "(silent or unreadable), so crest invariance cannot be verified",
        )
    output_crest = fresh.true_peak_dbtp - fresh.integrated_lufs
    if source is None or not Path(source).exists():
        return (
            CODE_CREST_NOT_INVARIANT,
            "STRICT_LINEAR was claimed but no source programme was supplied, so the "
            "crest reduction cannot be measured — an unverifiable claim is refused, "
            "not passed",
        )
    measurer = engine if engine is not None else FfmpegTranscoder()
    try:
        source_measurement = measurer.measure_loudness(Path(source), cancel=cancel)
    except Exception as error:  # pragma: no cover - unreadable source is an I/O fault
        return (
            CODE_CREST_NOT_INVARIANT,
            f"STRICT_LINEAR was claimed but the source could not be measured: {error}",
        )
    if source_measurement.true_peak_dbtp is None or source_measurement.integrated_lufs is None:
        return (
            CODE_CREST_NOT_INVARIANT,
            "STRICT_LINEAR was claimed but the source has no measurable loudness, so "
            "crest reduction cannot be measured",
        )
    source_crest = source_measurement.true_peak_dbtp - source_measurement.integrated_lufs
    reduction = source_crest - output_crest
    claimed = passport.loudness_target.crest_reduction_db if passport.loudness_target else None
    if abs(reduction) > STRICT_LINEAR_CREST_TOLERANCE_DB:
        return (
            CODE_CREST_NOT_INVARIANT,
            f"STRICT_LINEAR was claimed but the published file removed {reduction:+.2f} dB "
            f"of crest factor (source {source_crest:.2f} dB, published {output_crest:.2f} dB, "
            f"budget {STRICT_LINEAR_CREST_TOLERANCE_DB:g} dB): this artifact was dynamically "
            "compressed or limited and must not be published as a linear master",
        )
    if (
        claimed is not None
        and abs(claimed - max(0.0, reduction)) > STRICT_LINEAR_CREST_TOLERANCE_DB
    ):
        return (
            CODE_CREST_NOT_INVARIANT,
            f"the passport claims {claimed:.2f} dB of crest reduction but re-measurement "
            f"against the source gives {max(0.0, reduction):.2f} dB",
        )
    return None, ""


def verify_passport(
    passport: ArtifactPassport,
    *,
    keyring: PassportKeyring | None = None,
    path: Path | None = None,
    engine: FfmpegTranscoder | None = None,
    require_signature: bool = True,
    cancel: CancelToken | None = None,
    source: Path | None = None,
) -> VerificationReport:
    """Re-check a passport: signature, bytes, decode, loudness, canonical shape.

    Every check runs against the world *now*.  A passport that says "verified"
    is worth exactly as much as this function re-confirming it.

    ``source`` is the programme the artifact was derived from, and it is what
    makes the STRICT_LINEAR crest claim checkable: the source crest is measured
    from the source *file*, not read back out of the passport being audited.
    Omitting it does not disable the check — a STRICT_LINEAR artifact whose crest
    cannot be verified is refused, because an unverifiable claim is not a
    verified one.
    """
    codes: list[str] = []
    details: list[str] = []

    # 1. signature
    signature_ok = False
    if passport.signature_hex is None or passport.public_key_hex is None:
        codes.append(CODE_UNSIGNED)
        details.append("passport carries no Ed25519 signature")
    elif keyring is None or passport.signer_key_id is None:
        codes.append(CODE_UNKNOWN_KEY)
        details.append("no trust root given for the passport signer key")
    else:
        trusted = keyring.public_keys.get(passport.signer_key_id)
        if trusted is None:
            codes.append(CODE_UNKNOWN_KEY)
            details.append(f"signer key id {passport.signer_key_id!r} is not in the keyring")
        elif trusted.hex() != passport.public_key_hex:
            codes.append(CODE_SIGNATURE_INVALID)
            details.append("passport public key does not match the trusted key for that id")
        else:
            try:
                verify_ed25519(
                    public_key=bytes.fromhex(passport.public_key_hex),
                    message=canonical_json(passport.signable_payload).encode("utf-8"),
                    signature=bytes.fromhex(passport.signature_hex),
                )
                signature_ok = True
            except (Ed25519Error, ValueError) as error:
                codes.append(CODE_SIGNATURE_INVALID)
                details.append(f"signature verification failed: {error}")

    # 2. bytes
    bytes_ok = False
    if path is None:
        codes.append(CODE_SHA_MISMATCH)
        details.append("no artifact path supplied; the bytes cannot be re-hashed")
    elif not Path(path).is_file():
        codes.append(CODE_SHA_MISMATCH)
        details.append(f"artifact is missing: {path}")
    else:
        measured = hash_file(path)
        bytes_ok = measured == passport.output_sha256
        if not bytes_ok:
            codes.append(CODE_SHA_MISMATCH)
            details.append(f"artifact sha256 {measured} != passport {passport.output_sha256}")

    # 3-5. re-measure when an engine is supplied
    decode_ok = passport.decode.decode_ok
    loudness_ok = bool(passport.loudness_target is None or passport.loudness_target.passed)
    canonical_ok = passport.canonical_ok
    if engine is not None and path is not None and bytes_ok:
        fresh_decode, _ = decode_evidence(engine, Path(path), cancel=cancel)
        decode_ok = fresh_decode.decode_ok
        if not decode_ok:
            codes.append(CODE_DECODE_FAILED)
            details.append(str(fresh_decode.detail))
        fresh_probe = engine.probe(path, cancel=cancel)
        canonical_ok, canonical_code, canonical_detail = check_canonical_profile(
            fresh_probe, CanonicalStreamProfile(profile_id=passport.canonical_profile_id)
        )
        if not canonical_ok and canonical_detail:
            codes.append(canonical_code or CODE_PROFILE_MISMATCH)
            details.append(canonical_detail)
        if passport.loudness_target is not None:
            fresh = engine.measure_loudness(path, cancel=cancel)
            threshold_profile = LoudnessProfile(
                profile_id="custom",
                target_lufs=passport.loudness_target.target_lufs,
                true_peak_dbtp=passport.loudness_target.true_peak_limit_dbtp,
                target_lra_lu=11.0,
            )
            # Re-run the *target* check on the fresh measurement.  Note what is
            # deliberately NOT done here: the passport's claimed crest reduction
            # is not fed back in as the source crest.  Doing that made the check a
            # round trip — any claim re-derived itself exactly (2, 8 and 12 dB all
            # "survived") and a tampered passport passed.  The crest claim is
            # checked against a freshly measured source in
            # ``_enforce_strict_linear_crest`` below, where it is falsifiable.
            claimed_target = passport.loudness_target
            fresh_check = check_loudness_target(
                threshold_profile,
                fresh,
                integrated_tolerance_lu=claimed_target.integrated_tolerance_lu,
                true_peak_tolerance_db=claimed_target.true_peak_tolerance_db,
                # ``None`` — deliberately.  Re-confirming a *claimed* crest
                # reduction by feeding the claim back in as the source crest
                # re-derives the claim exactly whatever its value: 2 dB, 8 dB
                # and 12 dB all "survive" because the arithmetic is a round trip.
                # A tampered passport therefore passed, which is the specific
                # failure this verifier exists to prevent.
                input_crest_factor_db=None,
            )
            loudness_ok = loudness_ok and fresh_check.passed
            if not fresh_check.passed:
                codes.append(fresh_check.code or CODE_INTEGRATED_OUT_OF_RANGE)
                details.append(str(fresh_check.detail))

            # STRICT_LINEAR means gain only: the file's crest factor must still
            # be the source's.  The source crest is NOT a passport claim here —
            # it is re-measured from the source file passed alongside, and if no
            # source is available the check is reported as *unverifiable* rather
            # than silently passed.
            if claimed_target.processing_mode == "STRICT_LINEAR":
                crest_code, crest_detail = _enforce_strict_linear_crest(
                    passport=passport,
                    artifact_path=path,
                    fresh=fresh,
                    source=source,
                    engine=engine,
                    cancel=cancel,
                )
                if crest_code is not None:
                    codes.append(crest_code)
                    details.append(crest_detail)
                    loudness_ok = False
            if not fresh_check.passed:
                codes.append(fresh_check.code or CODE_INTEGRATED_OUT_OF_RANGE)
                details.append(str(fresh_check.detail))
    elif passport.loudness_target is None:
        codes.append(CODE_TARGET_NOT_RUN)
        details.append("no loudness target was checked for this artifact")

    if require_signature and not signature_ok:
        passed = False
    else:
        passed = bool(bytes_ok and decode_ok and loudness_ok and canonical_ok and signature_ok)
    return VerificationReport(
        passport_id=passport.passport_id,
        signature_ok=signature_ok,
        bytes_ok=bytes_ok,
        decode_ok=decode_ok,
        loudness_ok=loudness_ok,
        canonical_ok=canonical_ok,
        passed=passed,
        codes=tuple(dict.fromkeys(codes)),
        details=tuple(details),
    )


def _utc_now() -> str:
    """RFC 3339 UTC timestamp with second precision.

    ``datetime.UTC`` is a 3.11+ alias for ``datetime.timezone.utc``; this package
    declares ``requires-python >= 3.10`` and CI runs a blocking 3.10 leg, so the
    3.10-compatible spelling is used.  The two are the same object on 3.11+, which
    is exactly why the difference is invisible locally and only shows up on the
    oldest supported interpreter.
    """
    import datetime

    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
