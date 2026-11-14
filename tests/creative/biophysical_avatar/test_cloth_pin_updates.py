from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.biophysical_avatar.locomotion_and_cloth_physics import (
    GarmentCategory,
    GarmentDefinition,
    VerletClothSolver,
    make_cloth_grid,
)


def test_cloth_attachment_pins_follow_body_without_velocity_spikes() -> None:
    mesh = make_cloth_grid(0.8, 1.2, columns=4, rows=5)
    fabric = GarmentDefinition(
        "test_fabric", GarmentCategory.MODERN, 0.8, 1.2, 0.04, 0.02, 8, (80, 90, 100)
    )
    solver = VerletClothSolver.from_mesh(mesh, fabric)
    pin = mesh.pinned_indices[0]
    destination = (0.3, -0.2, 1.8)
    solver.update_pins({pin: destination})
    assert tuple(solver.positions[pin]) == destination
    assert tuple(solver.previous_positions[pin]) == destination
    solver.step(0.02)
    assert np.allclose(solver.positions[pin], destination, atol=0.0)
    with pytest.raises(ValueError, match="only fixed"):
        solver.update_pins({mesh.columns + 1: destination})
    with pytest.raises(ValueError, match="anchor position"):
        solver.update_pins({pin: (0.0, 1.0)})


def test_verlet_constraint_projection_handles_fixed_endpoints_and_bad_inputs() -> None:
    coincident = ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    both_fixed = VerletClothSolver(coincident, ((0, 1, 1.0),), fixed_indices=(0, 1))
    both_fixed.step(0.01)
    assert both_fixed.max_constraint_error == 1.0

    first_fixed = VerletClothSolver(coincident, ((0, 1, 1.0),), fixed_indices=(0,), iterations=1)
    first_fixed.step(0.01)
    assert np.linalg.norm(first_fixed.positions[1] - first_fixed.positions[0]) == pytest.approx(1.0)

    second_fixed = VerletClothSolver(coincident, ((0, 1, 1.0),), fixed_indices=(1,), iterations=1)
    second_fixed.step(0.01)
    assert np.linalg.norm(second_fixed.positions[1] - second_fixed.positions[0]) == pytest.approx(
        1.0
    )

    nonzero_second_fixed = VerletClothSolver(
        ((0.0, 0.0, 0.0), (2.0, 0.0, 0.0)),
        ((0, 1, 1.0),),
        fixed_indices=(1,),
        iterations=1,
    )
    nonzero_second_fixed.step(0.01)
    assert np.linalg.norm(
        nonzero_second_fixed.positions[1] - nonzero_second_fixed.positions[0]
    ) == pytest.approx(1.0)
    with pytest.raises(ValueError, match="numeric"):
        VerletClothSolver("bad", ())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_wind_acceleration"):
        VerletClothSolver(((0.0, 0.0, 0.0),), (), max_wind_acceleration=float("nan"))
    with pytest.raises(ValueError, match="3-vector"):
        nonzero_second_fixed.step(0.01, wind=(float("nan"), 0.0, 0.0))
