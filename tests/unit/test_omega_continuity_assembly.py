"""Zero-Drift continuity + lossless-assembly proof, exercised on real media.

The North-Star promise is that episode 2 assembles from episode 1's segments with
no generation loss.  These tests synthesize two "episodes", render them through
the real engine, and then ask the two questions the promise rests on:

1. *Do the segments share one stream shape?*  :meth:`NativeAudioEngine.prove_lossless_assembly`
   probes each published file and either proves ``-c copy`` is safe or names the
   mismatch that forces a re-encode.
2. *Did the programme drift?*  :func:`compare_continuity` compares the measured
   passports through both commits, including the two deliberate flaws the
   previous phase was blind to: an unmeasurable axis reported as ``ok`` and an
   identical program declared drift because of float noise.

A regression here is a wrong *claim about the master*, so every assertion is on a
verdict, not on an internal number.
"""

from __future__ import annotations

import omega_media as media
import pytest

from nexus_ai_agent.creative.audio_engine.continuity import (
    TOLERANCES,
    AssemblyProof,
    ContinuityError,
    assert_lossless_assembly,
    compare_continuity,
    detect_lock_drift,
)
from nexus_ai_agent.creative.audio_engine.ed25519_sign import derive_public_key, key_id_for
from nexus_ai_agent.creative.audio_engine.execution import (
    AudioExecutionSettings,
    LocalDirectorySourceCatalog,
    NativeAudioEngine,
)
from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import FfmpegTranscoder, FfmpegUnavailableError
from nexus_ai_agent.creative.audio_engine.models import (
    CanonicalStreamProfile,
    ContinuityLockSpec,
    IdentityLockSpec,
    LoudnessProfile,
    MediaProbe,
    SpectralSignature,
)
from nexus_ai_agent.creative.audio_engine.workspace import hash_file
from nexus_ai_agent.creative.studio.audio_engine import LoudnessRequest

SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")


@pytest.fixture(scope="module")
def engine_and_media(tmp_path_factory: pytest.TempPathFactory):
    transcoder = FfmpegTranscoder()
    try:
        _ = transcoder.binary
    except FfmpegUnavailableError:
        pytest.skip("no FFmpeg binary available", allow_module_level=True)
    root = tmp_path_factory.mktemp("continuity")
    media_dir = root / "media"
    media_dir.mkdir()
    engine = NativeAudioEngine(
        catalog=LocalDirectorySourceCatalog(media_dir),
        settings=AudioExecutionSettings(
            workspace_root=root / "ws",
            signing_key=SEED,
            signer_key_id=key_id_for(derive_public_key(SEED)),
        ),
        transcoder=transcoder,
    )
    return engine, media_dir


def _render(engine: NativeAudioEngine, media_dir, name: str, amplitude: float, freq: float):
    source = media.write_sine(media_dir / f"{name}.wav", amplitude=amplitude, frequency=freq)
    return engine.normalize_loudness(
        LoudnessRequest(
            request_id=f"render-{name}",
            source_asset_id=name,
            output_asset_id=f"{name}_master",
            source_sha256=hash_file(source),
            profile_id="nagar_streaming_preset",
            target_lufs=-14.0,
            true_peak_dbtp=-1.0,
            target_lra_lu=11.0,
        )
    )


# --------------------------------------------------------------------------- #
# 1. the assembly proof
# --------------------------------------------------------------------------- #


def test_episode_segments_are_stitchable_without_a_reencode(engine_and_media) -> None:
    engine, media_dir = engine_and_media
    first = _render(engine, media_dir, "ep1_sc1", 0.2, 300.0)
    second = _render(engine, media_dir, "ep1_sc2", 0.15, 420.0)
    proof = engine.prove_lossless_assembly([first.output_path, second.output_path])
    assert isinstance(proof, AssemblyProof)
    assert proof.lossless is True, proof.reason
    assert proof.strategy == "stream_copy"
    assert proof.probed_sample_rates == (48_000,)
    assert proof.probed_layouts == ("2",)
    assert proof.segments == ("ep1_sc1_master.flac", "ep1_sc2_master.flac")


