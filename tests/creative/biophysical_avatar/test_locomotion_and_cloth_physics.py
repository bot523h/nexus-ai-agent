from __future__ import annotations

import math

import numpy as np
import pytest

from nexus_ai_agent.creative.biophysical_avatar.locomotion_and_cloth_physics import (
    DEFAULT_GARMENT_ARCHIVE,
    ClothMesh,
    FootLock,
    GarmentArchive,
    GarmentCategory,
    GarmentDefinition,
    LocomotionMode,
    ParametricLocomotionManifold,
    VerletClothSolver,
    make_cloth_grid,
    solve_two_bone_ik,
)


def test_two_bone_ik_uses_cosine_law_and_clamps_unreachable_targets() -> None:
    root = (0.0, 0.0, 0.0)
    target = (0.25, 0.0, -0.55)
    result = solve_two_bone_ik(root, target, 0.45, 0.40)
    assert result.reached
    assert np.linalg.norm(np.asarray(result.joint) - root) == pytest.approx(0.45)
    assert np.linalg.norm(np.asarray(result.end) - result.joint) == pytest.approx(0.40)
    assert result.endpoint_error < 1e-9

    far = solve_two_bone_ik(root, (5.0, 0.0, 0.0), 0.4, 0.3)
    assert not far.reached
    assert np.linalg.norm(far.end) == pytest.approx(0.7, abs=1e-9)
    close = solve_two_bone_ik(root, (0.0, 0.0, 0.0), 0.5, 0.2)
    assert not close.reached
    parallel_pole = solve_two_bone_ik(
        root, (0.0, 0.0, -0.5), 0.35, 0.35, pole_direction=(0.0, 0.0, -1.0)
    )
    assert np.isfinite(parallel_pole.joint).all()
    x_axis_pole = solve_two_bone_ik(root, (0.5, 0.0, 0.0), 0.4, 0.4, pole_direction=(1.0, 0.0, 0.0))
    assert np.isfinite(x_axis_pole.joint).all()
    with pytest.raises(ValueError, match="bone lengths"):
        solve_two_bone_ik(root, target, 0.0, 0.4)
    with pytest.raises(ValueError, match="finite 3-vector"):
        solve_two_bone_ik(root, (1.0, 2.0), 0.4, 0.4)


def test_foot_lock_holds_world_contact_until_swing_and_reset() -> None:
    lock = FootLock()
    touchdown = (1.0, 2.0, 0.0)
    assert (
        lock.update(
            in_stance=True,
            touchdown_position=touchdown,
            swing_position=(9.0, 0.0, 1.0),
        )
        == touchdown
    )
    for index in range(1, 11):
        contact = lock.update(
            in_stance=True,
            touchdown_position=(1.0 + index, 2.0, 0.0),
            swing_position=(0.0, 0.0, 0.0),
        )
        assert contact == touchdown
        assert lock.anchor == touchdown
    assert lock.update(
        in_stance=False,
        touchdown_position=(0.0, 0.0, 0.0),
        swing_position=(0.5, 0.0, 0.1),
    ) == (0.5, 0.0, 0.1)
    assert lock.anchor is None
    lock.reset()
    assert lock.anchor is None
    with pytest.raises(ValueError, match="3-vector"):
        lock.update(in_stance=True, touchdown_position=(0.0,), swing_position=(0.0, 0.0, 0.0))
    with pytest.raises(ValueError, match="3-vector"):
        lock.update(
            in_stance=True,
            touchdown_position="bad",  # type: ignore[arg-type]
            swing_position=(0.0, 0.0, 0.0),
        )
    with pytest.raises(ValueError, match="3-vector"):
        lock.update(
            in_stance=True,
            touchdown_position=(object(), 0.0, 0.0),  # type: ignore[arg-type]
            swing_position=(0.0, 0.0, 0.0),
        )


def test_locomotion_archive_covers_idle_gait_turn_sit_jump_and_action_modes() -> None:
    manifold = ParametricLocomotionManifold()
    for mode in LocomotionMode:
        sample = manifold.sample(mode, 0.25)
        assert sample.mode is mode
        assert len(sample.left_foot) == 3
        assert len(sample.right_knee) == 3
        assert np.isfinite(sample.root_position).all()
        assert np.isfinite(sample.left_foot).all()
    standing = manifold.sample("standing_breathing", 0.25)
    assert abs(standing.body_bob_m) <= 0.004
    assert manifold.sample(LocomotionMode.WALK, 0.0).left_in_stance
    assert manifold.sample(LocomotionMode.SPRINT, 0.0).left_in_stance
    turn = manifold.sample(LocomotionMode.TURN_360, 2.0, heading_radians=0.3)
    assert turn.heading_radians == pytest.approx(0.3 + math.tau / 2.0)
    assert manifold.sample(LocomotionMode.JUMP, 0.6).root_position[2] > 0.92
    assert manifold.sample(LocomotionMode.SIT, 1.0).root_position[2] == pytest.approx(0.56)
    assert manifold.sample(LocomotionMode.RISE, 1.0).root_position[2] == pytest.approx(0.92)
    assert manifold.sample(LocomotionMode.DODGE, 0.275).root_position[1] > 0.0
    assert manifold.sample(LocomotionMode.COMBAT, 0.4, speed=1.4).root_position[0] > 0.0


