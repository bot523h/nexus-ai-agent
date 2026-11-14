"""WAVE 27 — the PROMETHEUS-ATLAS real source-code mutation campaign (M001-M120).

This is a **mutation** campaign, not an adversarial negative-input test. For each
entry in :data:`MUTANTS` a single, real source change is injected into an isolated
temporary copy of the package; the targeted test file is then run in a subprocess.
A mutant counts as KILLED only when a relevant test actually fails because of the
injected defect (pytest exit code 1, ``FAILED <nodeid>`` present, no collection
crash). The campaign asserts every mutant is killed, that every mutation anchor is
unique in the real tree, and that there are exactly 120.

Together with the 80 TITAN/OLYMPUS mutants in
``tests/creative/atlas/test_titan_olympus_mutations.py`` this brings the
repository's causal-mutation total to **200/200 killed**.

The temporary copy is created under ``tmp_path``; the real repository source is
never modified. This is a REAL code-mutation experiment (REAL COMPUTE): each mutant
is compiled and imported by a real subprocess that runs the targeted test file, so
a kill means a real behavioural assertion failed.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src"
TESTS = REPO_ROOT / "tests"

#: ``(relative source path, original text, mutated text, targeted test file)``.
#: Each ``original`` must occur exactly once in the real tree.
MUTANTS: tuple[tuple[str, str, str, str], ...] = (
    (
        "nexus_ai_agent/creative/atlas/port_hamiltonian_world_solver.py",
        'raise PortHamiltonianError(f"{name} must be finite")',
        "pass  # MUT",
        "tests/creative/atlas/test_port_hamiltonian_world_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/port_hamiltonian_world_solver.py",
        'raise PortHamiltonianError(f"dimension must be an int in [1, {MAX_DIM}]")',
        "pass  # MUT",
        "tests/creative/atlas/test_port_hamiltonian_world_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/port_hamiltonian_world_solver.py",
        'raise PortHamiltonianError("Q must be an (n, n) matrix")',
        "pass  # MUT",
        "tests/creative/atlas/test_port_hamiltonian_world_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/port_hamiltonian_world_solver.py",
        'raise PortHamiltonianError("Q must be finite")',
        "pass  # MUT",
        "tests/creative/atlas/test_port_hamiltonian_world_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/port_hamiltonian_world_solver.py",
        'raise PortHamiltonianError("Q must be symmetric")',
        "pass  # MUT",
        "tests/creative/atlas/test_port_hamiltonian_world_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/port_hamiltonian_world_solver.py",
        'raise PortHamiltonianError("Q must be positive definite (coercive energy)")',
        "pass  # MUT",
        "tests/creative/atlas/test_port_hamiltonian_world_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/port_hamiltonian_world_solver.py",
        'raise PortHamiltonianError("nonlinear coefficient must be finite and >= 0")',
        "pass  # MUT",
        "tests/creative/atlas/test_port_hamiltonian_world_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/port_hamiltonian_world_solver.py",
        'raise PortHamiltonianError("G must be an (n, m) matrix")',
        "pass  # MUT",
        "tests/creative/atlas/test_port_hamiltonian_world_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/port_hamiltonian_world_solver.py",
        'raise PortHamiltonianError("G must be finite")',
        "pass  # MUT",
        "tests/creative/atlas/test_port_hamiltonian_world_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/port_hamiltonian_world_solver.py",
        'raise PortHamiltonianError("J(x) must be finite")',
        "pass  # MUT",
        "tests/creative/atlas/test_port_hamiltonian_world_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/port_hamiltonian_world_solver.py",
        'raise PortHamiltonianError("R(x) must be symmetric")',
        "pass  # MUT",
        "tests/creative/atlas/test_port_hamiltonian_world_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/port_hamiltonian_world_solver.py",
        'raise PortHamiltonianError("R(x) is not positive semi-definite")',
        "pass  # MUT",
        "tests/creative/atlas/test_port_hamiltonian_world_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/port_hamiltonian_world_solver.py",
        'raise PortHamiltonianError("state vectors must have the system dimension")',
        "pass  # MUT",
        "tests/creative/atlas/test_port_hamiltonian_world_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/port_hamiltonian_world_solver.py",
        'raise PortHamiltonianError("x_k must have the system dimension")',
        "pass  # MUT",
        "tests/creative/atlas/test_port_hamiltonian_world_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/port_hamiltonian_world_solver.py",
        'raise PortHamiltonianError("dt must be a positive finite number")',
        "pass  # MUT",
        "tests/creative/atlas/test_port_hamiltonian_world_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/port_hamiltonian_world_solver.py",
        'raise PortHamiltonianError("tolerance must be positive")',
        "pass  # MUT",
        "tests/creative/atlas/test_port_hamiltonian_world_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/wasserstein_schrodinger_bridge.py",
        'raise TransportError(f"{name} must be finite")',
        "pass  # MUT",
        "tests/creative/atlas/test_wasserstein_schrodinger_bridge.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/wasserstein_schrodinger_bridge.py",
        'raise TransportError(f"{name} must be non-negative")',
        "pass  # MUT",
        "tests/creative/atlas/test_wasserstein_schrodinger_bridge.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/wasserstein_schrodinger_bridge.py",
        'raise TransportError(f"{name} must be a normalised simplex point (sum={total:.6f})")',
        "pass  # MUT",
        "tests/creative/atlas/test_wasserstein_schrodinger_bridge.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/wasserstein_schrodinger_bridge.py",
        'raise TransportError(f"cost must have shape ({n}, {m})")',
        "pass  # MUT",
        "tests/creative/atlas/test_wasserstein_schrodinger_bridge.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/wasserstein_schrodinger_bridge.py",
        'raise TransportError("cost must be finite")',
        "pass  # MUT",
        "tests/creative/atlas/test_wasserstein_schrodinger_bridge.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/wasserstein_schrodinger_bridge.py",
        'raise TransportError("cost must be non-negative")',
        "pass  # MUT",
        "tests/creative/atlas/test_wasserstein_schrodinger_bridge.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/wasserstein_schrodinger_bridge.py",
        'raise TransportError(f"eps must be in [{MIN_EPS}, {MAX_EPS}]")',
        "pass  # MUT",
        "tests/creative/atlas/test_wasserstein_schrodinger_bridge.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/wasserstein_schrodinger_bridge.py",
        'raise TransportError("t must be in [0, 1]")',
        "pass  # MUT",
        "tests/creative/atlas/test_wasserstein_schrodinger_bridge.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/wasserstein_schrodinger_bridge.py",
        'raise TransportError("distributions must be a non-empty tuple")',
        "pass  # MUT",
        "tests/creative/atlas/test_wasserstein_schrodinger_bridge.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/wasserstein_schrodinger_bridge.py",
        'raise TransportError("all distributions must share the same support size")',
        "pass  # MUT",
        "tests/creative/atlas/test_wasserstein_schrodinger_bridge.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/wasserstein_schrodinger_bridge.py",
        'raise TransportError("weights must have one entry per distribution")',
        "pass  # MUT",
        "tests/creative/atlas/test_wasserstein_schrodinger_bridge.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/wasserstein_schrodinger_bridge.py",
        'raise TransportError("weights must be finite and non-negative")',
        "pass  # MUT",
        "tests/creative/atlas/test_wasserstein_schrodinger_bridge.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/wasserstein_schrodinger_bridge.py",
        'raise TransportError("weights must sum to one")',
        "pass  # MUT",
        "tests/creative/atlas/test_wasserstein_schrodinger_bridge.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/wasserstein_schrodinger_bridge.py",
        "plan = np.exp((f[:, None] + g[None, :] - cost) / eps)",
        "plan = np.exp((f[:, None] + g[None, :] + cost) / eps)",
        "tests/creative/atlas/test_wasserstein_schrodinger_bridge.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/wasserstein_schrodinger_bridge.py",
        "divergence = transport - 0.5 * self_source - 0.5 * self_target",
        "divergence = transport - 0.5 * self_source + 0.5 * self_target",
        "tests/creative/atlas/test_wasserstein_schrodinger_bridge.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/wasserstein_schrodinger_bridge.py",
        "variance = epsilon * t * (1.0 - t)",
        "variance = epsilon * t * (1.0 + t)",
        "tests/creative/atlas/test_wasserstein_schrodinger_bridge.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/wasserstein_schrodinger_bridge.py",
        "total = np.log(np.sum(np.exp(values - safe), axis=axis, keepdims=True))",
        "total = np.log(np.sum(np.exp(values + safe), axis=axis, keepdims=True))",
        "tests/creative/atlas/test_wasserstein_schrodinger_bridge.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/wasserstein_schrodinger_bridge.py",
        "return np.asarray((z - z.T) ** 2, dtype=np.float64)",
        "return np.asarray((z + z.T) ** 2, dtype=np.float64)",
        "tests/creative/atlas/test_wasserstein_schrodinger_bridge.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/adjoint_inverse_cinema_solver.py",
        'raise InverseCinemaError(f"theta must have shape ({PARAMETER_COUNT},)")',
        "pass  # MUT",
        "tests/creative/atlas/test_adjoint_inverse_cinema_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/adjoint_inverse_cinema_solver.py",
        'raise InverseCinemaError("theta must be finite")',
        "pass  # MUT",
        "tests/creative/atlas/test_adjoint_inverse_cinema_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/adjoint_inverse_cinema_solver.py",
        'raise InverseCinemaError("theta lies outside the physical box")',
        "pass  # MUT",
        "tests/creative/atlas/test_adjoint_inverse_cinema_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/adjoint_inverse_cinema_solver.py",
        'raise InverseCinemaError("step must be a positive finite number")',
        "pass  # MUT",
        "tests/creative/atlas/test_adjoint_inverse_cinema_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/adjoint_inverse_cinema_solver.py",
        'raise InverseCinemaError(f"target_observation must have shape ({OBSERVATION_COUNT},)")',
        "pass  # MUT",
        "tests/creative/atlas/test_adjoint_inverse_cinema_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/adjoint_inverse_cinema_solver.py",
        'raise InverseCinemaError("gamma must be finite and non-negative")',
        "pass  # MUT",
        "tests/creative/atlas/test_adjoint_inverse_cinema_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/adjoint_inverse_cinema_solver.py",
        'raise InverseCinemaError("lambda_initial must be positive and finite")',
        "pass  # MUT",
        "tests/creative/atlas/test_adjoint_inverse_cinema_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/adjoint_inverse_cinema_solver.py",
        "y[2] = e_key / fill_denom",
        "y[2] = e_key * fill_denom",
        "tests/creative/atlas/test_adjoint_inverse_cinema_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/adjoint_inverse_cinema_solver.py",
        "y[5] = float(np.exp(-extinction * _DEPTH))",
        "y[5] = float(np.exp(extinction * _DEPTH))",
        "tests/creative/atlas/test_adjoint_inverse_cinema_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/adjoint_inverse_cinema_solver.py",
        "jac[5, 13] = -_DEPTH * float(np.exp(-extinction * _DEPTH))",
        "jac[5, 13] = _DEPTH * float(np.exp(-extinction * _DEPTH))",
        "tests/creative/atlas/test_adjoint_inverse_cinema_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/adjoint_inverse_cinema_solver.py",
        "y[8] = 0.161 * _ROOM_VOLUME / (absorption * _ROOM_SURFACE)",
        "y[8] = 0.161 * _ROOM_VOLUME * (absorption * _ROOM_SURFACE)",
        "tests/creative/atlas/test_adjoint_inverse_cinema_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/adjoint_inverse_cinema_solver.py",
        "y[3] = albedo * (e_key * cos_key + e_fill * cos_fill)",
        "y[3] = albedo * (e_key * cos_key - e_fill * cos_fill)",
        "tests/creative/atlas/test_adjoint_inverse_cinema_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/adjoint_inverse_cinema_solver.py",
        "y[4] = (1.0 - roughness) * e_key * cos_key",
        "y[4] = (1.0 + roughness) * e_key * cos_key",
        "tests/creative/atlas/test_adjoint_inverse_cinema_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/adjoint_inverse_cinema_solver.py",
        "y[9] = absorption * _ROOM_SURFACE / (16.0 * np.pi * _SOURCE_DISTANCE**2)",
        "y[9] = absorption * _ROOM_SURFACE * (16.0 * np.pi * _SOURCE_DISTANCE**2)",
        "tests/creative/atlas/test_adjoint_inverse_cinema_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/adjoint_inverse_cinema_solver.py",
        "jac[3, 11] = e_key * cos_key + e_fill * cos_fill",
        "jac[3, 11] = e_key * cos_key - e_fill * cos_fill",
        "tests/creative/atlas/test_adjoint_inverse_cinema_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/riemannian_camera_choreographer.py",
        'raise CameraChoreographyError("sample time outside the trajectory span")',
        "pass  # MUT",
        "tests/creative/atlas/test_riemannian_camera_choreographer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/riemannian_camera_choreographer.py",
        'raise CameraChoreographyError("keyframe_positions must have shape (K, 3)")',
        "pass  # MUT",
        "tests/creative/atlas/test_riemannian_camera_choreographer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/riemannian_camera_choreographer.py",
        'raise CameraChoreographyError("keyframe_times must have one entry per keyframe")',
        "pass  # MUT",
        "tests/creative/atlas/test_riemannian_camera_choreographer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/riemannian_camera_choreographer.py",
        'raise CameraChoreographyError("keyframes must be finite")',
        "pass  # MUT",
        "tests/creative/atlas/test_riemannian_camera_choreographer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/riemannian_camera_choreographer.py",
        'raise CameraChoreographyError(f"keyframe rotation {index} is not orthonormal")',
        "pass  # MUT",
        "tests/creative/atlas/test_riemannian_camera_choreographer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/riemannian_camera_choreographer.py",
        'raise CameraChoreographyError("positions must have shape (N, 3)")',
        "pass  # MUT",
        "tests/creative/atlas/test_riemannian_camera_choreographer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/riemannian_camera_choreographer.py",
        'raise CameraChoreographyError("obstacles must have shape (M, 4)")',
        "pass  # MUT",
        "tests/creative/atlas/test_riemannian_camera_choreographer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/riemannian_camera_choreographer.py",
        'raise CameraChoreographyError("at least one obstacle is required")',
        "pass  # MUT",
        "tests/creative/atlas/test_riemannian_camera_choreographer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/riemannian_camera_choreographer.py",
        'raise CameraChoreographyError("positions and obstacles must be finite")',
        "pass  # MUT",
        "tests/creative/atlas/test_riemannian_camera_choreographer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/riemannian_camera_choreographer.py",
        'raise CameraChoreographyError("obstacle radii must be positive")',
        "pass  # MUT",
        "tests/creative/atlas/test_riemannian_camera_choreographer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/riemannian_camera_choreographer.py",
        'raise CameraChoreographyError("delta_safe must be finite and non-negative")',
        "pass  # MUT",
        "tests/creative/atlas/test_riemannian_camera_choreographer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/riemannian_camera_choreographer.py",
        'raise CameraChoreographyError("roll_angles must match the number of samples")',
        "pass  # MUT",
        "tests/creative/atlas/test_riemannian_camera_choreographer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/riemannian_camera_choreographer.py",
        "if residual > 1e-6:",
        "if residual > 1e9:",
        "tests/creative/atlas/test_riemannian_camera_choreographer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/riemannian_camera_choreographer.py",
        "if obs.shape[0] < 1:",
        "if False:",
        "tests/creative/atlas/test_riemannian_camera_choreographer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/riemannian_camera_choreographer.py",
        "if samples_per_segment < 1:",
        "if False:",
        "tests/creative/atlas/test_riemannian_camera_choreographer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/nash_dramatic_staging_solver.py",
        'raise StagingError(f"unknown proxemic preset {name!r}")',
        "pass  # MUT",
        "tests/creative/atlas/test_nash_dramatic_staging_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/nash_dramatic_staging_solver.py",
        'raise StagingError(f"actor_count must be an int in [2, {MAX_ACTORS}]")',
        "pass  # MUT",
        "tests/creative/atlas/test_nash_dramatic_staging_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/nash_dramatic_staging_solver.py",
        'raise StagingError("camera_position must be a 2-D point")',
        "pass  # MUT",
        "tests/creative/atlas/test_nash_dramatic_staging_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/nash_dramatic_staging_solver.py",
        'raise StagingError("camera_position must be finite")',
        "pass  # MUT",
        "tests/creative/atlas/test_nash_dramatic_staging_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/nash_dramatic_staging_solver.py",
        'raise StagingError("interaction indices must be distinct valid actors")',
        "pass  # MUT",
        "tests/creative/atlas/test_nash_dramatic_staging_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/nash_dramatic_staging_solver.py",
        'raise StagingError("speaker index out of range")',
        "pass  # MUT",
        "tests/creative/atlas/test_nash_dramatic_staging_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/nash_dramatic_staging_solver.py",
        'raise StagingError("d_collision must be positive and finite")',
        "pass  # MUT",
        "tests/creative/atlas/test_nash_dramatic_staging_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/nash_dramatic_staging_solver.py",
        'raise StagingError(f"initial_poses must have shape ({problem.actor_count}, 3)")',
        "pass  # MUT",
        "tests/creative/atlas/test_nash_dramatic_staging_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/nash_dramatic_staging_solver.py",
        'raise StagingError("initial_poses must be finite")',
        "pass  # MUT",
        "tests/creative/atlas/test_nash_dramatic_staging_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/nash_dramatic_staging_solver.py",
        'raise StagingError("epsilon must be finite and non-negative")',
        "pass  # MUT",
        "tests/creative/atlas/test_nash_dramatic_staging_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/nash_dramatic_staging_solver.py",
        'raise StagingError("learning_rate must be positive and finite")',
        "pass  # MUT",
        "tests/creative/atlas/test_nash_dramatic_staging_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/nash_dramatic_staging_solver.py",
        'raise StagingError("response_radius must be positive and finite")',
        "pass  # MUT",
        "tests/creative/atlas/test_nash_dramatic_staging_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/nash_dramatic_staging_solver.py",
        'raise StagingError("response_samples must be at least 3")',
        "pass  # MUT",
        "tests/creative/atlas/test_nash_dramatic_staging_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/nash_dramatic_staging_solver.py",
        "energy += 2.0 * math.exp(-(distance - target)) - math.exp(-2.0 * (distance - target))",
        "energy -= 2.0 * math.exp(-(distance - target)) - math.exp(-2.0 * (distance - target))",
        "tests/creative/atlas/test_nash_dramatic_staging_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/nash_dramatic_staging_solver.py",
        "if not (0 <= i < n and 0 <= j < n) or i == j:",
        "if False:",
        "tests/creative/atlas/test_nash_dramatic_staging_solver.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/anamorphic_scattering_renderer.py",
        'raise ScatteringError("asymmetry parameter g must satisfy |g| < 1")',
        "pass  # MUT",
        "tests/creative/atlas/test_anamorphic_scattering_renderer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/anamorphic_scattering_renderer.py",
        'raise ScatteringError("cos_theta must lie in [-1, 1]")',
        "pass  # MUT",
        "tests/creative/atlas/test_anamorphic_scattering_renderer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/anamorphic_scattering_renderer.py",
        'raise ScatteringError("quadrature_nodes must be at least 8")',
        "pass  # MUT",
        "tests/creative/atlas/test_anamorphic_scattering_renderer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/anamorphic_scattering_renderer.py",
        'raise ScatteringError("extinction_coefficient must be finite and non-negative")',
        "pass  # MUT",
        "tests/creative/atlas/test_anamorphic_scattering_renderer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/anamorphic_scattering_renderer.py",
        'raise ScatteringError("depth must be finite and non-negative")',
        "pass  # MUT",
        "tests/creative/atlas/test_anamorphic_scattering_renderer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/anamorphic_scattering_renderer.py",
        'raise ScatteringError("f_number must be positive and finite")',
        "pass  # MUT",
        "tests/creative/atlas/test_anamorphic_scattering_renderer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/anamorphic_scattering_renderer.py",
        'raise ScatteringError(f"squeeze must be one of {ANAMORPHIC_SQUEEZES}")',
        "pass  # MUT",
        "tests/creative/atlas/test_anamorphic_scattering_renderer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/anamorphic_scattering_renderer.py",
        'raise ScatteringError(f"side must be an odd int in [5, {MAX_GRID_SIDE}]")',
        "pass  # MUT",
        "tests/creative/atlas/test_anamorphic_scattering_renderer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/anamorphic_scattering_renderer.py",
        'raise ScatteringError("side must be odd so the kernel has a centre")',
        "pass  # MUT",
        "tests/creative/atlas/test_anamorphic_scattering_renderer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/anamorphic_scattering_renderer.py",
        'raise ScatteringError("coc_radius must be positive and finite")',
        "pass  # MUT",
        "tests/creative/atlas/test_anamorphic_scattering_renderer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/anamorphic_scattering_renderer.py",
        'raise ScatteringError("streak_length must be finite and non-negative")',
        "pass  # MUT",
        "tests/creative/atlas/test_anamorphic_scattering_renderer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/anamorphic_scattering_renderer.py",
        'raise ScatteringError("source_frequency must be positive and finite")',
        "pass  # MUT",
        "tests/creative/atlas/test_anamorphic_scattering_renderer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/anamorphic_scattering_renderer.py",
        'raise ScatteringError("amplitude must be in (0, 1]")',
        "pass  # MUT",
        "tests/creative/atlas/test_anamorphic_scattering_renderer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/anamorphic_scattering_renderer.py",
        'raise ScatteringError("acoustic source cannot reach or exceed the speed of sound")',
        "pass  # MUT",
        "tests/creative/atlas/test_anamorphic_scattering_renderer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/anamorphic_scattering_renderer.py",
        'raise ScatteringError("acoustic observer cannot reach or exceed the speed of sound")',
        "pass  # MUT",
        "tests/creative/atlas/test_anamorphic_scattering_renderer.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_studio_playground.py",
        'raise PlaygroundError("fork_time must be finite and non-negative")',
        "pass  # MUT",
        "tests/creative/atlas/test_prometheus_studio_playground.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_studio_playground.py",
        'raise PlaygroundError("fog_anisotropy_g must satisfy |g| < 1")',
        "pass  # MUT",
        "tests/creative/atlas/test_prometheus_studio_playground.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_studio_playground.py",
        'raise PlaygroundError("target_staging_proxemics_m must be positive")',
        "pass  # MUT",
        "tests/creative/atlas/test_prometheus_studio_playground.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_studio_playground.py",
        'raise PlaygroundError("inverse_target_t60_sec must be positive")',
        "pass  # MUT",
        "tests/creative/atlas/test_prometheus_studio_playground.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_studio_playground.py",
        'raise PlaygroundError("camera_obstacle_pos must be a 3-vector")',
        "pass  # MUT",
        "tests/creative/atlas/test_prometheus_studio_playground.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_studio_playground.py",
        'raise PlaygroundError(f"unknown universe id {universe_id!r}")',
        "pass  # MUT",
        "tests/creative/atlas/test_prometheus_studio_playground.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_studio_playground.py",
        'raise PlaygroundError("exactly 2 or 4 universes are required")',
        "pass  # MUT",
        "tests/creative/atlas/test_prometheus_studio_playground.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_studio_playground.py",
        'raise PlaygroundError("universe ids must be distinct")',
        "pass  # MUT",
        "tests/creative/atlas/test_prometheus_studio_playground.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_studio_playground.py",
        'raise PlaygroundError(f"side must be an int in [16, {MAX_QUADRANT_SIDE}]")',
        "pass  # MUT",
        "tests/creative/atlas/test_prometheus_studio_playground.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_studio_playground.py",
        "return flattened / total",
        "return flattened * total",
        "tests/creative/atlas/test_prometheus_studio_playground.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_studio_playground.py",
        "return float(0.161 * volume / max(surface * absorption, _EPS))",
        "return float(0.161 * volume * max(surface * absorption, _EPS))",
        "tests/creative/atlas/test_prometheus_studio_playground.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_studio_playground.py",
        "if len(universe_ids) not in (2, 4):",
        "if len(universe_ids) not in (2, 3, 4):",
        "tests/creative/atlas/test_prometheus_studio_playground.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_studio_playground.py",
        "if self.fog_anisotropy_g is not None and abs(self.fog_anisotropy_g) >= 1.0:",
        "if self.fog_anisotropy_g is not None and abs(self.fog_anisotropy_g) >= 2.0:",
        "tests/creative/atlas/test_prometheus_studio_playground.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_studio_playground.py",
        "if not _is_descendant_of(playground, state, ancestor.universe_id):",
        "if False:",
        "tests/creative/atlas/test_prometheus_studio_playground.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_studio_playground.py",
        "if self.target_staging_proxemics_m is not None and "
        "(self.target_staging_proxemics_m <= 0.0):",
        "if False:",
        "tests/creative/atlas/test_prometheus_studio_playground.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_verifier_and_governance.py",
        'raise VerifierError("index out of range")',
        "pass  # MUT",
        "tests/creative/atlas/test_prometheus_verifier_and_governance.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_verifier_and_governance.py",
        'raise VerifierError(f"reference PNG could not be decoded: {exc}") from exc',
        "pass  # MUT",
        "tests/creative/atlas/test_prometheus_verifier_and_governance.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_verifier_and_governance.py",
        'raise VerifierError("no reference PNG frames found on disk")',
        "pass  # MUT",
        "tests/creative/atlas/test_prometheus_verifier_and_governance.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_verifier_and_governance.py",
        "size = (size + 1) // 2",
        "size = size // 2",
        "tests/creative/atlas/test_prometheus_verifier_and_governance.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_verifier_and_governance.py",
        'if not leaves:\n        return _sha256(b"")',
        'if False:\n        return _sha256(b"")',
        "tests/creative/atlas/test_prometheus_verifier_and_governance.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_verifier_and_governance.py",
        "existing is not None\n"
        "            and existing.state is not LedgerState.UNOBSERVED\n"
        "            and observation.state is LedgerState.UNOBSERVED",
        "False",
        "tests/creative/atlas/test_prometheus_verifier_and_governance.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_verifier_and_governance.py",
        "mp4_matches = file_digests[receipt.mp4_path] == expected",
        "mp4_matches = True",
        "tests/creative/atlas/test_prometheus_verifier_and_governance.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_verifier_and_governance.py",
        "if not a or not b:\n        return -1.0",
        "if False:\n        return -1.0",
        "tests/creative/atlas/test_prometheus_verifier_and_governance.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_verifier_and_governance.py",
        'name = rel.replace("/", "__")',
        'name = rel.replace("/", "-")',
        "tests/creative/atlas/test_prometheus_verifier_and_governance.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_verifier_and_governance.py",
        "return filesystem.resolve(rel).is_file()",
        "return True",
        "tests/creative/atlas/test_prometheus_verifier_and_governance.py",
    ),
    (
        "nexus_ai_agent/creative/atlas/prometheus_verifier_and_governance.py",
        "if not target.is_dir():\n        return []",
        "if False:\n        return []",
        "tests/creative/atlas/test_prometheus_verifier_and_governance.py",
    ),
)


def test_campaign_has_exactly_one_hundred_twenty_mutants() -> None:
    assert len(MUTANTS) == 120


def test_every_mutation_anchor_is_unique() -> None:
    """Every ``original`` snippet must appear exactly once in the real tree."""
    for rel, old, _new, _test in MUTANTS:
        text = (SRC / rel).read_text(encoding="utf-8")
        assert text.count(old) == 1, f"{rel}: anchor count {text.count(old)} for {old!r}"


def test_every_target_test_file_exists() -> None:
    for _rel, _old, _new, target in MUTANTS:
        assert (REPO_ROOT / target).is_file(), f"missing target test {target}"


# --- causal kill proof: baseline, syntactic validity, exit-code discrimination ----


@dataclass(frozen=True)
class MutationKillReceipt:
    """A structured receipt proving one mutant was killed by a *test assertion*."""

    mutant_id: str
    target_module: str
    target_test_file: str
    killed_by_node_id: str
    exit_code: int


# pytest exit-code contract (see pytest's ``ExitCode`` enum):
#   0 OK, 1 TESTS_FAILED, 2 INTERRUPTED, 3 INTERNAL_ERROR, 4 USAGE_ERROR,
#   5 NO_TESTS_COLLECTED.
_PYTEST_TESTS_FAILED = 1
# A collection crash (not a test assertion) leaks one of these into the output.
_COLLECTION_CRASH_MARKERS = (
    "ImportError",
    "ModuleNotFoundError",
    "SyntaxError",
    "ERROR collecting",
)


def _failed_node_ids(stdout: str) -> list[str]:
    """Extract ``FAILED <nodeid>`` lines from a pytest ``-q`` summary."""
    ids: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("FAILED "):
            node = stripped[len("FAILED ") :].split(" ")[0].strip()
            if node:
                ids.append(node)
    return ids


# A single pristine copy of the unmutated tree, used for Step A baselines. The
# baseline for a given target file is identical for every mutant that targets it,
# so it is computed once and memoised by target-file path.
_BASELINE_STATUS: dict[str, int] = {}


def _make_pristine_tree(destination: Path) -> Path:
    shutil.copytree(SRC, destination / "src")
    shutil.copytree(TESTS, destination / "tests")
    shutil.copy(REPO_ROOT / "pyproject.toml", destination / "pyproject.toml")
    return destination


def _baseline_is_green(target_test: str, pristine_tree: Path) -> None:
    """Step A — the unmutated target test file must pass (exit code 0)."""
    if target_test not in _BASELINE_STATUS:
        env: dict[str, Any] = dict(os.environ)
        env["PYTHONPATH"] = str(pristine_tree / "src")
        baseline = subprocess.run(
            [sys.executable, "-m", "pytest", target_test, "-q", "-p", "no:cacheprovider"],
            cwd=pristine_tree,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        _BASELINE_STATUS[target_test] = baseline.returncode
        if baseline.returncode != 0:
            raise AssertionError(
                f"baseline FAILED for {target_test} (exit {baseline.returncode}); the target "
                f"test file is not green before mutation:\n{baseline.stdout[-2000:]}"
            )
    assert _BASELINE_STATUS[target_test] == 0


@pytest.fixture(scope="session")
def pristine_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _make_pristine_tree(tmp_path_factory.mktemp("pristine") / "tree")


@pytest.mark.parametrize(("rel", "old", "new", "target_test"), MUTANTS)
def test_mutant_is_killed(
    rel: str, old: str, new: str, target_test: str, tmp_path: Path, pristine_tree: Path
) -> None:
    """Each injected defect must make the targeted test file fail (by assertion).

    Three proofs are required, in order:

    * Step A — the unmutated target test file is green (``returncode == 0``);
    * Step B — the mutated module source is still valid Python (``ast.parse``);
    * Step C — pytest exits specifically with ``1`` (TESTS_FAILED) and reports at
      least one ``FAILED test_...`` node id, with no collection crash
      (``ImportError``/``ModuleNotFoundError``/``SyntaxError``). This separates a
      genuine *assertion* kill from an accidental syntax/import/collection error
      (exit codes 2/3/4/5).
    """
    work = tmp_path / "campaign"
    _make_pristine_tree(work)

    # Step A — baseline pre-flight on the unmutated tree (memoised per target file).
    _baseline_is_green(target_test, pristine_tree)

    module = work / "src" / rel
    original = module.read_text(encoding="utf-8")
    assert original.count(old) == 1
    mutated = original.replace(old, new, 1)

    # Step B — the mutated source must still parse as valid Python.
    ast.parse(mutated, filename=str(module))
    module.write_text(mutated, encoding="utf-8")

    env: dict[str, Any] = dict(os.environ)
    env["PYTHONPATH"] = str(work / "src")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", target_test, "-q", "-p", "no:cacheprovider", "-x"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )

    combined = result.stdout + result.stderr
    if result.returncode == 0:
        raise AssertionError(
            f"mutant SURVIVED in {rel}: {old!r} -> {new!r}\nstdout tail:\n{result.stdout[-2000:]}"
        )
    # Step C — a *causal assertion* kill, not a collection/infra crash.
    assert result.returncode == _PYTEST_TESTS_FAILED, (
        f"mutant in {rel} did not fail by assertion: pytest exit "
        f"{result.returncode} != {_PYTEST_TESTS_FAILED} (TESTS_FAILED). "
        f"exit 2=INTERRUPTED, 3=INTERNAL_ERROR, 4=USAGE_ERROR, 5=NO_TESTS_COLLECTED.\n"
        f"stdout tail:\n{result.stdout[-2000:]}"
    )
    for marker in _COLLECTION_CRASH_MARKERS:
        assert marker not in combined, (
            f"mutant in {rel} produced a collection/infra crash marker {marker!r}; "
            f"that is not an assertion kill:\n{combined[-2000:]}"
        )
    node_ids = _failed_node_ids(result.stdout)
    assert node_ids, (
        f"mutant in {rel} exited {result.returncode} but reported no 'FAILED <nodeid>'; "
        f"not a verifiable assertion kill:\n{result.stdout[-2000:]}"
    )


def _collect_kill_receipts(base: Path, pristine_tree: Path) -> list[MutationKillReceipt]:
    """Run all 120 mutants and return a structured kill receipt for each (M001..M120)."""
    receipts: list[MutationKillReceipt] = []
    for index, (rel, old, new, target_test) in enumerate(MUTANTS, start=1):
        mutant_id = f"M{index:03d}"
        _baseline_is_green(target_test, pristine_tree)
        work = base / mutant_id
        _make_pristine_tree(work)

        module = work / "src" / rel
        mutated = module.read_text(encoding="utf-8").replace(old, new, 1)
        ast.parse(mutated, filename=str(module))
        module.write_text(mutated, encoding="utf-8")

        env: dict[str, Any] = dict(os.environ)
        env["PYTHONPATH"] = str(work / "src")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", target_test, "-q", "-p", "no:cacheprovider", "-x"],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        nodes = _failed_node_ids(result.stdout)
        receipts.append(
            MutationKillReceipt(
                mutant_id=mutant_id,
                target_module=rel,
                target_test_file=target_test,
                killed_by_node_id=nodes[0] if nodes else "",
                exit_code=result.returncode,
            )
        )
    return receipts


def test_all_one_hundred_twenty_mutants_have_a_causal_assertion_kill_receipt(
    tmp_path_factory: pytest.TempPathFactory, pristine_tree: Path
) -> None:
    """Every mutant M001..M120 must be killed specifically by a failing assertion."""
    base = tmp_path_factory.mktemp("kill_ledger")
    receipts = _collect_kill_receipts(base, pristine_tree)
    assert len(receipts) == 120
    assert [r.mutant_id for r in receipts] == [f"M{i:03d}" for i in range(1, 121)]
    bad = [r for r in receipts if r.exit_code != _PYTEST_TESTS_FAILED or not r.killed_by_node_id]
    assert not bad, f"non-causal kills: {bad}"
    for r in receipts:
        assert r.exit_code == _PYTEST_TESTS_FAILED
        assert r.killed_by_node_id.startswith("test_") or "::test_" in r.killed_by_node_id
