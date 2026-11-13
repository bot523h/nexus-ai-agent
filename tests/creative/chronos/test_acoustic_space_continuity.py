"""Room geometry, binaural image sources, and cross-shot acoustic continuity."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.chronos import acoustic_space_continuity
from nexus_ai_agent.creative.chronos.acoustic_space_continuity import (
    AcousticRoomSpec,
    BinauralRIRReceipt,
    audit_cross_shot_acoustic_continuity,
    compute_room_acoustic_invariants,
    convolve_shot_dialogue_with_room_acoustics,
    synthesize_binaural_room_impulse_response,
)


@pytest.fixture
def room() -> AcousticRoomSpec:
    return AcousticRoomSpec(
        room_id="stage-a",
        dimensions_m=(12.0, 10.0, 4.0),
        wall_absorption=(0.25, 0.25, 0.25, 0.25, 0.25, 0.25),
    )


def test_room_sabine_eyring_invariants(room: AcousticRoomSpec) -> None:
    receipt = compute_room_acoustic_invariants(room)
    volume = 12.0 * 10.0 * 4.0
    area = 2 * (12.0 * 10.0 + 12.0 * 4.0 + 10.0 * 4.0)
    expected_rt60 = 0.161 * volume / (-area * np.log1p(-0.25))
    assert receipt.room_id == "stage-a"
    assert receipt.volume_m3 == volume
    assert receipt.surface_area_m2 == area
    assert receipt.area_weighted_mean_absorption == pytest.approx(0.25)
    assert receipt.rt60_seconds == pytest.approx(expected_rt60)
    assert receipt.critical_distance_m > 0


def test_binaural_image_source_rir_has_27_paths_and_woodworth_stereo(
    room: AcousticRoomSpec,
) -> None:
    source = np.array([3.0, 5.0, 2.0])
    camera = np.array([4.0, 5.0, 2.0])
    receipt = synthesize_binaural_room_impulse_response(
        room, source, camera, np.eye(3), sample_rate_hz=8_000, max_order=2
    )
    assert receipt.path_count == 27
    assert receipt.stereo_rir.ndim == 2 and receipt.stereo_rir.shape[1] == 2
    assert receipt.direct_arrival_sample == round(8_000 / 343.0)
    assert receipt.direct_distance_m == pytest.approx(1.0)
    assert receipt.itd_seconds != 0.0
    assert np.all(np.isfinite(receipt.stereo_rir))
    assert receipt.drr_db > 0
    assert receipt.room_rt60_seconds == pytest.approx(
        compute_room_acoustic_invariants(room).rt60_seconds
    )
    assert not np.allclose(receipt.stereo_rir[:, 0], receipt.stereo_rir[:, 1])


def test_exact_horizon_fft_convolution_preserves_pcm_count(
    room: AcousticRoomSpec,
) -> None:
    rir = synthesize_binaural_room_impulse_response(
        room,
        np.array([3.0, 5.0, 2.0]),
        np.array([4.0, 5.0, 2.0]),
        np.eye(3),
        sample_rate_hz=8_000,
    )
    mono = np.zeros(257)
    mono[0] = 1.0
    receipt = convolve_shot_dialogue_with_room_acoustics(mono, rir.stereo_rir, sample_rate_hz=8_000)
    assert receipt.input_sample_count == 257
    assert receipt.output_sample_count == 257
    assert receipt.sample_drift == 0
    assert receipt.stereo_pcm.shape == (257, 2)
    assert np.any(receipt.stereo_pcm)


def test_cross_shot_rt60_is_invariant_and_drr_tracks_distance(
    room: AcousticRoomSpec,
) -> None:
    speaker = np.array([3.0, 5.0, 2.0])
    shots = tuple(
        (f"camera-{distance}", np.array([3.0 + distance, 5.0, 2.0]), np.eye(3))
        for distance in (1.0, 2.0, 4.0)
    )
    receipt = audit_cross_shot_acoustic_continuity(
        room, speaker, shots, sample_rate_hz=8_000, max_order=2
    )
    assert receipt.rt60_max_spread_seconds == 0.0
    assert receipt.drr_monotonic_with_proximity
    assert len(receipt.shots) == 3
    assert receipt.shots[0].drr_db > receipt.shots[-1].drr_db


def test_room_and_rir_refusal_probes(room: AcousticRoomSpec) -> None:
    with pytest.raises(ValueError):
        compute_room_acoustic_invariants(AcousticRoomSpec("bad", (1.0, 0.0, 1.0), (0.2,) * 6))
    with pytest.raises(ValueError):
        compute_room_acoustic_invariants(AcousticRoomSpec("bad", (1.0, 2.0, 3.0), (0.0,) * 6))
    with pytest.raises(ValueError):
        synthesize_binaural_room_impulse_response(
            room,
            np.array([0.0, 5.0, 2.0]),
            np.array([4.0, 5.0, 2.0]),
            np.eye(3),
        )
    with pytest.raises(ValueError):
        synthesize_binaural_room_impulse_response(
            room,
            np.array([3.0, 5.0, 2.0]),
            np.array([4.0, 5.0, 2.0]),
            np.diag([1.0, 1.0, -1.0]),
        )
    with pytest.raises(ValueError):
        synthesize_binaural_room_impulse_response(
            room,
            np.array([3.0, 5.0, 2.0]),
            np.array([4.0, 5.0, 2.0]),
            np.eye(3),
            sample_rate_hz=0,
        )


def test_physical_room_edge_and_singular_path_refusals(room: AcousticRoomSpec) -> None:
    with pytest.raises(ValueError):
        compute_room_acoustic_invariants(
            AcousticRoomSpec("underflow", (1e-200, 1e-200, 1e-200), (0.2,) * 6)
        )
    with pytest.raises(ValueError):
        synthesize_binaural_room_impulse_response(
            room,
            np.array([3.0, 5.0, 2.0]),
            np.array([0.05, 5.0, 2.0]),
            np.eye(3),
        )
    with pytest.raises(ValueError):
        synthesize_binaural_room_impulse_response(
            room,
            np.array([4.0, 5.0, 2.0]),
            np.array([4.0, 5.0, 2.0]),
            np.eye(3),
        )
    impulse = np.zeros(8)
    with pytest.raises(ValueError):
        acoustic_space_continuity._add_linear_fractional_impulse(impulse, -1.0, 1.0)


def test_cross_shot_continuity_refuses_rt60_spread_and_inverted_drr(
    room: AcousticRoomSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    shots = (
        ("near", np.array([4.0, 5.0, 2.0]), np.eye(3)),
        ("far", np.array([5.0, 5.0, 2.0]), np.eye(3)),
    )
    speaker = np.array([3.0, 5.0, 2.0])

    def fake_rir(*args: object, **kwargs: object) -> BinauralRIRReceipt:
        camera = np.asarray(args[2], dtype=np.float64)
        far = camera[0] > 4.5
        rt60 = 1.0 if far else 0.5
        return BinauralRIRReceipt(
            room.room_id,
            np.zeros((4, 2)),
            27,
            1,
            float(np.linalg.norm(camera - np.asarray(args[1], dtype=np.float64))),
            0.0,
            0.0,
            rt60,
        )

    monkeypatch.setattr(
        acoustic_space_continuity, "synthesize_binaural_room_impulse_response", fake_rir
    )
    with pytest.raises(ValueError, match="RT60"):
        audit_cross_shot_acoustic_continuity(room, speaker, shots)

    def fake_inverted_drr(*args: object, **kwargs: object) -> BinauralRIRReceipt:
        camera = np.asarray(args[2], dtype=np.float64)
        far = camera[0] > 4.5
        return BinauralRIRReceipt(
            room.room_id,
            np.zeros((4, 2)),
            27,
            1,
            float(np.linalg.norm(camera - np.asarray(args[1], dtype=np.float64))),
            0.0,
            4.0 if far else 0.0,
            0.5,
        )

    monkeypatch.setattr(
        acoustic_space_continuity, "synthesize_binaural_room_impulse_response", fake_inverted_drr
    )
    with pytest.raises(ValueError, match="DRR"):
        audit_cross_shot_acoustic_continuity(room, speaker, shots)


def test_convolution_and_cross_shot_refusal_probes(room: AcousticRoomSpec) -> None:
    with pytest.raises(ValueError):
        convolve_shot_dialogue_with_room_acoustics(np.array([]), np.zeros((2, 2)))
    with pytest.raises(ValueError):
        convolve_shot_dialogue_with_room_acoustics(np.ones(4), np.zeros((2, 1)))
    with pytest.raises(ValueError):
        convolve_shot_dialogue_with_room_acoustics(np.ones((4, 2)), np.zeros((2, 2)))
    with pytest.raises(ValueError):
        audit_cross_shot_acoustic_continuity(
            room,
            np.array([3.0, 5.0, 2.0]),
            (("same", np.array([4.0, 5.0, 2.0]), np.eye(3)),) * 2,
        )
    with pytest.raises(ValueError):
        audit_cross_shot_acoustic_continuity(
            room,
            np.array([3.0, 5.0, 2.0]),
            (("only", np.array([4.0, 5.0, 2.0]), np.eye(3)),),
        )