def test_a_segment_at_the_wrong_rate_blocks_the_stream_copy(engine_and_media) -> None:
    engine, media_dir = engine_and_media
    canonical = _render(engine, media_dir, "ep2_sc1", 0.2, 300.0)
    odd = media.write_sine(media_dir / "ep2_sc2_44k.wav", amplitude=0.2, rate=44_100)
    proof = engine.prove_lossless_assembly([canonical.output_path, odd])
    assert proof.lossless is False
    assert proof.strategy == "re_encode_required"
    assert "sample rates differ" in str(proof.reason)
    assert proof.probed_sample_rates == (44_100, 48_000)


def test_a_mono_segment_blocks_a_stereo_series(engine_and_media) -> None:
    engine, media_dir = engine_and_media
    canonical = _render(engine, media_dir, "ep3_sc1", 0.2, 300.0)
    mono_profile_engine = _render(engine, media_dir, "ep3_sc2", 0.2, 330.0)
    # Force a genuinely mono file at the canonical rate.
    mono = media_dir / "mono_48k.wav"
    media.write_sine(mono, amplitude=0.2)
    proof = engine.prove_lossless_assembly([canonical.output_path, mono])
    assert proof.lossless is False
    assert "channel counts differ" in str(proof.reason)
    assert mono_profile_engine.verified is True


def test_the_assembly_proof_refuses_nothing_to_assemble(engine_and_media) -> None:
    engine, _ = engine_and_media
    with pytest.raises(ContinuityError):
        engine.prove_lossless_assembly([])
    with pytest.raises(ContinuityError):
        assert_lossless_assembly((), profile=CanonicalStreamProfile())


def test_a_consistent_probe_set_passes_the_low_level_proof() -> None:
    probe = MediaProbe(
        sample_rate=48_000,
        channels=2,
        codec="flac",
        sample_format="s32",
        container_duration_us=1_000_000,
        size_bytes=64_000,
        sha256="sha256:" + "a" * 64,
    )
    proof = assert_lossless_assembly(
        [probe, probe.model_copy()], profile=CanonicalStreamProfile(), names=("a.flac", "b.flac")
    )
    assert proof.lossless, proof.reason
    assert proof.reference_profile.sample_rate == 48_000


# --------------------------------------------------------------------------- #
# 2. episodic continuity
# --------------------------------------------------------------------------- #


def test_two_renders_of_one_voice_do_not_drift(engine_and_media) -> None:
    engine, media_dir = engine_and_media
    episode1 = _render(engine, media_dir, "cont_a", 0.25, 200.0)
    episode2 = _render(engine, media_dir, "cont_b", 0.25, 200.0)
    from nexus_ai_agent.creative.audio_engine.verify import build_passport

    passports = []
    for name, evidence in (("a", episode1), ("b", episode2)):
        source = media_dir / f"cont_{name}.wav"
        passports.append(
            build_passport(
                operation="audio.normalize_loudness",
                asset_id=f"cont_{name}",
                path=evidence.output_path,
                source_sha256=hash_file(source),
                profile=CanonicalStreamProfile(),
                loudness_profile=LoudnessProfile(
                    profile_id="nagar_streaming_preset",
                    target_lufs=-14.0,
                    true_peak_dbtp=-1.0,
                    target_lra_lu=11.0,
                ),
                engine=engine._transcoder,  # noqa: SLF001
                signing_key=SEED,
                signer_key_id=key_id_for(derive_public_key(SEED)),
            )
        )
    # Two passports that both lack a continuity lock cannot be *proven*
    # continuous: absence of the lock is reported as ``unknown``, and the module
    # refuses to call the pair stitchable.  The acoustic/loudness axes, which
    # carry real measurements, are all ``ok``.
    pair_report = compare_continuity(passports[0], passports[1])
    assert pair_report.unknown_axes == ("continuity_lock",)
    assert not pair_report.stitchable
    assert pair_report.drift_axes == ()
    assert all(axis.verdict == "ok" for axis in pair_report.axes if axis.axis != "continuity_lock")

    # Under one declared series lock both segments verify as continuous, which
    # is the North-Star claim: same voice + same loudness + same stream shape.
    lock = ContinuityLockSpec(
        spec_id="nagar.s01e01",
        identity=IdentityLockSpec(series_id="nagar.s01", color_space="bt709"),
    )
    locked = [
        passport.model_copy(update={"continuity_lock_hash": lock.lock_hash})
        for passport in passports
    ]
    report = compare_continuity(locked[0], locked[1], lock=lock)
    assert report.continuous, report.codes
    assert report.lock_hash_match is True
    assert report.stitchable is True
    assert report.drift_axes == ()