def test_gait_support_phase_has_zero_millimetre_foot_skate() -> None:
    manifold = ParametricLocomotionManifold(stride_length=1.4)
    contacts = [
        manifold.sample(LocomotionMode.WALK, time).left_foot for time in (0.04, 0.06, 0.08, 0.10)
    ]
    assert all(contact == contacts[0] for contact in contacts)
    assert np.linalg.norm(np.asarray(contacts[-1]) - contacts[0]) == 0.0
    swing = manifold.sample(LocomotionMode.WALK, 0.5)
    assert not swing.left_in_stance
    custom = manifold.sample(LocomotionMode.WALK, 1.0, speed=2.0, heading_radians=1.2)
    assert custom.root_position[1] > 0.0
    slow = manifold.sample(LocomotionMode.SLOW_MOTION, 0.5)
    assert slow.root_position[0] < manifold.sample(LocomotionMode.WALK, 0.5).root_position[0]
    manifold.reset()
    assert manifold.sample(LocomotionMode.MARCH, 0.2).mode is LocomotionMode.MARCH
    with pytest.raises(ValueError, match="unsupported"):
        manifold.sample("fly", 0.0)
    with pytest.raises(ValueError, match="non-negative"):
        manifold.sample(LocomotionMode.WALK, -0.1)
    with pytest.raises(ValueError, match="speed"):
        manifold.sample(LocomotionMode.WALK, 0.0, speed=float("nan"))
    with pytest.raises(ValueError, match="stride"):
        ParametricLocomotionManifold(stride_length=0.0)
    with pytest.raises(ValueError, match="leg_lengths"):
        ParametricLocomotionManifold(leg_lengths=(0.4,))  # type: ignore[arg-type]


def test_garment_archive_has_multicultural_and_scifi_parametric_presets() -> None:
    archive = DEFAULT_GARMENT_ARCHIVE
    assert "qabaye_shahnameh" in archive.names
    assert "iranian_traditional" in archive.names
    assert "regional_balochi" in archive.names
    assert "formal_suit" in archive.names
    assert "modern_coat" in archive.names
    assert "tactical_armor" in archive.names
    assert "sci_fi_cloak" in archive.names
    assert archive.get("orbital_suit").category is GarmentCategory.SCI_FI
    assert archive.by_category("formal")
    assert archive.by_category(GarmentCategory.HISTORICAL)[0].name == "qabaye_shahnameh"
    mesh = archive.mesh("action_cape", columns=5, rows=6)
    assert mesh.vertices.shape == (30, 3)
    assert len(mesh.pinned_indices) == 5
    with pytest.raises(KeyError, match="unknown garment"):
        archive.get("not-a-garment")
    with pytest.raises(ValueError, match="category"):
        archive.by_category("unknown")
    with pytest.raises(ValueError, match="must not be empty"):
        GarmentArchive(())
    duplicate = GarmentDefinition("same", GarmentCategory.MODERN, 0.5, 0.8, 0.1, 0.1, 2, (1, 2, 3))
    with pytest.raises(ValueError, match="unique"):
        GarmentArchive((duplicate, duplicate))
    with pytest.raises(ValueError, match="dimensions"):
        GarmentDefinition("bad", GarmentCategory.MODERN, 0.0, 0.8, 0.1, 0.1, 2, (1, 2, 3))
    with pytest.raises(ValueError, match="damping"):
        GarmentDefinition("bad", GarmentCategory.MODERN, 0.5, 0.8, 1.0, 0.1, 2, (1, 2, 3))
    with pytest.raises(ValueError, match="iterations"):
        GarmentDefinition("bad", GarmentCategory.MODERN, 0.5, 0.8, 0.1, 0.1, 0, (1, 2, 3))
    with pytest.raises(ValueError, match="color_rgb"):
        GarmentDefinition("bad", GarmentCategory.MODERN, 0.5, 0.8, 0.1, 0.1, 2, (300, 0, 0))
    with pytest.raises(ValueError, match="dimensions"):
        GarmentDefinition("bad", GarmentCategory.MODERN, float("nan"), 0.8, 0.1, 0.1, 2, (1, 2, 3))
    with pytest.raises(ValueError, match="damping"):
        GarmentDefinition("bad", GarmentCategory.MODERN, 0.5, 0.8, float("nan"), 0.1, 2, (1, 2, 3))
    with pytest.raises(ValueError, match="damping"):
        GarmentDefinition("bad", GarmentCategory.MODERN, 0.5, 0.8, 0.1, float("nan"), 2, (1, 2, 3))
    with pytest.raises(ValueError, match="color_rgb"):
        GarmentDefinition("bad", GarmentCategory.MODERN, 0.5, 0.8, 0.1, 0.1, 2, (1.5, 2, 3))  # type: ignore[arg-type]


