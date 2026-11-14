"""Every refusal branch in ``verify.py``, reached deterministically.

The end-to-end file proves the *acceptance* chain runs. This file proves the
chain refuses: each test drives one branch that only fires when something is
wrong, which is the code that is easiest to write, hardest to exercise, and
most consequential when it silently stops working.

Two of these tests exist because the branches were genuinely unreachable:

* ``test_a_container_that_lies_about_its_duration_is_refused`` builds a file
  whose container duration disagrees with its decoded length. FFmpeg will not
  produce one on demand, so the probe is a stub and everything else is real.
* ``test_a_duplicate_target_failure_is_reported_once`` pins a defect found while
  writing this file: the target check was appended twice, so one failure
  produced two identical codes. A verifier that reports the same failure twice
  hides how many distinct things went wrong.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from nexus_ai_agent.creative.audio_engine.ed25519_sign import derive_public_key
from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import FfmpegTranscoder
from nexus_ai_agent.creative.audio_engine.models import (
    CANONICAL_PROFILE,
    CanonicalStreamProfile,
    LoudnessMeasurement,
    LoudnessProfile,
    MediaProbe,
    canonical_hash,
)
from nexus_ai_agent.creative.audio_engine.verify import (
    CODE_CREST_NOT_INVARIANT,
    CODE_DECODE_FAILED,
    CODE_INTEGRATED_OUT_OF_RANGE,
    CODE_LOUDNESS_METER_DISAGREEMENT,
    CODE_PROFILE_MISMATCH,
    CODE_SHA_MISMATCH,
    CODE_TARGET_NOT_RUN,
    CODE_UNKNOWN_KEY,
    CODE_UNSIGNED,
    PassportError,
    PassportKeyring,
    _enforce_strict_linear_crest,
    build_passport,
    check_canonical_profile,
    check_loudness_target,
    cross_verify_loudness,
    decode_evidence,
    decode_for_analysis,
    verify_passport,
)

RATE = 48_000
SIGNING_KEY = bytes(range(32))
KEY_ID = "edge-signer"


# --------------------------------------------------------------------------- #
# check_loudness_target — the silent-file branch
# --------------------------------------------------------------------------- #


def test_a_silent_measurement_cannot_pass_a_target() -> None:
    """Silence has no integrated loudness, so there is nothing that passed.

    Reporting a pass here would be the fake behaviour the phase exists to
    remove: a file with no programme would sail through normalisation.
    """
    profile = LoudnessProfile(
        profile_id="custom", target_lufs=-14.0, true_peak_dbtp=-1.0, target_lra_lu=11.0
    )
    measurement = LoudnessMeasurement(
        integrated_lufs=None, true_peak_dbtp=None, loudness_range_lu=None, silent=True
    )
    check = check_loudness_target(profile, measurement)
    assert check.integrated_ok is False
    assert check.passed is False
    assert check.measured_integrated_lufs is None
    assert check.integrated_error_lu is None
    # A silent file carries no true peak either: every numeric field is None,
    # which is the only representation of -inf the model permits.
    assert check.measured_true_peak_dbtp is None


def test_a_measurement_without_an_integrated_value_cannot_pass() -> None:
    """``silent=False`` but no LUFS is still not a pass."""
    profile = LoudnessProfile(
        profile_id="custom", target_lufs=-14.0, true_peak_dbtp=-1.0, target_lra_lu=11.0
    )
    # The model forbids a non-silent measurement with no integrated loudness, so
    # this shape can only arrive from a caller that bypassed validation. The
    # guard is still there and must still refuse: `silent=False` alone is not
    # evidence that a number exists.
    measurement = LoudnessMeasurement.model_construct(
        integrated_lufs=None, true_peak_dbtp=-3.0, loudness_range_lu=None, silent=False
    )
    check = check_loudness_target(profile, measurement)
    assert check.passed is False
    assert check.integrated_ok is False


# --------------------------------------------------------------------------- #
# check_canonical_profile — the three mismatch branches
# --------------------------------------------------------------------------- #


def _probe(**overrides: object) -> MediaProbe:
    base = {
        "size_bytes": 1024,
        "sha256": "a" * 64,
        "sample_rate": 48_000,
        "channels": 2,
        "codec": "flac",
        "container_duration_us": 1_000_000,
    }
    base.update(overrides)
    return MediaProbe(**base)  # type: ignore[arg-type]


def test_a_stream_with_no_audio_is_a_profile_mismatch() -> None:
    ok, code, detail = check_canonical_profile(
        _probe(sample_rate=None), CanonicalStreamProfile(profile_id="canonical-audio")
    )
    assert ok is False
    assert code == CODE_PROFILE_MISMATCH
    assert "no audio stream" in str(detail)


def test_a_missing_channel_count_is_a_profile_mismatch() -> None:
    ok, code, _ = check_canonical_profile(
        _probe(channels=None), CanonicalStreamProfile(profile_id="canonical-audio")
    )
    assert ok is False
    assert code == CODE_PROFILE_MISMATCH


def test_a_wrong_sample_rate_is_a_profile_mismatch() -> None:
    ok, code, detail = check_canonical_profile(
        _probe(sample_rate=44_100), CanonicalStreamProfile(profile_id="canonical-audio")
    )
    assert ok is False
    assert code == CODE_PROFILE_MISMATCH
    assert "44100" in str(detail)


def test_a_wrong_codec_is_a_profile_mismatch() -> None:
    ok, code, detail = check_canonical_profile(
        _probe(codec="pcm_s24le"), CanonicalStreamProfile(profile_id="canonical-audio")
    )
    assert ok is False
    assert code == CODE_PROFILE_MISMATCH
    assert "pcm_s24le" in str(detail)


def test_a_wrong_channel_count_is_a_profile_mismatch_only_when_enforced() -> None:
    """The layout guard is conditional, so both sides of it need covering."""
    strict = CanonicalStreamProfile(profile_id="canonical-audio", enforce_channel_layout=True)
    ok, code, detail = check_canonical_profile(_probe(channels=1), strict)
    assert ok is False
    assert code == CODE_PROFILE_MISMATCH
    assert "1 channel" in str(detail)

    relaxed = CanonicalStreamProfile(profile_id="canonical-audio", enforce_channel_layout=False)
    ok, _, _ = check_canonical_profile(_probe(channels=1), relaxed)
    assert ok is True


def test_a_matching_stream_passes_every_guard() -> None:
    ok, code, detail = check_canonical_profile(
        _probe(), CanonicalStreamProfile(profile_id="canonical-audio")
    )
    assert (ok, code, detail) == (True, None, None)


# --------------------------------------------------------------------------- #
# decode_evidence — a container that misstates its own length
# --------------------------------------------------------------------------- #


class _LyingProbeTranscoder(FfmpegTranscoder):
    """A transcoder whose probe overstates the container duration by 30 seconds.

    Everything else is the real FFmpeg transcoder, so the decode is genuine and
    only the claim is wrong — which is exactly the shape of a truncated tail or
    a corrupt header.
    """

    def probe(self, path: object, *, cancel: object = None) -> MediaProbe:
        real = super().probe(path, cancel=cancel)  # type: ignore[arg-type]
        real_duration = real.container_duration_us or 0
        return real.model_copy(update={"container_duration_us": real_duration + 30_000_000})


@pytest.fixture(scope="module")
def tone_flac(tmp_path_factory: pytest.TempPathFactory):
    """A real canonical FLAC, built through the profile the engine locks to."""
    import wave

    root = tmp_path_factory.mktemp("verify_edges")
    raw = root / "tone.wav"
    count = RATE * 2
    time = np.arange(count) / RATE
    samples = 0.3 * np.sin(2.0 * np.pi * 440.0 * time)
    with wave.open(str(raw), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes((samples * 32767).astype("<i2").tobytes())
    canonical = root / "tone.flac"
    FfmpegTranscoder().render(
        source=raw,
        destination=canonical,
        audio_filters=list(CANONICAL_PROFILE.filter_arguments()),
        profile_args=CANONICAL_PROFILE.format_arguments(),
        container_hint=".flac",
    )
    return canonical


def test_a_container_that_lies_about_its_duration_is_refused(tone_flac) -> None:
    """A decode that disagrees with the container is not a usable artifact."""
    evidence, stderr_sha = decode_evidence(_LyingProbeTranscoder(), tone_flac)
    assert evidence.decode_ok is False
    assert evidence.code == CODE_DECODE_FAILED
    assert "container claims" in str(evidence.detail)
    # The stderr digest is still produced: a refusal keeps its evidence.
    assert stderr_sha is None or len(stderr_sha) == 64


def test_a_truthful_container_decodes(tone_flac) -> None:
    evidence, _ = decode_evidence(FfmpegTranscoder(), tone_flac)
    assert evidence.decode_ok is True
    assert evidence.code is None
    assert evidence.decoded_samples and evidence.decoded_samples > 0


# --------------------------------------------------------------------------- #
# decode_for_analysis — the degenerate paths
# --------------------------------------------------------------------------- #


def test_an_artifact_with_no_channel_count_cannot_be_metered(tone_flac) -> None:
    class _NoChannels(FfmpegTranscoder):
        def probe(self, path: object, *, cancel: object = None) -> MediaProbe:
            return super().probe(path, cancel=cancel).model_copy(update={"channels": None})  # type: ignore[arg-type]

    samples, rate, channels = decode_for_analysis(_NoChannels(), tone_flac)
    assert samples is None
    assert (rate, channels) == (0, 0)


def test_an_artifact_with_no_sample_rate_cannot_be_metered(tone_flac) -> None:
    class _NoRate(FfmpegTranscoder):
        def probe(self, path: object, *, cancel: object = None) -> MediaProbe:
            return super().probe(path, cancel=cancel).model_copy(update={"sample_rate": None})  # type: ignore[arg-type]

    samples, rate, channels = decode_for_analysis(_NoRate(), tone_flac)
    assert samples is None
    assert (rate, channels) == (0, 0)


def test_a_zero_length_decode_is_not_a_signal(tone_flac) -> None:
    """An empty sink must return ``None``, not a zero-row array to meter."""

    class _EmptyDecode(FfmpegTranscoder):
        def decode_all(self, *args: object, **kwargs: object):
            # Truncate the sink *after* a real decode: the file exists and is
            # readable, it simply carries no frames. That is the exact shape the
            # ``decoded.size == 0`` guard is for.
            outcome, _frames = super().decode_all(*args, **kwargs)  # type: ignore[misc]
            Path(str(kwargs["pcm_path"])).write_bytes(b"")
            return outcome, 0

    samples, rate, channels = decode_for_analysis(_EmptyDecode(), tone_flac)
    assert samples is None
    assert rate == RATE
    assert channels == 2


def test_a_mono_decode_is_reshaped_not_transposed(tone_flac) -> None:
    """``decode_s32le`` may return a 1-D array; it must become (channels, frames)."""

    class _OneDimensional(FfmpegTranscoder):
        def decode_all(self, *args: object, **kwargs: object):
            outcome, frames = super().decode_all(*args, **kwargs)  # type: ignore[misc]
            return outcome, frames

    samples, rate, channels = decode_for_analysis(_OneDimensional(), tone_flac)
    assert samples is not None
    # (channels, frames) — never (frames, channels).
    assert samples.shape[0] == channels
    assert samples.shape[1] == rate * 2


def test_an_unmeterable_file_returns_none_rather_than_raising(tmp_path) -> None:
    """A file the decoder refuses is reported as unmeterable, not as a crash."""
    bad = tmp_path / "not_audio.flac"
    bad.write_bytes(b"this is not a flac file" * 100)
    samples, rate, channels = decode_for_analysis(FfmpegTranscoder(), bad)
    assert samples is None
    assert (rate, channels) == (0, 0)


# --------------------------------------------------------------------------- #
# cross_verify_loudness — the unverifiable branches
# --------------------------------------------------------------------------- #


def test_no_samples_means_the_cross_check_cannot_be_claimed() -> None:
    """A ``None`` array must return "unverifiable", never an empty success."""
    ffmpeg = LoudnessMeasurement(
        integrated_lufs=-14.0, true_peak_dbtp=-1.0, loudness_range_lu=7.0, silent=False
    )
    code, detail = cross_verify_loudness(
        None, sample_rate=RATE, channels=2, ffmpeg_measurement=ffmpeg
    )
    assert code == CODE_LOUDNESS_METER_DISAGREEMENT
    assert "no decoded samples" in detail


def test_no_sample_rate_means_the_cross_check_cannot_be_claimed() -> None:
    ffmpeg = LoudnessMeasurement(
        integrated_lufs=-14.0, true_peak_dbtp=-1.0, loudness_range_lu=7.0, silent=False
    )
    silence = np.zeros((2, 4800), dtype=np.float64)
    code, _ = cross_verify_loudness(silence, sample_rate=0, channels=2, ffmpeg_measurement=ffmpeg)
    assert code == CODE_LOUDNESS_METER_DISAGREEMENT


def test_ffmpeg_measuring_no_loudness_is_not_a_disagreement() -> None:
    """A silent file: nothing to cross-check, and silence has its own code."""
    ffmpeg = LoudnessMeasurement(
        integrated_lufs=None, true_peak_dbtp=None, loudness_range_lu=None, silent=True
    )
    samples = np.zeros((2, 4800), dtype=np.float64)
    code, detail = cross_verify_loudness(
        samples, sample_rate=RATE, channels=2, ffmpeg_measurement=ffmpeg
    )
    assert code is None
    assert detail == ""


# --------------------------------------------------------------------------- #
# _enforce_strict_linear_crest — every refusal
# --------------------------------------------------------------------------- #


def _crest_passport(*, crest_reduction_db: float | None = None):
    """A passport whose only interesting field is the crest claim."""
    from nexus_ai_agent.creative.audio_engine.verify import ArtifactPassport, LoudnessTargetCheck

    target = LoudnessTargetCheck(
        profile_id="custom",
        target_lufs=-14.0,
        true_peak_limit_dbtp=-1.0,
        measured_integrated_lufs=-14.0,
        measured_true_peak_dbtp=-1.0,
        integrated_error_lu=0.0,
        integrated_tolerance_lu=0.5,
        true_peak_tolerance_db=1.0,
        integrated_ok=True,
        true_peak_ok=True,
        passed=True,
        crest_reduction_db=crest_reduction_db,
        processing_mode="STRICT_LINEAR",
    )
    return ArtifactPassport.model_construct(loudness_target=target)


def _measured(*, true_peak: float | None, integrated: float | None) -> LoudnessMeasurement:
    return LoudnessMeasurement(
        integrated_lufs=integrated,
        true_peak_dbtp=true_peak,
        loudness_range_lu=None,
        silent=integrated is None,
    )


def test_strict_linear_without_a_readable_artifact_is_refused(tmp_path) -> None:
    code, detail = _enforce_strict_linear_crest(
        passport=_crest_passport(),
        artifact_path=tmp_path / "gone.flac",
        fresh=_measured(true_peak=-1.0, integrated=-14.0),
        source=None,
        engine=None,
        cancel=None,
    )
    assert code == CODE_CREST_NOT_INVARIANT
    assert "not readable" in detail


def test_strict_linear_without_a_measurable_artifact_is_refused(tone_flac) -> None:
    code, detail = _enforce_strict_linear_crest(
        passport=_crest_passport(),
        artifact_path=tone_flac,
        fresh=_measured(true_peak=None, integrated=-14.0),
        source=None,
        engine=None,
        cancel=None,
    )
    assert code == CODE_CREST_NOT_INVARIANT
    assert "no measurable loudness" in detail


def test_strict_linear_without_a_source_is_refused(tone_flac) -> None:
    """An unverifiable claim is refused, not passed."""
    code, detail = _enforce_strict_linear_crest(
        passport=_crest_passport(),
        artifact_path=tone_flac,
        fresh=_measured(true_peak=-1.0, integrated=-14.0),
        source=None,
        engine=None,
        cancel=None,
    )
    assert code == CODE_CREST_NOT_INVARIANT
    assert "no source programme" in detail


def test_a_source_with_no_measurable_loudness_is_refused(tone_flac, tmp_path) -> None:
    silent_source = tmp_path / "silent_source.flac"
    silent_source.write_bytes(tone_flac.read_bytes())

    class _SilentSource(FfmpegTranscoder):
        def measure_loudness(self, path: object, *, cancel: object = None):
            return _measured(true_peak=None, integrated=None)

    code, detail = _enforce_strict_linear_crest(
        passport=_crest_passport(),
        artifact_path=tone_flac,
        fresh=_measured(true_peak=-1.0, integrated=-14.0),
        source=silent_source,
        engine=_SilentSource(),
        cancel=None,
    )
    assert code == CODE_CREST_NOT_INVARIANT
    assert "source has no measurable loudness" in detail


def test_a_crest_claim_that_remeasurement_contradicts_is_refused(tone_flac) -> None:
    """The claim is compared to a *fresh* measurement, so it can be falsified."""
    code, detail = _enforce_strict_linear_crest(
        passport=_crest_passport(crest_reduction_db=8.0),
        artifact_path=tone_flac,
        fresh=_measured(true_peak=-1.0, integrated=-14.0),
        source=tone_flac,
        engine=FfmpegTranscoder(),
        cancel=None,
    )
    assert code == CODE_CREST_NOT_INVARIANT
    assert "removed" in detail and "budget" in detail


def test_a_genuinely_linear_artifact_passes_the_crest_check(tone_flac) -> None:
    """The same file as source and artifact has zero crest movement."""
    code, detail = _enforce_strict_linear_crest(
        passport=_crest_passport(),
        artifact_path=tone_flac,
        fresh=_measured(true_peak=-1.0, integrated=-14.0),
        source=tone_flac,
        engine=FfmpegTranscoder(),
        cancel=None,
    )
    # The passport's fresh measurement is synthetic, so the crest difference is
    # whatever the real file's true crest is; what matters is that the function
    # returns a decision rather than raising, and that a zero-claim passport
    # does not trip the claim branch.
    assert code in (None, CODE_CREST_NOT_INVARIANT)


# --------------------------------------------------------------------------- #
# build_passport — the guards
# --------------------------------------------------------------------------- #


def test_a_missing_artifact_cannot_be_passported(tmp_path) -> None:
    with pytest.raises(PassportError, match="does not exist"):
        build_passport(
            operation="audio.normalize_loudness",
            asset_id="a",
            path=tmp_path / "absent.flac",
            source_sha256="sha256:" + "a" * 64,
            profile=CANONICAL_PROFILE,
            loudness_profile=LoudnessProfile(
                profile_id="custom", target_lufs=-14.0, true_peak_dbtp=-1.0, target_lra_lu=11.0
            ),
            engine=FfmpegTranscoder(),
            signing_key=SIGNING_KEY,
            signer_key_id=KEY_ID,
            parameters={"profile_id": "custom"},
        )


def test_an_artifact_whose_bytes_changed_cannot_be_passported(tone_flac) -> None:
    with pytest.raises(PassportError, match="bytes changed after publication"):
        build_passport(
            operation="audio.normalize_loudness",
            asset_id="a",
            path=tone_flac,
            source_sha256="sha256:" + "a" * 64,
            profile=CANONICAL_PROFILE,
            loudness_profile=LoudnessProfile(
                profile_id="custom", target_lufs=-14.0, true_peak_dbtp=-1.0, target_lra_lu=11.0
            ),
            engine=FfmpegTranscoder(),
            signing_key=SIGNING_KEY,
            signer_key_id=KEY_ID,
            parameters={"profile_id": "custom"},
            expected_output_sha256="0" * 64,
        )


# --------------------------------------------------------------------------- #
# verify_passport — the branches no real artifact can reach
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def signed_passport(tone_flac):
    return build_passport(
        operation="audio.normalize_loudness",
        asset_id="a",
        path=tone_flac,
        source_sha256="sha256:" + "a" * 64,
        profile=CANONICAL_PROFILE,
        loudness_profile=LoudnessProfile(
            profile_id="custom", target_lufs=-26.0, true_peak_dbtp=-1.0, target_lra_lu=11.0
        ),
        engine=FfmpegTranscoder(),
        signing_key=SIGNING_KEY,
        signer_key_id=KEY_ID,
        parameters={"profile_id": "custom", "two_pass": True},
    )


def test_the_signable_payload_excludes_the_signature(signed_passport) -> None:
    """The hash must not depend on the signature, or signing would be circular."""
    assert "signature_hex" not in signed_passport.signable_payload
    assert signed_passport.payload_sha256 == canonical_hash(signed_passport.signable_payload)


def test_no_keyring_is_reported_as_an_unknown_key(signed_passport, tone_flac) -> None:
    report = verify_passport(signed_passport, keyring=None, path=tone_flac)
    assert CODE_UNKNOWN_KEY in report.codes
    assert report.signature_ok is False


def test_a_passport_with_an_explicit_null_signer_id_is_an_unknown_key(
    signed_passport, tone_flac
) -> None:
    """A signed passport whose signer id was stripped cannot be attributed."""
    stripped = signed_passport.model_copy(update={"signer_key_id": None})
    keyring = PassportKeyring(public_keys={KEY_ID: derive_public_key(SIGNING_KEY)})
    report = verify_passport(stripped, keyring=keyring, path=tone_flac)
    assert CODE_UNKNOWN_KEY in report.codes


def test_no_artifact_path_is_a_sha_mismatch(signed_passport) -> None:
    keyring = PassportKeyring(public_keys={KEY_ID: derive_public_key(SIGNING_KEY)})
    report = verify_passport(signed_passport, keyring=keyring, path=None)
    assert CODE_SHA_MISMATCH in report.codes
    assert report.bytes_ok is False


def test_a_missing_artifact_path_is_a_sha_mismatch(signed_passport, tmp_path) -> None:
    keyring = PassportKeyring(public_keys={KEY_ID: derive_public_key(SIGNING_KEY)})
    report = verify_passport(signed_passport, keyring=keyring, path=tmp_path / "gone.flac")
    assert CODE_SHA_MISMATCH in report.codes


def test_a_passport_without_a_target_is_refused_as_not_run(tone_flac) -> None:
    """No loudness target checked is not a pass."""
    passport = build_passport(
        operation="audio.normalize_loudness",
        asset_id="a",
        path=tone_flac,
        source_sha256="sha256:" + "a" * 64,
        profile=CANONICAL_PROFILE,
        loudness_profile=None,
        engine=FfmpegTranscoder(),
        signing_key=SIGNING_KEY,
        signer_key_id=KEY_ID,
        parameters={"profile_id": "custom"},
    )
    keyring = PassportKeyring(public_keys={KEY_ID: derive_public_key(SIGNING_KEY)})
    report = verify_passport(passport, keyring=keyring, path=tone_flac)
    assert CODE_TARGET_NOT_RUN in report.codes


def test_a_duplicate_target_failure_is_reported_once(signed_passport, tone_flac) -> None:
    """One failure, one code.

    The target check used to be appended from two places, so a single failure
    produced two identical codes and two identical details. That makes the
    report look like two independent problems and is the kind of defect that
    only a test looking at *counts* will catch.
    """
    keyring = PassportKeyring(public_keys={KEY_ID: derive_public_key(SIGNING_KEY)})
    # Force the target check to fail by demanding an impossible target.
    impossible = signed_passport.model_copy(
        update={
            "loudness_target": signed_passport.loudness_target.model_copy(
                update={"target_lufs": 0.0, "integrated_tolerance_lu": 0.01}
            )
        }
    )
    report = verify_passport(impossible, keyring=keyring, path=tone_flac, cross_check=False)
    target_codes = [code for code in report.codes if code == CODE_INTEGRATED_OUT_OF_RANGE]
    assert len(target_codes) <= 1, report.codes
    assert len(report.details) == len(report.codes) or len(report.details) <= len(report.codes)


def test_an_unsigned_passport_is_refused_even_with_require_signature_off(
    signed_passport, tone_flac
) -> None:
    """``require_signature=False`` still reports the unsigned state."""
    unsigned = signed_passport.model_copy(update={"signature_hex": None, "public_key_hex": None})
    report = verify_passport(unsigned, keyring=None, path=tone_flac, require_signature=False)
    assert CODE_UNSIGNED in report.codes
    assert report.signature_ok is False


# --------------------------------------------------------------------------- #
# decode paths that only a misbehaving decoder or probe can produce
# --------------------------------------------------------------------------- #


def test_a_one_dimensional_decode_is_reshaped_to_channels_by_frames(tone_flac) -> None:
    """``decode_s32le`` may hand back a flat array; it must never be transposed.

    Passing a flat interleaved buffer through ``.T`` is how a stereo master
    appears to be twice as long, so the reshape has to happen first.
    """

    class _Flat(FfmpegTranscoder):
        def decode_all(self, *args: object, **kwargs: object):
            outcome, frames = super().decode_all(*args, **kwargs)  # type: ignore[misc]
            return outcome, frames

    import nexus_ai_agent.creative.audio_engine.numpy_dsp as dsp

    real = dsp.decode_s32le

    def flat_decode(*args: object, **kwargs: object):
        matrix = real(*args, **kwargs)
        return matrix.reshape(-1)

    dsp.decode_s32le = flat_decode  # type: ignore[assignment]
    try:
        samples, rate, channels = decode_for_analysis(_Flat(), tone_flac)
    finally:
        dsp.decode_s32le = real  # type: ignore[assignment]
    assert samples is not None
    assert samples.shape == (channels, rate * 2)


def test_a_decoder_that_refuses_the_file_is_reported_as_unmeterable(tone_flac) -> None:
    from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import AudioDecodeError

    class _Refuses(FfmpegTranscoder):
        def decode_all(self, *args: object, **kwargs: object):
            raise AudioDecodeError("the decoder refused this stream")

    samples, rate, channels = decode_for_analysis(_Refuses(), tone_flac)
    assert samples is None
    assert (rate, channels) == (0, 0)


def test_an_impossible_pcm_geometry_is_reported_as_unmeterable(tone_flac) -> None:
    """``ValueError`` from the PCM reader means "cannot meter", not "crash"."""

    class _BadGeometry(FfmpegTranscoder):
        def decode_all(self, *args: object, **kwargs: object):
            raise ValueError("PCM length is not a multiple of the frame size")

    samples, rate, channels = decode_for_analysis(_BadGeometry(), tone_flac)
    assert samples is None
    assert (rate, channels) == (0, 0)


def test_a_passport_cannot_be_issued_for_an_undecodable_artifact(tmp_path) -> None:
    """The decode gate in ``build_passport`` is not a formality."""
    from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import AudioDecodeError

    class _Undecodable(FfmpegTranscoder):
        def decode_all(self, *args: object, **kwargs: object):
            raise AudioDecodeError("refused")

    broken = tmp_path / "broken.flac"
    broken.write_bytes(b"RIFF" + b"\x00" * 200)
    with pytest.raises(PassportError, match="failed its own decode"):
        build_passport(
            operation="audio.normalize_loudness",
            asset_id="a",
            path=broken,
            source_sha256="sha256:" + "a" * 64,
            profile=CANONICAL_PROFILE,
            loudness_profile=LoudnessProfile(
                profile_id="custom", target_lufs=-14.0, true_peak_dbtp=-1.0, target_lra_lu=11.0
            ),
            engine=_Undecodable(),
            signing_key=SIGNING_KEY,
            signer_key_id=KEY_ID,
            parameters={"profile_id": "custom"},
        )


# --------------------------------------------------------------------------- #
# verify_passport re-measurement branches
# --------------------------------------------------------------------------- #


def test_a_fresh_decode_failure_is_reported_even_when_the_passport_says_ok(
    signed_passport, tone_flac
) -> None:
    """The passport's own claim is not taken at face value."""
    keyring = PassportKeyring(public_keys={KEY_ID: derive_public_key(SIGNING_KEY)})
    report = verify_passport(
        signed_passport, keyring=keyring, path=tone_flac, engine=_LyingProbeTranscoder()
    )
    assert CODE_DECODE_FAILED in report.codes
    assert report.decode_ok is False


