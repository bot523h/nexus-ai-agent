"""Unit tests for Law 7 — symplectic mechanics and impulse collisions."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.veritas.symplectic_contact_mechanics import (
    MechanicsViolationError,
    kinetic_energy,
    resolve_rigid_body_collision,
    stormer_verlet_step,
    total_angular_momentum,
    total_linear_momentum,
)

POSITIONS = np.array([[0.0, 0.0, 0.0], [1.5, 0.0, 0.0]])
VELOCITIES = np.array([[1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]])
MASSES = np.array([1.0, 2.0])
RADII = np.array([1.0, 1.0])


def test_momentum_and_energy_helpers() -> None:
    momentum = total_linear_momentum(VELOCITIES, MASSES)
    assert np.allclose(momentum, np.array([-1.0, 0.0, 0.0]))
    # Body 1 sits at x=1.5 and moves +y with mass 2 -> L_z = 2 * (1.5 * 1) = 3.
    angular_vel = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    angular = total_angular_momentum(POSITIONS, angular_vel, MASSES)
    assert np.allclose(angular, np.array([0.0, 0.0, 3.0]))
    assert kinetic_energy(VELOCITIES, MASSES) == pytest.approx(1.5)


def test_helper_validation() -> None:
    with pytest.raises(MechanicsViolationError):
        total_linear_momentum(np.zeros((2, 2)), MASSES)
    with pytest.raises(MechanicsViolationError):
        total_angular_momentum(np.zeros((2, 2)), VELOCITIES, MASSES)
    with pytest.raises(MechanicsViolationError):
        kinetic_energy(VELOCITIES, np.array([1.0]))


def test_stormer_verlet_free_fall() -> None:
    positions = np.zeros((1, 3))
    velocities = np.zeros((1, 3))
    masses = np.array([1.0])
    forces = np.array([[0.0, 0.0, -9.80665]])
    new_positions, new_velocities = stormer_verlet_step(positions, velocities, masses, forces, 0.01)
    assert new_positions[0, 2] < 0.0
    assert new_velocities[0, 2] == pytest.approx(-9.80665 * 0.01)


def test_stormer_verlet_validation() -> None:
    with pytest.raises(MechanicsViolationError):
        stormer_verlet_step(np.zeros((2, 2)), np.zeros((2, 2)), MASSES, np.zeros((2, 2)), 0.1)
    with pytest.raises(MechanicsViolationError):
        stormer_verlet_step(np.zeros((2, 3)), np.zeros((2, 3)), MASSES, np.zeros((2, 2)), 0.1)
    with pytest.raises(MechanicsViolationError):
        stormer_verlet_step(np.zeros((2, 3)), np.zeros((2, 3)), MASSES, np.zeros((2, 3)), 0.0)


def test_elastic_collision_conserves_momentum_and_energy() -> None:
    result = resolve_rigid_body_collision(
        POSITIONS, VELOCITIES, MASSES, RADII, restitution=1.0, friction=0.0
    )
    assert np.allclose(result.momentum_after, result.momentum_before, atol=1e-12)
    assert np.allclose(result.angular_momentum_after, result.angular_momentum_before, atol=1e-12)
    assert result.energy_after <= result.energy_before + 1e-12
    assert result.energy_after == pytest.approx(result.energy_before, rel=1e-12)


def test_inelastic_collision_dissipates_energy() -> None:
    result = resolve_rigid_body_collision(
        POSITIONS, VELOCITIES, MASSES, RADII, restitution=0.0, friction=0.5
    )
    assert np.allclose(result.momentum_after, result.momentum_before, atol=1e-12)
    assert result.energy_after < result.energy_before
    assert np.any(np.abs(result.impulses) > 0.0)


def test_collision_validation() -> None:
    with pytest.raises(MechanicsViolationError):
        resolve_rigid_body_collision(POSITIONS, VELOCITIES, np.array([1.0, -1.0]), RADII)
    with pytest.raises(MechanicsViolationError):
        resolve_rigid_body_collision(POSITIONS, VELOCITIES, MASSES, np.array([1.0, 0.0]))
    with pytest.raises(MechanicsViolationError):
        resolve_rigid_body_collision(POSITIONS, VELOCITIES, MASSES, RADII, restitution=1.5)
    with pytest.raises(MechanicsViolationError):
        resolve_rigid_body_collision(POSITIONS, VELOCITIES, MASSES, RADII, friction=-1.0)
    with pytest.raises(MechanicsViolationError):
        resolve_rigid_body_collision(POSITIONS, VELOCITIES, MASSES, RADII, iterations=0)


def test_body_validation_branches() -> None:
    with pytest.raises(MechanicsViolationError):
        total_angular_momentum(np.zeros((2, 3)), np.zeros((3, 3)), np.ones(2))
    with pytest.raises(MechanicsViolationError):
        total_angular_momentum(np.zeros((2, 3)), np.zeros((2, 3)), np.ones(3))
    bad = np.zeros((2, 3))
    bad[0, 0] = np.nan
    with pytest.raises(MechanicsViolationError):
        total_angular_momentum(bad, np.zeros((2, 3)), np.ones(2))


def test_non_overlapping_bodies_are_skipped() -> None:
    positions = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]])
    velocities = np.array([[1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]])
    masses = np.array([1.0, 1.0])
    result = resolve_rigid_body_collision(positions, velocities, masses, np.array([1.0, 1.0]))
    assert np.allclose(result.velocities, velocities)


def test_friction_impulse_on_tangential_contact() -> None:
    positions = np.array([[0.0, 0.0, 0.0], [1.5, 0.0, 0.0]])
    velocities = np.array([[1.0, 1.0, 0.0], [-1.0, -1.0, 0.0]])
    masses = np.array([1.0, 1.0])
    result = resolve_rigid_body_collision(
        positions, velocities, masses, np.array([1.0, 1.0]), restitution=0.5, friction=0.5
    )
    assert np.allclose(result.momentum_after, result.momentum_before, atol=1e-12)
    assert np.any(np.abs(result.impulses) > 0.0)
