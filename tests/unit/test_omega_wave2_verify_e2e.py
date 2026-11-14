"""End-to-end verification of a real artifact, including the STRICT_LINEAR path.

The unit tests around `_enforce_strict_linear_crest` and `cross_verify_loudness`
prove those functions behave. They do not prove the *verifier* calls them, and a
check that is never reached is not a check. These tests build a real passport
from a real file with the real FFmpeg transcoder, then tamper with it, so the
whole chain runs: signature -> bytes -> decode -> canonical shape -> loudness
target -> independent meter cross-check -> crest invariance.
"""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.audio_engine.ed25519_sign import derive_public_key
from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import FfmpegTranscoder
from nexus_ai_agent.creative.audio_engine.models import (
    CANONICAL_PROFILE,
    LoudnessProfile,
)
from nexus_ai_agent.creative.audio_engine.verify import (
    CODE_CREST_NOT_INVARIANT,
    CODE_LOUDNESS_METER_DISAGREEMENT,
    PassportKeyring,
    build_passport,
    verify_passport,
)

RATE = 48_000
SIGNING_KEY = bytes(range(32))
KEY_ID = "test-signer"


def _write_programme(path: object) -> np.ndarray:
    """A 25 dB-crest programme: quiet body with sharp transients."""
    import wave

    count = RATE * 6
    rng = np.random.default_rng(5)
    body = 0.06 * rng.standard_normal(count)
    for when in np.arange(0.5, 6.0, 0.5):
        start = int(when * RATE)
        length = int(0.01 * RATE)
        body[start : start + length] += (
            0.9 * np.exp(-np.linspace(0.0, 8.0, length)) * rng.standard_normal(length)
        )
    body = np.clip(body, -1.0, 1.0)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes((body * 32767).astype("<i2").tobytes())
    return body