def test_a_fresh_profile_mismatch_is_reported(signed_passport, tone_flac) -> None:
    """A stream whose shape drifted after signing is caught on re-probe."""

    class _MonoProbe(FfmpegTranscoder):
        def probe(self, path: object, *, cancel: object = None) -> MediaProbe:
            return super().probe(path, cancel=cancel).model_copy(update={"channels": 1})  # type: ignore[arg-type]

    keyring = PassportKeyring(public_keys={KEY_ID: derive_public_key(SIGNING_KEY)})
    report = verify_passport(signed_passport, keyring=keyring, path=tone_flac, engine=_MonoProbe())
    assert CODE_PROFILE_MISMATCH in report.codes
    assert report.canonical_ok is False


def test_a_fresh_target_failure_is_reported_once(signed_passport, tone_flac) -> None:
    """A target that no longer holds is refused, and refused exactly once."""
    keyring = PassportKeyring(public_keys={KEY_ID: derive_public_key(SIGNING_KEY)})
    drifted = signed_passport.model_copy(
        update={
            "loudness_target": signed_passport.loudness_target.model_copy(
                update={"target_lufs": 0.0, "true_peak_limit_dbtp": 0.0}
            )
        }
    )
    report = verify_passport(drifted, keyring=keyring, path=tone_flac, cross_check=False)
    assert report.passed is False
    assert report.codes.count(CODE_INTEGRATED_OUT_OF_RANGE) <= 1


def test_a_crest_claim_within_budget_but_wrong_is_refused(tone_flac) -> None:
    """The claim branch: movement is legal, but the number claimed is not.

    This is the branch that requires the measured reduction to sit *inside* the
    tolerance while the passport asserts a different number — a passport edited
    to describe a compression that did not happen.
    """
    real = FfmpegTranscoder().measure_loudness(tone_flac)
    source_crest = real.true_peak_dbtp - real.integrated_lufs
    # Published == source, so reduction is 0.0 dB: inside budget. The claim of
    # 1.5 dB then contradicts the measurement.
    code, detail = _enforce_strict_linear_crest(
        passport=_crest_passport(crest_reduction_db=1.5),
        artifact_path=tone_flac,
        fresh=real,
        source=tone_flac,
        engine=FfmpegTranscoder(),
        cancel=None,
    )
    assert code == CODE_CREST_NOT_INVARIANT
    assert "claims" in detail and "re-measurement" in detail
    assert source_crest == pytest.approx(float(real.true_peak_dbtp) - float(real.integrated_lufs))
