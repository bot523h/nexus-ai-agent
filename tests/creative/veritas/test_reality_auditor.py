"""Unit tests for the unified VeritasRealityAuditor and its passport."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas.hand_kinematics import (
    HandSkeleton,
    canonical_hand_skeleton,
)
from nexus_ai_agent.creative.veritas.reality_auditor import (
    LAW_NAMES,
    CinemaShot,
    VeritasRealityAuditor,
)
from nexus_ai_agent.creative.veritas.se3_object_permanence import ObjectState
from nexus_ai_agent.creative.veritas.thermodynamic_arrow_of_time import retarded_wavefront


def _broken_hand() -> HandSkeleton:
    base = canonical_hand_skeleton()
    angles = np.array(base.joint_angles, copy=True)
    angles[5, 0] = 200.0  # MCP flexion far beyond its cone
    return HandSkeleton(base.joints, base.rays, angles, base.bone_lengths)


def _entropy_reversal() -> np.ndarray:
    frames = np.zeros((3, 6, 6))
    frames[0] = 1.0
    frames[1, 0, 0] = 1.0
    frames[2, 0, 0] = 1.0
    return frames


def _facs_violation() -> np.ndarray:
    au = np.zeros((2, 26))
    au[:, 11] = 1.0  # AU12 zygomaticus fully active
    au[:, 5] = 0.0  # AU6 orbicularis absent -> Duchenne violation
    return au


def test_law_names_cover_ten_laws() -> None:
    assert set(LAW_NAMES) == set(range(1, 16))


def test_empty_shot_is_not_healed() -> None:
    passport = VeritasRealityAuditor().audit_and_heal_cinema_shot(CinemaShot())
    assert passport.pre_audit_violations == 0
    assert passport.post_heal_violations == 0
    assert not passport.healed


def test_multi_law_shot_is_healed_and_hashed() -> None:
    shot = CinemaShot(
        hand=_broken_hand(),
        eyes=(
            np.array([-31.5, 0.0, 0.0]),
            np.array([31.5, 0.0, 0.0]),
            np.array([0.0, 0.0, 500.0]),
        ),
        scalar_frames=_entropy_reversal(),
        facs=_facs_violation(),
    )
    passport = VeritasRealityAuditor().audit_and_heal_cinema_shot(shot)
    assert passport.pre_audit_violations > 0
    assert passport.post_heal_violations == 0
    assert passport.healed
    assert passport.law_violations_before[1] > 0
    assert passport.law_violations_after[1] == 0
    assert len(passport.digest) == 64
    assert all(character in "0123456789abcdef" for character in passport.digest)


def test_digest_is_deterministic() -> None:
    shot = CinemaShot(hand=_broken_hand())
    first = VeritasRealityAuditor().audit_and_heal_cinema_shot(shot)
    second = VeritasRealityAuditor().audit_and_heal_cinema_shot(shot)
    assert first.digest == second.digest


def test_passport_as_dict_is_json_safe() -> None:
    passport = VeritasRealityAuditor().audit_and_heal_cinema_shot(CinemaShot(hand=_broken_hand()))
    payload = passport.as_dict()
    assert payload["pre_audit_violations"] == passport.pre_audit_violations
    assert set(payload["law_violations_before"]) == {str(law) for law in range(1, 16)}


def test_clean_hand_has_no_violations() -> None:
    passport = VeritasRealityAuditor().audit_and_heal_cinema_shot(
        CinemaShot(hand=canonical_hand_skeleton())
    )
    assert passport.pre_audit_violations == 0
    assert passport.post_heal_violations == 0


def test_law_subset_restricts_audit() -> None:
    auditor = VeritasRealityAuditor(laws=(1,))
    passport = auditor.audit_and_heal_cinema_shot(
        CinemaShot(hand=_broken_hand(), scalar_frames=_entropy_reversal())
    )
    assert set(passport.law_violations_before) == {1}
    assert passport.pre_audit_violations > 0
    assert passport.post_heal_violations == 0


def _cloth_grid(rows: int, cols: int, z: float) -> tuple[np.ndarray, np.ndarray]:
    ys, xs = np.meshgrid(np.arange(rows), np.arange(cols), indexing="ij")
    pos = np.stack([xs, ys, np.full_like(xs, z, dtype=np.float64)], axis=-1).reshape(-1, 3)
    edges = []
    for r in range(rows):
        for c in range(cols):
            i = r * cols + c
            if c + 1 < cols:
                edges.append((i, i + 1))
            if r + 1 < rows:
                edges.append((i, i + cols))
    return pos.astype(np.float64), np.asarray(edges, dtype=np.float64)


def _clean_full_shot() -> CinemaShot:
    cloth_pos, cloth_edges = _cloth_grid(3, 3, 2.0)
    wave = retarded_wavefront((10, 10), (4.0, 4.0), 1.0, 1.5, frames=5)
    return CinemaShot(
        hand=canonical_hand_skeleton(),
        eyes=(
            np.array([-31.5, 0.0, 0.0]),
            np.array([31.5, 0.0, 0.0]),
            np.array([0.0, 0.0, 500.0]),
        ),
        saccade=(10.0, 0.1, 0.0),
        pupil=(0.9, 20.0, 30.0, 1.0),
        facs=np.zeros((2, 26)),
        blink=(0.4, 0.0),
        skin=(np.zeros((8, 8, 3)), np.zeros((8, 8, 3)), np.ones((8, 8))),
        mirror=(
            np.array([0.0, 0.0, 1.0]),
            0.0,
            np.array([0.0, 0.0, 2.0]),
            np.array([1.0, 1.0, 1.0]),
        ),
        fresnel=(45.0, 1.0, 1.5),
        shadow=(
            np.array([[0.5, 0.0, 1.0]]),
            np.array([0.0, 0.0, 3.0, 1.0]),
            np.array([0.0, 0.0, 1.0, 0.0]),
        ),
        typography=(
            "HELLO",
            np.array([[10.0, 10.0], [90.0, 20.0], [80.0, 80.0], [20.0, 90.0]]),
            (100, 100),
        ),
        objects=(
            ObjectState(np.eye(4), np.zeros(6), np.ones(3), 1.0),
            ObjectState(np.eye(4), np.zeros(6), np.ones(3), 1.0),
        ),
        collision=(
            np.array([[0.0, 0.0, 0.0], [1.5, 0.0, 0.0]]),
            np.array([[1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]]),
            np.array([1.0, 2.0]),
            np.array([1.0, 1.0]),
            0.5,
            0.2,
        ),
        cloth=(cloth_pos, np.zeros_like(cloth_pos), cloth_edges, []),
        scalar_frames=np.ones((2, 4, 4)),
        wave=(wave, (4.0, 4.0), 1.0, 1.5),
    )


def test_full_clean_shot_dispatches_every_law() -> None:
    passport = VeritasRealityAuditor().audit_and_heal_cinema_shot(_clean_full_shot())
    assert passport.post_heal_violations == 0
    assert set(passport.law_violations_before) == set(range(1, 16))


def test_violating_shot_exercises_healing_paths() -> None:
    cloth_pos, cloth_edges = _cloth_grid(3, 3, 0.0)
    cloth_pos[:, 0] -= 1.0
    capsule = (np.array([-2.0, 0.0, 0.0]), np.array([2.0, 0.0, 0.0]), 0.3)
    wave = retarded_wavefront((10, 10), (4.0, 4.0), 1.0, 1.5, frames=5)
    wave[0, 0, 0] = 5.0  # acausal
    teleported = np.eye(4)
    teleported[:3, 3] = [100.0, 0.0, 0.0]
    shot = CinemaShot(
        hand=_broken_hand(),
        eyes=(
            np.array([-31.5, 0.0, 0.0]),
            np.array([31.5, 0.0, 0.0]),
            np.array([0.0, 0.0, 500.0]),
        ),
        saccade=(0.0, 0.1, 0.0),  # static saccade
        facs=_facs_violation(),
        objects=(
            ObjectState(np.eye(4), np.zeros(6), np.ones(3), 1.0),
            ObjectState(teleported, np.zeros(6), np.ones(3), 1.0),
        ),
        cloth=(cloth_pos, np.zeros_like(cloth_pos), cloth_edges, [capsule]),
        scalar_frames=_entropy_reversal(),
        wave=(wave, (4.0, 4.0), 1.0, 1.5),
    )
    passport = VeritasRealityAuditor().audit_and_heal_cinema_shot(shot)
    assert passport.pre_audit_violations > 0
    assert passport.post_heal_violations == 0
    assert passport.healed


def test_auditor_records_every_violation_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """The auditor must faithfully aggregate each law's violation strings."""
    import types

    import nexus_ai_agent.creative.veritas.reality_auditor as ra

    monkeypatch.setattr(
        ra,
        "solve_binocular_vergence",
        lambda *a, **k: types.SimpleNamespace(violations_before=(), is_consistent=False),
    )
    monkeypatch.setattr(
        ra,
        "compute_watson_yellott_pupil_diameter",
        lambda *a, **k: types.SimpleNamespace(steady_state_mm=10.0),
    )
    monkeypatch.setattr(
        ra,
        "verify_reflection_epipolar_consistency",
        lambda *a, **k: types.SimpleNamespace(consistent=False),
    )
    monkeypatch.setattr(
        ra,
        "compute_fresnel_reflectance_rgb",
        lambda *a, **k: types.SimpleNamespace(brewster_deg=0.0, reflectance_p=np.array([1.0])),
    )
    monkeypatch.setattr(
        ra,
        "synthesize_coherent_cast_shadows",
        lambda *a, **k: types.SimpleNamespace(
            shadow_vertices=np.array([[0.0, 0.0, 1.0]]), penumbra_radius=np.array([-1.0])
        ),
    )
    monkeypatch.setattr(
        ra,
        "render_homography_locked_diegetic_text",
        lambda *a, **k: types.SimpleNamespace(homography=np.eye(3) * 2.0, mask=np.zeros((4, 4))),
    )
    monkeypatch.setattr(
        ra,
        "resolve_rigid_body_collision",
        lambda *a, **k: types.SimpleNamespace(
            velocities=np.zeros((1, 3)),
            momentum_before=np.zeros(3),
            momentum_after=np.array([1.0, 0.0, 0.0]),
            energy_before=1.0,
            energy_after=2.0,
        ),
    )
    monkeypatch.setattr(
        ra,
        "step_xpbd_cloth_simulation",
        lambda *a, **k: types.SimpleNamespace(min_sdf=-1.0),
    )
    monkeypatch.setattr(
        ra,
        "synthesize_asymmetric_blink_curve",
        lambda *a, **k: types.SimpleNamespace(peak_closure=1.5),
    )
    monkeypatch.setattr(
        ra,
        "apply_dermal_subsurface_scattering",
        lambda *a, **k: np.zeros((2, 2, 3)),
    )
    shot = CinemaShot(
        eyes=(np.zeros(3), np.ones(3), np.zeros(3)),
        pupil=(1.0, 1.0, 1.0, 1.0),
        mirror=(np.array([0.0, 0.0, 1.0]), 0.0, np.zeros(3), np.ones(3)),
        fresnel=(45.0, 1.0, 1.5),
        shadow=(np.zeros((1, 3)), np.array([0.0, 0.0, 1.0, 1.0]), np.array([0.0, 0.0, 1.0, 0.0])),
        typography=("HI", np.zeros((4, 2)), (10, 10)),
        collision=(np.zeros((1, 3)), np.zeros((1, 3)), np.ones(1), np.ones(1), 0.5, 0.2),
        cloth=(np.zeros((1, 3)), np.zeros((1, 3)), np.zeros((0, 2)), []),
        blink=(0.4, 0.0),
        skin=(np.zeros((4, 4, 3)), np.zeros((4, 4, 3)), np.ones((4, 4))),
    )
    passport = VeritasRealityAuditor().audit_and_heal_cinema_shot(shot)
    flat = {name for names in passport.details_before.values() for name in names}
    for expected in (
        "gaze_projected",
        "pupil_out_of_range",
        "reflection_inconsistent",
        "brewster_violation",
        "floating_shadow",
        "negative_penumbra",
        "homography_not_sl3",
        "text_invisible",
        "momentum_nonconservation",
        "energy_creation",
        "cloth_penetration",
        "blink_overclosure",
        "sss_shape",
    ):
        assert expected in flat, expected

    # A permanently broken validation kernel must remain visible after healing.
    assert "pupil_out_of_range" in passport.details_after[2]
    assert "reflection_inconsistent" in passport.details_after[3]
    assert "floating_shadow" in passport.details_after[4]
    assert "homography_not_sl3" in passport.details_after[6]
    assert "momentum_nonconservation" in passport.details_after[7]
    assert "blink_overclosure" in passport.details_after[10]
    assert "sss_shape" in passport.details_after[10]