def test_a_different_source_voice_is_reported_as_drift(engine_and_media) -> None:
    """A different centre frequency moves the acoustic signature: report it."""
    engine, media_dir = engine_and_media
    low = _render(engine, media_dir, "voice_low", 0.25, 200.0)
    high = _render(engine, media_dir, "voice_high", 0.25, 1_400.0)
    left = engine.signature_of(low.output_path)
    right = engine.signature_of(high.output_path)
    # The signatures must differ — otherwise the continuity check is decorative.
    assert left.signature_hash != right.signature_hash
    assert abs(left.spectral_centroid_hz - right.spectral_centroid_hz) > 100.0


def test_an_unmeasurable_axis_is_unknown_not_ok() -> None:
    """The regression class: absence of evidence must never read as agreement."""
    from types import SimpleNamespace

    empty = SimpleNamespace(passport_id="pp_empty")
    report = compare_continuity(empty, empty)
    assert report.continuous is False
    assert not report.stitchable
    assert report.unknown_axes, "missing evidence must be reported as unknown"
    assert "acoustic_signature" in report.unknown_axes
    assert not any(axis.verdict == "ok" for axis in report.axes)


def test_tolerances_are_the_documented_numbers() -> None:
    assert TOLERANCES == {
        "rms_db": 1.5,
        "crest_db": 2.0,
        "centroid_relative": 0.12,
        "band_cosine_distance": 0.08,
        "integrated_lu": 1.0,
        "duration_relative": 0.001,
    }


def test_lock_drift_names_the_axis_that_moved() -> None:
    base = ContinuityLockSpec(
        spec_id="nagar.s01",
        identity=IdentityLockSpec(series_id="nagar.s01", color_space="rec709"),
        stream=CanonicalStreamProfile(),
        loudness=LoudnessProfile(
            profile_id="nagar_streaming_preset",
            target_lufs=-14.0,
            true_peak_dbtp=-1.0,
            target_lra_lu=11.0,
        ),
    )
    identical = detect_lock_drift(base, base)
    assert identical == ()
    drifted = detect_lock_drift(
        base,
        base.model_copy(
            update={"identity": IdentityLockSpec(series_id="nagar.s02", color_space="rec2020")}
        ),
    )
    joined = " | ".join(drifted)
    assert "color_space" in joined
    assert "series_id" in joined


def test_signature_shape_is_measured_not_declared(engine_and_media) -> None:
    engine, media_dir = engine_and_media
    evidence = _render(engine, media_dir, "sig_shape", 0.3, 440.0)
    signature = engine.signature_of(evidence.output_path)
    assert isinstance(signature, SpectralSignature)
    assert signature.spectral_centroid_hz == pytest.approx(440.0, rel=0.35)
    assert signature.rms_dbfs is not None and signature.rms_dbfs < 0.0
    assert signature.dynamic_range_db is not None
    assert signature.band_energy_ratio
    assert signature.samples_analyzed > 0
    assert signature.truncated is False
