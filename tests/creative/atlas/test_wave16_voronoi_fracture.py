"""WAVE 16 — tests for Voronoi fracture, energy budget and the destruction ledger."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import voronoi_fracture_destruction_ledger as vf


def _grid_seeds(n: int = 2) -> list[vf.FractureSeed]:
    return [
        vf.FractureSeed(np.array([float(i), float(j), float(k)]), 1.0)
        for i in range(n)
        for j in range(n)
        for k in range(n)
    ]


# --- seed validation -------------------------------------------------------------


def test_seed_rejects_bad_shape() -> None:
    with pytest.raises(vf.FractureError):
        vf.FractureSeed(np.array([1.0, 2.0]))


def test_seed_rejects_nan() -> None:
    with pytest.raises(vf.FractureError):
        vf.FractureSeed(np.array([1.0, np.nan, 0.0]))


def test_seed_rejects_nonpositive_density() -> None:
    with pytest.raises(vf.FractureError):
        vf.FractureSeed(np.array([1.0, 1.0, 1.0]), density=0.0)


# --- fracture --------------------------------------------------------------------


def test_fracture_mass_is_exact() -> None:
    res = vf.fracture_voronoi_cells((8, 8, 8), _grid_seeds(2))
    assert res.mass_drift == 0.0
    assert res.total_mass == pytest.approx(res.body_mass, abs=1e-12)


def test_fracture_covers_all_voxels() -> None:
    res = vf.fracture_voronoi_cells((8, 8, 8), _grid_seeds(2))
    assert res.total_voxels == 8 * 8 * 8


def test_fracture_variable_density_mass_exact() -> None:
    seeds = [
        vf.FractureSeed(np.array([1.0, 1.0, 1.0]), 2.0),
        vf.FractureSeed(np.array([5.0, 5.0, 5.0]), 1.0),
    ]
    res = vf.fracture_voronoi_cells((8, 8, 8), seeds)
    assert res.mass_drift == 0.0
    assert res.total_mass == pytest.approx(720.0, abs=1e-9)


def test_fracture_assignment_is_deterministic() -> None:
    a = vf.fracture_voronoi_cells((6, 6, 6), _grid_seeds(2))
    b = vf.fracture_voronoi_cells((6, 6, 6), _grid_seeds(2))
    assert np.array_equal(a.assignment, b.assignment)


def test_fracture_tie_break_is_lowest_index() -> None:
    # Two seeds equidistant from the midpoint voxel: the lower index wins.
    seeds = [
        vf.FractureSeed(np.array([0.0, 0.0, 0.0])),
        vf.FractureSeed(np.array([4.0, 0.0, 0.0])),
    ]
    res = vf.fracture_voronoi_cells((5, 1, 1), seeds)
    # Midpoint voxel (2,0,0) is equidistant; index 0 must own it.
    assert res.assignment[2, 0, 0] == 0


def test_fracture_body_mask_excludes_voxels() -> None:
    mask = np.zeros((6, 6, 6), bool)
    mask[:, :, :3] = True
    res = vf.fracture_voronoi_cells((6, 6, 6), _grid_seeds(2), body_mask=mask)
    assert res.total_voxels == 6 * 6 * 3
    assert res.mass_drift == 0.0


def test_fracture_empty_seed_cell_is_skipped() -> None:
    # Seed far outside the body owns no voxels and must not appear as a cell.
    seeds = [
        vf.FractureSeed(np.array([0.0, 0.0, 0.0])),
        vf.FractureSeed(np.array([100.0, 100.0, 100.0])),
    ]
    res = vf.fracture_voronoi_cells((4, 4, 4), seeds)
    assert len(res.cells) == 1
    assert res.cells[0].index == 0


def test_fracture_rejects_bad_shape() -> None:
    with pytest.raises(vf.FractureError):
        vf.fracture_voronoi_cells((0, 4, 4), _grid_seeds(1))


def test_fracture_rejects_no_seeds() -> None:
    with pytest.raises(vf.FractureError):
        vf.fracture_voronoi_cells((4, 4, 4), [])


def test_fracture_rejects_too_many_seeds() -> None:
    seeds = [vf.FractureSeed(np.array([0.0, 0.0, 0.0])) for _ in range(vf.MAX_SEEDS + 1)]
    with pytest.raises(vf.FractureError):
        vf.fracture_voronoi_cells((4, 4, 4), seeds)


def test_fracture_rejects_over_budget() -> None:
    shape = (300, 300, 300)  # 27M > 8.39M
    with pytest.raises(vf.FractureError):
        vf.fracture_voronoi_cells(shape, _grid_seeds(1))


def test_fracture_rejects_bad_voxel_volume() -> None:
    with pytest.raises(vf.FractureError):
        vf.fracture_voronoi_cells((4, 4, 4), _grid_seeds(1), voxel_volume=0.0)


def test_fracture_rejects_bad_mask_shape() -> None:
    with pytest.raises(vf.FractureError):
        vf.fracture_voronoi_cells((4, 4, 4), _grid_seeds(1), body_mask=np.zeros((2, 2), bool))


def test_fracture_rejects_empty_mask() -> None:
    with pytest.raises(vf.FractureError):
        vf.fracture_voronoi_cells((4, 4, 4), _grid_seeds(1), body_mask=np.zeros((4, 4, 4), bool))


# --- energy budget ---------------------------------------------------------------


def test_energy_feasible_when_kinetic_exceeds_surface() -> None:
    rep = vf.impact_energy_budget(
        impactor_mass=2.0, impactor_speed=10.0, fracture_toughness=5.0, new_surface_area=10.0
    )
    assert rep.kinetic_energy == pytest.approx(100.0)
    assert rep.surface_energy == pytest.approx(50.0)
    assert rep.verdict == "FEASIBLE"
    assert rep.fragment_budget == pytest.approx(25.0)


def test_energy_infeasible_when_surface_exceeds_kinetic() -> None:
    rep = vf.impact_energy_budget(
        impactor_mass=0.1, impactor_speed=1.0, fracture_toughness=100.0, new_surface_area=100.0
    )
    assert rep.verdict == "INFEASIBLE"
    assert rep.fragment_budget == 0.0


def test_energy_rejects_bad_mass() -> None:
    with pytest.raises(vf.FractureError):
        vf.impact_energy_budget(
            impactor_mass=0.0, impactor_speed=1.0, fracture_toughness=1.0, new_surface_area=1.0
        )


def test_energy_rejects_negative_speed() -> None:
    with pytest.raises(vf.FractureError):
        vf.impact_energy_budget(
            impactor_mass=1.0, impactor_speed=-1.0, fracture_toughness=1.0, new_surface_area=1.0
        )


def test_energy_rejects_negative_toughness() -> None:
    with pytest.raises(vf.FractureError):
        vf.impact_energy_budget(
            impactor_mass=1.0, impactor_speed=1.0, fracture_toughness=-1.0, new_surface_area=1.0
        )


def test_energy_rejects_negative_area() -> None:
    with pytest.raises(vf.FractureError):
        vf.impact_energy_budget(
            impactor_mass=1.0, impactor_speed=1.0, fracture_toughness=1.0, new_surface_area=-1.0
        )


def test_energy_rejects_bad_fraction() -> None:
    with pytest.raises(vf.FractureError):
        vf.impact_energy_budget(
            impactor_mass=1.0,
            impactor_speed=1.0,
            fracture_toughness=1.0,
            new_surface_area=1.0,
            fragment_energy_fraction=1.5,
        )


# --- destruction ledger ----------------------------------------------------------


def _cells() -> list[vf.FractureCell]:
    res = vf.fracture_voronoi_cells((6, 6, 6), _grid_seeds(2))
    return res.cells


def test_ledger_appends_and_verifies() -> None:
    led = vf.DestructionLedger()
    led.append_frame(0, _cells())
    led.append_frame(1, _cells())
    assert led.frame_count == 2
    assert led.verify().valid


def test_ledger_genesis_head_before_append() -> None:
    led = vf.DestructionLedger()
    assert led.head == "0" * 64
    assert led.frame_count == 0


def test_ledger_hash_changes_with_positions() -> None:
    cells = _cells()
    led = vf.DestructionLedger()
    h0 = led.append_frame(0, cells)
    led2 = vf.DestructionLedger()
    h1 = led2.append_frame(0, cells, positions=np.array([c.centroid + 1.0 for c in cells]))
    assert h0 != h1


def test_ledger_detects_tamper() -> None:
    led = vf.DestructionLedger()
    led.append_frame(0, _cells())
    led.append_frame(1, _cells())
    led.tamper_frame(0, {"frame": 0, "cells": [{"id": 0, "voxels": 1, "microns": [0, 0, 0]}]})
    ver = led.verify()
    assert not ver.valid
    assert ver.first_bad_frame == 0


def test_ledger_hash_at_out_of_range() -> None:
    led = vf.DestructionLedger()
    led.append_frame(0, _cells())
    with pytest.raises(vf.FractureError):
        led.hash_at(5)


def test_ledger_rejects_noncontiguous_frames() -> None:
    led = vf.DestructionLedger()
    with pytest.raises(vf.FractureError):
        led.append_frame(1, _cells())


def test_ledger_rejects_empty_cells() -> None:
    led = vf.DestructionLedger()
    with pytest.raises(vf.FractureError):
        led.append_frame(0, [])


def test_ledger_rejects_bad_positions_shape() -> None:
    led = vf.DestructionLedger()
    with pytest.raises(vf.FractureError):
        led.append_frame(0, _cells(), positions=np.zeros((2, 3)))


def test_ledger_rejects_nan_positions() -> None:
    led = vf.DestructionLedger()
    cells = _cells()
    pos = np.array([c.centroid for c in cells])
    pos[0, 0] = np.nan
    with pytest.raises(vf.FractureError):
        led.append_frame(0, cells, positions=pos)


def test_ledger_tamper_out_of_range() -> None:
    led = vf.DestructionLedger()
    with pytest.raises(vf.FractureError):
        led.tamper_frame(0, {})


def test_ledger_hash_at_returns_stored_hash() -> None:
    led = vf.DestructionLedger()
    h0 = led.append_frame(0, _cells())
    assert led.hash_at(0) == h0
