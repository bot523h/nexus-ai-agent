"""Explicit green-reference calibration and evidence-bearing model HFR rendering."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas import (
    VeritasRealityAuditor,
    build_corrupted_15_law_cinema_shot,
    calibrate_catadioptric_optics,
    synthesize_rotary_subframes,
    validate_rotary_subframes,
)
from nexus_ai_agent.creative.veritas.omnia_cinema_physics import (
    StroboscopicAliasingError,
    _fringe_metric,
)
from nexus_ai_agent.creative.veritas.real_media_veritas_compiler import (
    _read_png_rgb,
    _write_png,
    compile_and_heal_real_media_shot,
)
from nexus_ai_agent.creative.veritas.veritas_spine_adapter import (
    VeritasMediaVerificationError,
    VeritasSpinePublishRequest,
    VeritasStagedSpineAdapter,
    verify_compiled_veritas_bundle_on_disk,
)


def test_fringe_closure_in_exact_png_domain(tmp_path: Path) -> None:
    image = build_corrupted_15_law_cinema_shot().anamorphic
    assert image is not None and _fringe_metric(image) > 1e-6
    result = calibrate_catadioptric_optics(image)
    path = tmp_path / "calibrated.png"
    _write_png(path, np.rint(result.healed_image * 255).astype(np.uint8))
    decoded = _read_png_rgb(path).astype(float) / 255
    assert _fringe_metric(decoded) <= 1e-6
    assert result.fringe_after == _fringe_metric(decoded)
    assert result.fringe_before > result.fringe_after
    greenless = np.zeros((4, 4, 3))
    greenless[..., 0] = 1
    assert calibrate_catadioptric_optics(greenless).fringe_after > 1e-6


def test_full_fixture_raw_and_healed_reports() -> None:
    shot = build_corrupted_15_law_cinema_shot()
    auditor = VeritasRealityAuditor()
    raw = auditor.audit_and_heal(shot, heal=False)
    assert raw.pre_audit_violations == raw.post_heal_violations == 20
    report = auditor.audit_and_heal(shot)
    assert (report.pre_audit_violations, report.post_heal_violations) == (20, 0)
    healed = auditor.heal_cinema_shot(shot)
    recheck = auditor.audit_and_heal(healed, heal=False)
    assert recheck.pre_audit_violations == recheck.post_heal_violations == 0
    assert healed.rotor is not None and healed.rotor_samples is not None
    broken_clock = replace(healed, rotor=(healed.rotor[0], 201.0, 97.0))
    assert "trajectory_clock_mismatch" in auditor.audit_and_heal(broken_clock).details_after[15]
    broken_patch = replace(healed, rotor=(np.zeros_like(healed.rotor[0]), *healed.rotor[1:]))
    assert "trajectory_patch_mismatch" in auditor.audit_and_heal(broken_patch).details_after[15]


@pytest.mark.parametrize(
    "case", ["shape", "clock", "velocity", "count", "nodes", "shutter", "budget"]
)
def test_subframe_render_rejects_invalid_inputs(case: str) -> None:
    with pytest.raises(StroboscopicAliasingError):
        if case == "shape":
            synthesize_rotary_subframes(np.ones(4), angular_velocity_rad_s=200, fps=24)
        elif case == "clock":
            synthesize_rotary_subframes(np.ones((4, 4)), angular_velocity_rad_s=200, fps=0)
        elif case == "velocity":
            synthesize_rotary_subframes(np.ones((4, 4)), angular_velocity_rad_s=np.nan, fps=24)
        elif case == "count":
            synthesize_rotary_subframes(
                np.ones((4, 4)), angular_velocity_rad_s=200, fps=24, source_frame_count=0
            )
        elif case == "nodes":
            synthesize_rotary_subframes(
                np.ones((4, 4)), angular_velocity_rad_s=200, fps=24, samples=1
            )
        elif case == "shutter":
            synthesize_rotary_subframes(
                np.ones((4, 4)), angular_velocity_rad_s=200, fps=24, shutter_angle_deg=0
            )
        else:
            synthesize_rotary_subframes(
                np.ones((4, 4)), angular_velocity_rad_s=200, fps=24, source_frame_count=5000
            )


@pytest.mark.parametrize(
    "case",
    [
        "zero_fps",
        "factor",
        "shape",
        "nan",
        "times",
        "phase",
        "nyquist",
        "shutter",
        "asymmetry",
        "render",
    ],
)
def test_subframe_validation_rejects_missing_or_forged_evidence(case: str) -> None:
    patch = np.zeros((8, 8))
    patch[1:7, 4:6] = 1
    sequence = synthesize_rotary_subframes(patch, angular_velocity_rad_s=200, fps=24)
    assert not validate_rotary_subframes(sequence)
    if case == "zero_fps":
        sequence = replace(sequence, output_fps=0)
    elif case == "factor":
        sequence = replace(sequence, source_frame_count=3)
    elif case == "shape":
        sequence = replace(sequence, frames=sequence.frames[:-1])
    elif case == "nan":
        sequence = replace(sequence, frames=sequence.frames * np.nan)
    elif case == "times":
        sequence = replace(sequence, timestamps_s=sequence.timestamps_s + 0.01)
    elif case == "phase":
        sequence = replace(sequence, phases_rad=np.remainder(sequence.phases_rad, 2 * np.pi))
    elif case == "nyquist":
        sequence = replace(sequence, output_fps=24)
    elif case == "shutter":
        sequence = replace(sequence, shutter_angle_deg=15)
    elif case == "asymmetry":
        sequence = replace(sequence, exposure_offsets_s=sequence.exposure_offsets_s + 0.01)
    else:
        sequence = replace(
            sequence, frames=np.repeat(sequence.frames[:1], len(sequence.frames), axis=0)
        )
    assert validate_rotary_subframes(sequence)


def test_hfr_disk_evidence_publish_and_tamper(tmp_path: Path) -> None:
    shot = build_corrupted_15_law_cinema_shot(height=8, width=8)
    # Known-rotation rendering of a green-reference flat-field calibration shot.
    frames = np.empty((2, 8, 8, 3))
    frames[:] = [0.4, 0.4, 0.4]
    request = VeritasSpinePublishRequest(
        "p",
        "r",
        "s",
        "a",
        1,
        tmp_path,
        tmp_path / "published",
        shot=shot,
        input_frames_rgb=frames,
        input_audio_pcm=np.zeros(4800),
    )
    result = VeritasStagedSpineAdapter(lambda: 1).execute(request)
    bundle = result.compile_result
    assert result.post_disk_verified and bundle.post_disk_verified
    assert bundle.output_fps == 96 and bundle.source_fps == 24
    assert len(bundle.png_paths) == 8
    assert bundle.rotary_evidence_sha256
    assert all(
        payload["fps"] == 96
        and payload["output_frame_count"] == 8
        and payload["rotary_evidence_sha256"] == bundle.rotary_evidence_sha256
        for payload in bundle.leaf_payloads
    )
    assert verify_compiled_veritas_bundle_on_disk(bundle)
    path = result.published_dir / "rotary_subframes.json"
    path.write_text(path.read_text() + " ")
    with pytest.raises(VeritasMediaVerificationError, match="rotary evidence digest"):
        verify_compiled_veritas_bundle_on_disk(bundle)


def test_textured_hfr_output_is_still_refused_if_other_proxies_fail(tmp_path: Path) -> None:
    shot = build_corrupted_15_law_cinema_shot(height=8, width=8)
    assert shot.anamorphic is not None
    bundle = compile_and_heal_real_media_shot(
        tmp_path,
        shot=shot,
        input_frames=np.stack([shot.anamorphic] * 2),
        input_audio_pcm=np.zeros(4800),
        mux_mp4=False,
    )
    assert bundle.post_heal_violations == 0  # state certificate is not a pixel certificate
    assert not bundle.post_disk_verified
    with pytest.raises(VeritasMediaVerificationError, match="physical observations"):
        verify_compiled_veritas_bundle_on_disk(bundle)


def test_compiler_refuses_empty_audio(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        compile_and_heal_real_media_shot(tmp_path, input_audio_pcm=np.zeros(0), mux_mp4=False)