def test_cloth_grid_generates_structural_shear_constraints_and_triangles() -> None:
    mesh = make_cloth_grid(1.0, 1.5, columns=4, rows=5, origin=(0.0, 0.0, 2.0))
    assert mesh.vertices.shape == (20, 3)
    assert len(mesh.triangles) == 24
    assert len(mesh.pinned_indices) == 4
    assert len(mesh.constraints) > 20
    assert not mesh.vertices.flags.writeable
    assert mesh.vertices[0, 2] == pytest.approx(2.0)
    assert mesh.vertices[-1, 2] == pytest.approx(0.5)
    with pytest.raises(ValueError, match="positive"):
        make_cloth_grid(0.0, 1.0)
    with pytest.raises(ValueError, match="at least"):
        make_cloth_grid(1.0, 1.0, columns=1)
    with pytest.raises(ValueError, match="3-vector"):
        make_cloth_grid(1.0, 1.0, origin=(0.0, 0.0))
    with pytest.raises(ValueError, match="vertices"):
        ClothMesh(np.zeros((3, 3)), (), (), (), 2, 2)
    with pytest.raises(ValueError, match="triangle index"):
        ClothMesh(np.zeros((4, 3)), ((0, 1, 5),), (), (), 2, 2)
    with pytest.raises(ValueError, match="rest length"):
        ClothMesh(np.zeros((4, 3)), (), ((0, 1, 0.0),), (), 2, 2)
    with pytest.raises(ValueError, match="finite"):
        ClothMesh(np.full((4, 3), np.nan), (), (), (), 2, 2)
    with pytest.raises(ValueError, match="triangle index"):
        ClothMesh(np.zeros((4, 3)), ((0, 1),), (), (), 2, 2)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="constraint index"):
        ClothMesh(np.zeros((4, 3)), (), ((0, 0, 1.0),), (), 2, 2)
    with pytest.raises(ValueError, match="pinned vertex"):
        ClothMesh(np.zeros((4, 3)), (), (), (4,), 2, 2)
    with pytest.raises(ValueError, match="grid dimensions"):
        ClothMesh(np.zeros((4, 3)), (), (), (), 3, 2)


def test_verlet_solver_is_stable_in_strong_wind_and_keeps_pins_and_threads() -> None:
    mesh = make_cloth_grid(0.8, 1.2, columns=5, rows=6)
    garment = DEFAULT_GARMENT_ARCHIVE.get("sci_fi_cloak")
    solver = VerletClothSolver.from_mesh(mesh, garment)
    pinned_before = solver.positions[list(mesh.pinned_indices)].copy()
    for _ in range(20):
        positions = solver.step(1.0 / 60.0, wind=(900.0, -500.0, 300.0))
        assert np.isfinite(positions).all()
    assert np.allclose(solver.positions[list(mesh.pinned_indices)], pinned_before, atol=0.0)
    assert solver.max_constraint_error < 0.03
    assert np.isfinite(solver.previous_positions).all()
    caller_copy = solver.positions
    caller_copy[:] = 0.0
    assert not np.array_equal(caller_copy, solver.positions)


def test_verlet_solver_validation_empty_constraints_and_zero_length_repair() -> None:
    solver = VerletClothSolver(((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)), ((0, 1, 1.0),), iterations=2)
    solver.step(0.02, wind=(0.0, 0.0, 0.0))
    assert np.linalg.norm(solver.positions[1] - solver.positions[0]) == pytest.approx(1.0)
    no_constraints = VerletClothSolver(((0.0, 0.0, 0.0),), ())
    assert no_constraints.max_constraint_error == 0.0
    with pytest.raises(ValueError, match="positions"):
        VerletClothSolver(((0.0, 0.0),), ())
    with pytest.raises(ValueError, match="finite"):
        VerletClothSolver(((float("nan"), 0.0, 0.0),), ())
    with pytest.raises(ValueError, match="damping"):
        VerletClothSolver(((0.0, 0.0, 0.0),), (), damping=1.0)
    with pytest.raises(ValueError, match="wind_drag"):
        VerletClothSolver(((0.0, 0.0, 0.0),), (), wind_drag=-1.0)
    with pytest.raises(ValueError, match="iterations"):
        VerletClothSolver(((0.0, 0.0, 0.0),), (), iterations=0)
    with pytest.raises(ValueError, match="fixed vertex"):
        VerletClothSolver(((0.0, 0.0, 0.0),), (), fixed_indices=(2,))
    with pytest.raises(ValueError, match="constraint index"):
        VerletClothSolver(((0.0, 0.0, 0.0),), ((0, 3, 1.0),))
    with pytest.raises(ValueError, match="rest length"):
        VerletClothSolver(((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)), ((0, 1, 0.0),))
    with pytest.raises(ValueError, match="dt"):
        no_constraints.step(0.0)
    with pytest.raises(ValueError, match="dt"):
        no_constraints.step(0.11)
    with pytest.raises(ValueError, match="3-vector"):
        no_constraints.step(0.01, wind=(1.0, 2.0))
