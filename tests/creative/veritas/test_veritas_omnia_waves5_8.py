"""Regression gates for temporal continuity and independent facial channels."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas.continuum_and_quantum_mechanics import (
    madelung_continuity_residual,
    stabilize_madelung_phase_coherence,
)
from nexus_ai_agent.creative.veritas.omnia_cinema_physics import (
    audit_and_heal_viseme_formant_sync,
    enforce_helmholtz_fluid_conservation,
)


def test_temporal_continuity_is_measured_and_reduced() -> None:
    previous = np.ones((9, 5))
    density = previous * 2.0
    phase = np.zeros_like(density)
    result = stabilize_madelung_phase_coherence(density, phase, previous_density=previous)
    assert result.continuity_residual_before == pytest.approx(24.0)
    assert result.continuity_residual_after < result.continuity_residual_before
    assert result.continuity_residual_after <= 0.25
    assert "madelung_continuity_residual_exceeded" in result.violations
    assert not result.violations_after
    assert result.continuity_residual_after == pytest.approx(
        madelung_continuity_residual(
            result.density, result.phase, previous_density=previous, dt=1 / 24
        )
    )
    np.testing.assert_array_equal(density, previous * 2.0)
    np.testing.assert_allclose(np.abs(result.wavefunction) ** 2, result.density)


def test_zero_iteration_budget_reports_remaining_continuity_and_boiling() -> None:
    density = np.ones((8, 8)) * 2
    phase = np.indices((8, 8)).sum(axis=0) % 2
    result = stabilize_madelung_phase_coherence(
        density, phase.astype(float), previous_density=np.ones((8, 8)), max_iterations=0
    )
    assert "madelung_continuity_residual_exceeded" in result.violations_after
    assert "temporal_phase_boiling" in result.violations_after


@pytest.mark.parametrize("case", ["dimension", "finite", "limit", "dt", "negative_previous"])
def test_phase_rejects_invalid_inputs(case: str) -> None:
    density = np.ones((4, 4))
    phase = np.zeros_like(density)
    if case == "dimension":
        with pytest.raises(ValueError, match="at least 2-D"):
            stabilize_madelung_phase_coherence(np.ones(4), np.zeros(4))
    elif case == "finite":
        phase[0, 0] = np.nan
        with pytest.raises(ValueError, match="finite"):
            stabilize_madelung_phase_coherence(density, phase)
    elif case == "limit":
        with pytest.raises(ValueError, match="continuity_residual_max"):
            stabilize_madelung_phase_coherence(density, phase, continuity_residual_max=0)
    elif case == "dt":
        with pytest.raises(ValueError, match="finite"):
            stabilize_madelung_phase_coherence(density, phase, dt=np.nan)
    else:
        with pytest.raises(ValueError, match="non-negative"):
            stabilize_madelung_phase_coherence(density, phase, previous_density=-density)


@pytest.mark.parametrize("case", ["finite", "previous_shape", "previous_finite"])
def test_continuity_rejects_invalid_inputs(case: str) -> None:
    density = np.ones((4, 4))
    phase = np.zeros_like(density)
    with pytest.raises(ValueError):
        if case == "finite":
            madelung_continuity_residual(density * np.nan, phase)
        elif case == "previous_shape":
            madelung_continuity_residual(density, phase, previous_density=np.ones((3, 3)))
        else:
            madelung_continuity_residual(density, phase, previous_density=density * np.nan)


def test_au18_healing_does_not_overwrite_au20() -> None:
    au20 = np.full(4, 0.2)
    au18 = np.zeros(4)
    result = audit_and_heal_viseme_formant_sync(
        np.ones(4),
        au20,
        f1_hz=np.full(4, 700.0),
        au18_lip_pucker=au18,
        f2_hz=np.full(4, 800.0),
    )
    np.testing.assert_array_equal(result.au20_healed, au20)
    assert result.au18_lip_pucker_healed is not None
    np.testing.assert_allclose(result.au18_lip_pucker_healed, 0.48)
    np.testing.assert_array_equal(au18, np.zeros(4))
    assert "lip_rounding_underarticulation" in result.violations_before
    assert not result.violations_after


@pytest.mark.parametrize("case", ["shape", "finite", "range"])
def test_au18_rejects_invalid_input(case: str) -> None:
    pucker = {"shape": np.zeros(3), "finite": np.full(4, np.nan), "range": np.full(4, 2)}[case]
    with pytest.raises(ValueError):
        audit_and_heal_viseme_formant_sync(
            np.ones(4), np.zeros(4), f1_hz=np.full(4, 700.0), au18_lip_pucker=pucker
        )


def test_rectangular_odd_grid_projection() -> None:
    rng = np.random.default_rng(23)
    result = enforce_helmholtz_fluid_conservation(rng.normal(size=(2, 9, 5, 2)), np.ones((2, 9, 5)))
    assert result.divergence_after < 1e-12


def test_auditor_uses_temporal_density_and_independent_pucker() -> None:
    from nexus_ai_agent.creative.veritas.reality_auditor import CinemaShot, VeritasRealityAuditor

    density = np.ones((9, 5))
    shot = CinemaShot(
        phase=(density * 2, np.zeros_like(density), density),
        viseme=(
            np.ones(4),
            np.full(4, 0.2),
            np.full(4, 700.0),
            np.ones(4),
            np.zeros(4),
            np.full(4, 800.0),
        ),
    )
    passport = VeritasRealityAuditor().audit_and_heal_cinema_shot(shot)
    assert "madelung_continuity_residual_exceeded" in passport.details_before[11]
    assert "lip_rounding_underarticulation" in passport.details_before[13]
    assert not passport.details_after[11]
    assert not passport.details_after[13]


def test_disk_content_digests_and_rgb_preservation(tmp_path: Path) -> None:
    import hashlib
    from dataclasses import replace

    from nexus_ai_agent.creative.veritas.real_media_veritas_compiler import (
        _read_png_rgb,
        _read_wav_mono_16bit,
        compile_and_heal_real_media_shot,
        merkle_root_from_payloads,
    )

    frames = np.zeros((2, 8, 8, 3))
    frames[..., 0] = 0.7
    frames[..., 1] = 0.3
    frames[..., 2] = 0.1
    audio = np.sin(np.arange(240) * 0.3) * 0.4
    bundle = compile_and_heal_real_media_shot(
        tmp_path, input_frames=frames, input_audio_pcm=audio, mux_mp4=False
    )
    decoded = np.stack([_read_png_rgb(Path(path)) for path in bundle.png_paths])
    pcm = _read_wav_mono_16bit(Path(bundle.wav_path))
    assert (
        bundle.input_frames_content_sha256
        == hashlib.sha256(np.round(frames * 255).astype(np.uint8).tobytes()).hexdigest()
    )
    assert (
        bundle.input_audio_content_sha256
        == hashlib.sha256(np.round(audio * 32767).astype(np.int16).tobytes()).hexdigest()
    )
    assert bundle.output_frames_content_sha256 == hashlib.sha256(decoded.tobytes()).hexdigest()
    assert bundle.output_audio_content_sha256 == hashlib.sha256(pcm.tobytes()).hexdigest()
    assert np.any(decoded[0, ..., 0] != decoded[0, ..., 2])
    assert merkle_root_from_payloads(bundle.leaf_payloads) == bundle.merkle_root
    assert bundle.media_changed
    assert not replace(
        bundle, output_frames_content_sha256=bundle.input_frames_content_sha256
    ).media_changed
    for payload in bundle.leaf_payloads:
        for name in (
            "input_frames_content_sha256",
            "output_frames_content_sha256",
            "input_audio_content_sha256",
            "output_audio_content_sha256",
            "output_frames_sha256",
            "output_audio_sha256",
            "output_mp4_sha256",
        ):
            assert payload[name] == getattr(bundle, name)


@pytest.mark.parametrize(
    "case",
    [
        "signature",
        "short_chunk",
        "short_payload",
        "crc",
        "header_length",
        "duplicate_header",
        "encoding",
        "dimensions",
        "data_before_header",
        "end_payload",
        "trailing",
        "unknown_chunk",
        "missing_end",
        "missing_data",
        "short_stream",
        "bad_filter",
        "extra_stream",
        "truncated_stream",
    ],
)
def test_png_decoder_refuses_corrupt_or_unsupported_files(tmp_path: Path, case: str) -> None:
    import struct
    import zlib

    from nexus_ai_agent.creative.veritas.real_media_veritas_compiler import _read_png_rgb

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    signature = b"\x89PNG\r\n\x1a\n"
    header = chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
    raw = b"\x00" * 14
    compressed = zlib.compress(raw)
    data = chunk(b"IDAT", compressed)
    end = chunk(b"IEND", b"")
    variants = {
        "signature": b"invalid",
        "short_chunk": signature + b"\x00",
        "short_payload": signature + struct.pack(">I", 100) + b"IHDRxxxx",
        "crc": signature + header[:-1] + bytes([header[-1] ^ 1]),
        "header_length": signature + chunk(b"IHDR", b""),
        "duplicate_header": signature + header + header,
        "encoding": signature + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 16, 2, 0, 0, 0)),
        "dimensions": signature + chunk(b"IHDR", struct.pack(">IIBBBBB", 0, 2, 8, 2, 0, 0, 0)),
        "data_before_header": signature + data,
        "end_payload": signature + header + data + chunk(b"IEND", b"x"),
        "trailing": signature + header + data + end + b"x",
        "unknown_chunk": signature + header + chunk(b"XXXX", b""),
        "missing_end": signature + header + data,
        "missing_data": signature + header + end,
        "short_stream": signature + header + chunk(b"IDAT", zlib.compress(b"x")) + end,
        "bad_filter": signature + header + chunk(b"IDAT", zlib.compress(b"\x01" + raw[1:])) + end,
        "extra_stream": signature + header + chunk(b"IDAT", compressed + b"x") + end,
        "truncated_stream": signature + header + chunk(b"IDAT", compressed[:-1]) + end,
    }
    path = tmp_path / "corrupt.png"
    path.write_bytes(variants[case])
    with pytest.raises(ValueError):
        _read_png_rgb(path)


@pytest.mark.parametrize("case", ["stereo", "eight_bit", "truncated"])
def test_wav_decoder_refuses_noncanonical_or_truncated_pcm(tmp_path: Path, case: str) -> None:
    import wave

    from nexus_ai_agent.creative.veritas.real_media_veritas_compiler import _read_wav_mono_16bit

    path = tmp_path / "bad.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2 if case == "stereo" else 1)
        handle.setsampwidth(1 if case == "eight_bit" else 2)
        handle.setframerate(48000)
        handle.writeframes(bytes(16))
    if case == "truncated":
        path.write_bytes(path.read_bytes()[:-2])
    with pytest.raises(ValueError):
        _read_wav_mono_16bit(path)


@pytest.mark.parametrize("case", ["shape", "small", "range", "audio", "rate"])
def test_media_extraction_rejects_invalid_observations(case: str) -> None:
    from nexus_ai_agent.creative.veritas.real_media_veritas_compiler import (
        extract_cinema_shot_from_media,
    )

    frames = np.ones((2, 8, 8, 3)) * 0.4
    audio = np.zeros(128)
    with pytest.raises(ValueError):
        if case == "shape":
            extract_cinema_shot_from_media(frames[0], audio)
        elif case == "small":
            extract_cinema_shot_from_media(frames[:, :1], audio)
        elif case == "range":
            extract_cinema_shot_from_media(frames * 4, audio)
        elif case == "audio":
            extract_cinema_shot_from_media(frames, np.array([np.nan]))
        else:
            extract_cinema_shot_from_media(frames, audio, fps=0)


def test_media_extraction_tracks_consecutive_luminance_and_copies_audio() -> None:
    from nexus_ai_agent.creative.veritas.real_media_veritas_compiler import (
        extract_cinema_shot_from_media,
    )

    frames = np.ones((2, 2, 2, 3)) * 0.4
    frames[1] *= 2
    audio = np.array([0.2])
    shot = extract_cinema_shot_from_media(frames, audio)
    assert shot.phase is not None and len(shot.phase) == 3
    np.testing.assert_allclose(shot.phase[0], 0.8)
    np.testing.assert_allclose(shot.phase[2], 0.4)
    assert shot.doppler is not None
    np.testing.assert_array_equal(shot.doppler[0], audio)
    assert not np.shares_memory(shot.doppler[0], audio)


def test_geometric_healing_preserves_observations_and_reaudits() -> None:
    from nexus_ai_agent.creative.veritas.reality_auditor import (
        VeritasRealityAuditor,
        build_corrupted_15_law_cinema_shot,
    )

    shot = build_corrupted_15_law_cinema_shot()
    assert shot.collision is not None and shot.mirror is not None
    positions_before = shot.collision[0].copy()
    landmark_before = shot.mirror[3].copy()
    auditor = VeritasRealityAuditor(laws=(2, 3, 4, 6, 7))
    healed = auditor._heal_geometric_observations(shot)
    result = auditor.audit_and_heal_cinema_shot(healed)
    assert result.pre_audit_violations == result.post_heal_violations == 0
    assert healed.eyes is not None and healed.mirror is not None
    assert healed.shadow is not None and healed.typography is not None
    assert healed.collision is not None
    np.testing.assert_array_equal(shot.collision[0], positions_before)
    np.testing.assert_array_equal(shot.mirror[3], landmark_before)
    np.testing.assert_allclose(
        np.sum(healed.collision[2][:, None] * healed.collision[0], axis=0),
        np.sum(shot.collision[2][:, None] * shot.collision[0], axis=0),
    )


def test_full_shot_refuses_false_zero_zero_certificate() -> None:
    from nexus_ai_agent.creative.veritas import (
        VeritasHealingRefusalError,
        build_corrupted_15_law_cinema_shot,
        heal_cinema_shot,
    )

    shot = build_corrupted_15_law_cinema_shot()
    with pytest.raises(VeritasHealingRefusalError) as refusal:
        heal_cinema_shot(shot)
    residual = refusal.value.passport
    assert residual.details_before[12] == ("lateral_chromatic_aberration",)
    assert residual.details_before[15] == ("nyquist_rotary_aliasing",)
    assert all(not values for law, values in residual.details_before.items() if law not in (12, 15))
    assert shot.rotor is not None and shot.rotor[1:] == (200.0, 24.0)


def test_representable_clean_optics_and_sampling_round_trip() -> None:
    from dataclasses import replace

    from nexus_ai_agent.creative.veritas import (
        VeritasRealityAuditor,
        build_corrupted_15_law_cinema_shot,
        heal_cinema_shot,
        retarded_wavefront,
    )

    shot = build_corrupted_15_law_cinema_shot()
    assert shot.phase is not None and shot.viseme is not None and shot.rotor is not None
    wave = retarded_wavefront((8, 8), (3.0, 3.0), 1.0, 1.0)
    wave[0, 0, 0] = 1
    shot = replace(
        shot,
        anamorphic=np.zeros((16, 16, 3)),
        rotor=(shot.rotor[0], 20, 24),
        phase=(shot.phase[0], shot.phase[1], shot.phase[0] * 0.5),
        wave=(wave, (3.0, 3.0), 1.0, 1.0),
        viseme=(*shot.viseme, np.zeros(3), np.full(3, 800.0)),
    )
    healed = heal_cinema_shot(shot)
    report = VeritasRealityAuditor().audit_and_heal_cinema_shot(healed)
    assert report.pre_audit_violations == report.post_heal_violations == 0
    assert healed.hand is not None and shot.hand is not None
    assert healed.objects is not None and shot.objects is not None
    assert healed.cloth is not None and shot.cloth is not None
    assert healed.scalar_frames is not None and shot.scalar_frames is not None
    assert healed.facs is not None and shot.facs is not None
    assert healed.fluid is not None and shot.fluid is not None
    assert len(healed.objects) == len(shot.objects)
    assert healed.cloth[0].shape == shot.cloth[0].shape
    assert healed.scalar_frames.shape == shot.scalar_frames.shape
    assert healed.facs.shape == shot.facs.shape
    assert healed.fluid[0].shape == shot.fluid[0].shape


def test_empty_shot_projection_is_clean() -> None:
    from nexus_ai_agent.creative.veritas import CinemaShot, heal_cinema_shot

    assert heal_cinema_shot(CinemaShot()) == CinemaShot()