@pytest.fixture(scope="module")
def rig(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    root = tmp_path_factory.mktemp("verify_e2e")
    raw = root / "source.wav"
    _write_programme(raw)
    # The source the crest claim is measured *against* must be in the same
    # stream shape as the master.  Measuring a mono WAV against a canonical
    # stereo FLAC reports a 3.0 dB crest difference that belongs to the mono to
    # dual-mono up-mix, not to the render: measured 25.300 dB mono against
    # 22.300 dB for both canonical files, i.e. 0.0000 dB between them.  Feeding
    # the raw mono file here made a correct render fail, which is the wrong
    # direction for a test to be wrong in.
    source = root / "source_canonical.flac"
    FfmpegTranscoder().render(
        source=raw,
        destination=source,
        audio_filters=list(CANONICAL_PROFILE.filter_arguments()),
        profile_args=CANONICAL_PROFILE.format_arguments(),
        container_hint=".flac",
    )
    transcoder = FfmpegTranscoder()
    linear = root / "linear.flac"
    crushed = root / "crushed.flac"
    for destination, filters in (
        (linear, ["volume=-6dB"]),
        (
            crushed,
            ["acompressor=threshold=0.03:ratio=20:attack=1:release=50", "volume=4dB"],
        ),
    ):
        transcoder.render(
            source=source,
            destination=destination,
            audio_filters=[*filters, *CANONICAL_PROFILE.filter_arguments()],
            profile_args=CANONICAL_PROFILE.format_arguments(),
            container_hint=".flac",
        )
    profile = LoudnessProfile(
        profile_id="custom", target_lufs=-26.4, true_peak_dbtp=-1.0, target_lra_lu=11.0
    )
    keyring = PassportKeyring(public_keys={KEY_ID: derive_public_key(SIGNING_KEY)})
    return {
        "root": root,
        "source": source,
        "linear": linear,
        "crushed": crushed,
        "transcoder": transcoder,
        "profile": profile,
        "keyring": keyring,
    }


def _passport(rig: dict[str, object], artifact: object, *, processing_mode: str = "STRICT_LINEAR"):
    profile = rig["profile"].model_copy(update={"processing_mode": processing_mode})  # type: ignore[union-attr]
    return build_passport(
        operation="audio.normalize_loudness",
        asset_id="artifact",
        path=artifact,
        source_sha256="sha256:" + "a" * 64,
        profile=CANONICAL_PROFILE,
        loudness_profile=profile,
        engine=rig["transcoder"],
        signing_key=SIGNING_KEY,
        signer_key_id=KEY_ID,
        parameters={"profile_id": "custom", "two_pass": True},
    )


def test_a_linear_master_verifies_end_to_end(rig: dict[str, object]) -> None:
    """The happy path, with every check in the chain actually running."""
    passport = _passport(rig, rig["linear"])
    report = verify_passport(
        passport,
        keyring=rig["keyring"],
        path=rig["linear"],
        engine=rig["transcoder"],
        source=rig["source"],
    )
    assert report.passed, (report.codes, report.details)
    assert report.signature_ok and report.bytes_ok and report.decode_ok


def test_a_compressed_master_claiming_strict_linear_fails_verification(
    rig: dict[str, object],
) -> None:
    """The check that was circular now refuses a real crushed artifact.

    Built from the *crushed* file with a STRICT_LINEAR claim and its true
    loudness, so the only thing wrong with it is the crest factor — which is the
    claim under audit.
    """
    passport = _passport(rig, rig["crushed"])
    report = verify_passport(
        passport,
        keyring=rig["keyring"],
        path=rig["crushed"],
        engine=rig["transcoder"],
        source=rig["source"],
    )
    assert not report.passed
    assert CODE_CREST_NOT_INVARIANT in report.codes, report.codes


def test_strict_linear_without_a_source_is_refused(rig: dict[str, object]) -> None:
    """Unverifiable is not verified: no source means no crest comparison."""
    passport = _passport(rig, rig["linear"])
    report = verify_passport(
        passport,
        keyring=rig["keyring"],
        path=rig["linear"],
        engine=rig["transcoder"],
    )
    assert not report.passed
    assert CODE_CREST_NOT_INVARIANT in report.codes


def test_a_tampered_sha_is_caught(rig: dict[str, object]) -> None:
    passport = _passport(rig, rig["linear"])
    tampered = passport.model_copy(update={"output_sha256": "sha256:" + "0" * 64})
    report = verify_passport(
        tampered,
        keyring=rig["keyring"],
        path=rig["linear"],
        engine=rig["transcoder"],
        source=rig["source"],
    )
    assert not report.passed
    assert "sha256_mismatch" in report.codes


def test_the_independent_meter_runs_as_part_of_verification(
    rig: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prove the cross-check is *reached*, by making it disagree.

    Patching the meter's tolerance to a negative value makes any real
    measurement disagree, so a verification that did not call it would still
    pass and this test would fail.
    """
    import nexus_ai_agent.creative.audio_engine.verify as verify_module

    passport = _passport(rig, rig["linear"])
    monkeypatch.setattr(verify_module, "LOUDNESS_CROSS_CHECK_TOLERANCE_LU", -1.0)
    report = verify_passport(
        passport,
        keyring=rig["keyring"],
        path=rig["linear"],
        engine=rig["transcoder"],
        source=rig["source"],
    )
    assert CODE_LOUDNESS_METER_DISAGREEMENT in report.codes, report.codes


def test_verification_can_be_asked_to_skip_the_cross_check(rig: dict[str, object]) -> None:
    """The flag is a flag: off means the code is not emitted."""
    import nexus_ai_agent.creative.audio_engine.verify as verify_module

    passport = _passport(rig, rig["linear"])
    original = verify_module.LOUDNESS_CROSS_CHECK_TOLERANCE_LU
    verify_module.LOUDNESS_CROSS_CHECK_TOLERANCE_LU = -1.0
    try:
        report = verify_passport(
            passport,
            keyring=rig["keyring"],
            path=rig["linear"],
            engine=rig["transcoder"],
            source=rig["source"],
            cross_check=False,
        )
    finally:
        verify_module.LOUDNESS_CROSS_CHECK_TOLERANCE_LU = original
    assert CODE_LOUDNESS_METER_DISAGREEMENT not in report.codes


def test_an_unsigned_passport_is_rejected_when_a_signature_is_required(
    rig: dict[str, object],
) -> None:
    passport = build_passport(
        operation="audio.normalize_loudness",
        asset_id="artifact",
        path=rig["linear"],
        source_sha256="sha256:" + "a" * 64,
        profile=CANONICAL_PROFILE,
        loudness_profile=rig["profile"],  # type: ignore[arg-type]
        engine=rig["transcoder"],
        include_signature=False,
    )
    report = verify_passport(
        passport,
        keyring=rig["keyring"],
        path=rig["linear"],
        engine=rig["transcoder"],
        source=rig["source"],
        require_signature=True,
    )
    assert not report.passed
    assert "passport_unsigned" in report.codes


def test_an_unknown_signer_key_is_rejected(rig: dict[str, object]) -> None:
    passport = _passport(rig, rig["linear"])
    other_key = bytes(range(1, 33))
    report = verify_passport(
        passport,
        keyring=PassportKeyring(public_keys={KEY_ID: derive_public_key(other_key)}),
        path=rig["linear"],
        engine=rig["transcoder"],
        source=rig["source"],
    )
    assert not report.passed
    assert "passport_signature_invalid" in report.codes
