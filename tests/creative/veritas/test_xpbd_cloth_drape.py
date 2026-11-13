"""Unit tests for Law 8 — XPBD cloth drape and capsule collision."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas.xpbd_cloth_drape import (
    ClothSimulationError,
    capsule_sdf,
    step_xpbd_cloth_simulation,
)


def _grid(rows: int, cols: int, z: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    ys, xs = np.meshgrid(np.arange(rows), np.arange(cols), indexing="ij")
    positions = np.stack([xs, ys, np.full_like(xs, z, dtype=np.float64)], axis=-1)
    positions = positions.reshape(-1, 3).astype(np.float64)
    edges = []
    for r in range(rows):
        for c in range(cols):
            index = r * cols + c
            if c + 1 < cols:
                edges.append((index, index + 1))
            if r + 1 < rows:
                edges.append((index, index + cols))
    return positions, np.asarray(edges, dtype=np.float64)


def test_capsule_sdf_is_exact() -> None:
    start = np.array([0.0, 0.0, 0.0])
    end = np.array([0.0, 0.0, 1.0])
    points = np.array(
        [
            [0.5, 0.0, 0.5],  # on the surface
            [1.0, 0.0, 0.5],  # 0.5 outside
            [0.0, 0.0, 0.5],  # 0.5 inside
            [0.0, 0.0, 2.0],  # beyond the cap
        ]
    )
    sdf = capsule_sdf(points, start, end, 0.5)
    assert np.allclose(sdf, np.array([0.0, 0.5, -0.5, 0.5]))


def test_capsule_sdf_validation() -> None:
    with pytest.raises(ClothSimulationError):
        capsule_sdf(np.zeros((2, 2)), np.zeros(3), np.ones(3), 0.5)
    with pytest.raises(ClothSimulationError):
        capsule_sdf(np.zeros((2, 3)), np.zeros(2), np.ones(3), 0.5)
    with pytest.raises(ClothSimulationError):
        capsule_sdf(np.zeros((2, 3)), np.zeros(3), np.ones(3), 0.0)
    with pytest.raises(ClothSimulationError):
        capsule_sdf(np.zeros((2, 3)), np.zeros(3), np.zeros(3), 0.5)


def test_gravity_drapes_cloth_downward() -> None:
    positions, edges = _grid(3, 3, z=2.0)
    velocities = np.zeros_like(positions)
    result = step_xpbd_cloth_simulation(positions, velocities, edges)
    assert result.positions.shape == positions.shape
    assert np.all(result.positions[:, 2] < positions[:, 2])
    assert result.max_stretch >= 0.0


def test_capsule_collision_has_zero_penetration() -> None:
    # Cloth lying on the capsule axis penetrates; one step must push it out.
    positions, edges = _grid(3, 3, z=0.0)
    positions[:, 0] -= 1.0
    velocities = np.zeros_like(positions)
    capsule = (np.array([-2.0, 0.0, 0.0]), np.array([2.0, 0.0, 0.0]), 0.3)
    result = step_xpbd_cloth_simulation(
        positions, velocities, edges, capsules=[capsule], iterations=8
    )
    assert result.min_sdf >= -1e-6
    assert result.max_stretch < 1.0


def test_cloth_validation_errors() -> None:
    positions, edges = _grid(2, 2)
    velocities = np.zeros_like(positions)
    with pytest.raises(ClothSimulationError):
        step_xpbd_cloth_simulation(np.zeros((2, 2)), np.zeros((2, 2)), edges)
    with pytest.raises(ClothSimulationError):
        step_xpbd_cloth_simulation(positions, np.zeros((3, 3)), edges)
    bad = np.array(positions, copy=True)
    bad[0, 0] = np.nan
    with pytest.raises(ClothSimulationError):
        step_xpbd_cloth_simulation(bad, velocities, edges)
    with pytest.raises(ClothSimulationError):
        step_xpbd_cloth_simulation(positions, velocities, edges, dt=0.0)
    with pytest.raises(ClothSimulationError):
        step_xpbd_cloth_simulation(positions, velocities, edges, compliance=-1.0)
    with pytest.raises(ClothSimulationError):
        step_xpbd_cloth_simulation(positions, velocities, edges, iterations=0)
    with pytest.raises(ClothSimulationError):
        step_xpbd_cloth_simulation(positions, velocities, np.array([[0.0, 99.0]]))
    with pytest.raises(ClothSimulationError):
        step_xpbd_cloth_simulation(positions, velocities, np.array([[0.0, 0.0]]))
    with pytest.raises(ClothSimulationError):
        step_xpbd_cloth_simulation(positions, velocities, edges, gravity=(0.0, 0.0))
    with pytest.raises(ClothSimulationError):
        step_xpbd_cloth_simulation(
            positions, velocities, edges, rest_lengths=np.zeros(edges.shape[0])
        )
