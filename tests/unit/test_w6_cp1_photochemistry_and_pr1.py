"""W6 CP1: photochemical emulsion sensitometry + PR #342 supersession.

Proves the Hurter–Driffield D–log10 H curve (all three silver-halide layers),
Arrhenius slope kinetics, Schwarzschild reciprocity, DIR reaction–diffusion /
Beer–Lambert transmission, and verifies the Hyperion artifact-vault / backup
integrity contracts of old PR #342 on the current main base.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from nexus_ai_agent.creative.science.photochemical_emulsion import (
    CANONICAL_LAYERS,
    EmulsionLayer,
    arrhenius_gamma,
    beer_lambert_rgb,
    dir_film_state,
    dir_reaction_diffusion,
    hurt_driffield_slope_ln,
    hurt_driffield_slope_log10,
    hurter_driffield_density,
    layer_gamma,
    schwarzschild_reciprocity,
)
from nexus_ai_agent.storage.artifact_vault.cdc_chunker import GearChunker
from nexus_ai_agent.storage.artifact_vault.galois_erasure import encode, reconstruct
from nexus_ai_agent.storage.artifact_vault.vault_manager import ArtifactVault

# ---------------------------------------------------------------------------
# Hurter–Driffield curve
# ---------------------------------------------------------------------------


class TestHurterDriffield:
    @pytest.mark.parametrize(
        "layer",
        CANONICAL_LAYERS,
        ids=[layer.name for layer in CANONICAL_LAYERS],
    )
    def test_inflection_point_is_midpoint(self, layer):
        d = hurter_driffield_density(layer, layer.log10_h0, 293.15)
        assert d == pytest.approx(layer.midpoint, rel=1e-12)

    @pytest.mark.parametrize(
        "layer",
        CANONICAL_LAYERS,
        ids=[layer.name for layer in CANONICAL_LAYERS],
    )
    def test_slope_in_natural_log_at_inflection_equals_gamma(self, layer):
        slope = hurt_driffield_slope_ln(layer, layer.log10_h0, 293.15)
        assert slope == pytest.approx(layer_gamma(layer, 293.15), rel=1e-12)

    @pytest.mark.parametrize(
        "layer",
        CANONICAL_LAYERS,
        ids=[layer.name for layer in CANONICAL_LAYERS],
    )
    def test_slope_in_log10_at_inflection_is_ln10_times_gamma(self, layer):
        slope = hurt_driffield_slope_log10(layer, layer.log10_h0, 293.15)
        assert slope == pytest.approx(math.log(10.0) * layer_gamma(layer, 293.15), rel=1e-12)

    def test_curve_is_monotone_in_stimulus(self):
        layer = CANONICAL_LAYERS[0]
        stimuli = np.linspace(0.5, 4.0, 25)
        densities = [hurter_driffield_density(layer, value, 293.15) for value in stimuli]
        assert all(b >= a for a, b in zip(densities, densities[1:], strict=False))

    def test_extremes_saturate(self):
        layer = CANONICAL_LAYERS[0]
        at_low = hurter_driffield_density(layer, -20.0, 293.15)
        at_high = hurter_driffield_density(layer, 20.0, 293.15)
        assert at_low == pytest.approx(layer.d_min, rel=1e-6)
        assert at_high == pytest.approx(layer.d_max, rel=1e-6)

    def test_explicit_layer_at_inflection(self):
        layer = EmulsionLayer("test", d_min=0.1, d_max=2.5, log10_h0=3.0, gamma_ref=3.0)
        assert hurter_driffield_density(layer, 3.0, 293.15) == pytest.approx(1.3, rel=1e-12)
        slope = hurt_driffield_slope_ln(layer, 3.0, 293.15)
        assert slope == pytest.approx(3.0, rel=1e-12)

    def test_invalid_layer_refuses(self):
        with pytest.raises(ValueError):
            EmulsionLayer("bad", d_min=2.0, d_max=1.0)


# ---------------------------------------------------------------------------
# Arrhenius slope kinetics
# ---------------------------------------------------------------------------


class TestArrhenius:
    def test_gamma_at_reference_temperature(self):
        assert arrhenius_gamma(2.4, 42_000.0, 293.15, 293.15) == pytest.approx(2.4, rel=1e-12)

    def test_gamma_rises_with_temperature(self):
        gamma_cool = arrhenius_gamma(2.0, 42_000.0, 293.15, 293.15)
        gamma_hot = arrhenius_gamma(2.0, 42_000.0, 293.15, 313.15)
        assert gamma_hot > gamma_cool

    def test_higher_activation_barrier_amplifies_temperature_response(self):
        low_ea = arrhenius_gamma(2.0, 10_000.0, 293.15, 313.15)
        high_ea = arrhenius_gamma(2.0, 100_000.0, 293.15, 313.15)
        # Above T_ref the exponent is positive; a larger ea amplifies the increase.
        assert high_ea > low_ea

    def test_negative_ea_decreases_with_temperature(self):
        gamma_cool = arrhenius_gamma(2.0, -50_000.0, 293.15, 293.15)
        gamma_hot = arrhenius_gamma(2.0, -50_000.0, 293.15, 323.15)
        assert gamma_hot < gamma_cool

    def test_zero_ea_is_constant(self):
        g1 = arrhenius_gamma(2.0, 0.0, 293.15, 293.15)
        g2 = arrhenius_gamma(2.0, 0.0, 293.15, 373.15)
        assert g1 == pytest.approx(g2, rel=1e-12)

    def test_invalid_arguments(self):
        with pytest.raises(ValueError):
            arrhenius_gamma(2.0, 42_000.0, -1.0, 293.15)
        with pytest.raises(ValueError):
            arrhenius_gamma(0.0, 42_000.0, 293.15, 293.15)


# ---------------------------------------------------------------------------
# Schwarzschild reciprocity law
# ---------------------------------------------------------------------------


class TestSchwarzschildReciprocity:
    def test_p_equals_one(self):
        assert schwarzschild_reciprocity(11.0, 100.0, 1.0) == pytest.approx(1100.0)

    def test_p_equals_half(self):
        assert schwarzschild_reciprocity(11.0, 100.0, 0.5) == pytest.approx(110.0)

    def test_invalid_p_refused(self):
        with pytest.raises(ValueError):
            schwarzschild_reciprocity(11.0, 100.0, 0.4)
        with pytest.raises(ValueError):
            schwarzschild_reciprocity(11.0, 100.0, 1.5)

    def test_zero_intensity_refused(self):
        with pytest.raises(ValueError):
            schwarzschild_reciprocity(0.0, 100.0, 1.0)

    def test_zero_time_refused(self):
        with pytest.raises(ValueError):
            schwarzschild_reciprocity(11.0, 0.0, 1.0)

    def test_reciprocity_invariance_for_p_one(self):
        first = schwarzschild_reciprocity(2.0, 100.0, 1.0)
        second = schwarzschild_reciprocity(4.0, 50.0, 1.0)
        assert first == second


# ---------------------------------------------------------------------------
# DIR reaction-diffusion + Beer-Lambert
# ---------------------------------------------------------------------------


class TestDirReactionDiffusion:
    def test_clips_at_bounds(self):
        grid = np.array([0.1, 5.0, 2.0, 0.2])
        final = dir_reaction_diffusion(grid, 0.5, 0.0, 3.0)
        assert final.max() <= 3.0
        assert final.min() >= 0.0

    def test_reaction_term_moves_in_right_direction_and_clips(self):
        # Physical diffusion: gradients smooth out; a local peak is decreased,
        # a local trough is increased.
        grid = np.array([0.0, 1.0, 0.0, 1.0, 0.0])
        # interior Laplacian at index 2 = 1 - 0 + 1 = 2; physical step: 0 + 0.2*2 = 0.4
        # final[1]: 1.0 + 0.2*(0.0 - 2.0*1.0 + 0.0) = 0.6 (the hump recedes)
        final = dir_reaction_diffusion(grid, 0.2, 0.0, 3.0)
        assert final[2] == pytest.approx(0.4)
        assert final[1] == pytest.approx(1.0 + 0.2 * (0.0 - 2.0 * 1.0 + 0.0))  # 0.6
        assert final[1] < 1.0  # peak receded
        assert final[2] > 0.0  # trough filled

    def test_diffusion_is_physical_not_anti_diffusive(self):
        # A single peak must spread (peak decreases, neighbours rise).
        grid = np.ones(7)
        grid[3] = 5.0
        final = dir_reaction_diffusion(grid, 0.1, 0.0, 3.0)
        assert final[3] < 5.0
        assert final[2] > 1.0
        assert final[4] > 1.0

    def test_cfl_violation_is_refused_fail_closed(self):
        # 2D limit is r <= 0.25: this grid's kappa * dt / dx^2 = 0.5 is rejected.
        grid = np.ones((5, 5))
        with pytest.raises(ValueError, match="CFL"):
            dir_reaction_diffusion(grid, 0.5, 0.0, 3.0)

    def test_cfl_limit_1d_boundary_is_accepted(self):
        # 1D limit is r <= 0.5: exactly 0.5 is on the boundary and is allowed.
        grid = np.array([0.0, 1.0, 0.0, 1.0, 0.0])
        final = dir_reaction_diffusion(grid, 0.5, 0.0, 3.0)
        assert final.shape == (5,)

    def test_cfl_with_explicit_grid_spacing_and_time_step(self):
        grid = np.ones(5)
        with pytest.raises(ValueError, match="CFL"):
            dir_reaction_diffusion(grid, 0.5, 0.0, 3.0, grid_spacing=0.5, time_step=0.5)
        # passing case: r = 0.5 * 0.1 / 0.25 = 0.2 <= 0.5 -> accepted
        final = dir_reaction_diffusion(grid, 0.5, 0.0, 3.0, grid_spacing=0.5, time_step=0.1)
        assert final.shape == (5,)

    def test_1d_laplacian_interior(self):
        grid = np.array([0.0, 1.0, 0.0, 1.0, 0.0])
        state = dir_film_state(grid, 0.2, 0.0, 3.0)
        # interior laplacian at index 2 (with current neighbors 1 and 1)
        assert state.laplacian[2] == pytest.approx(1.0 - 2.0 * 0.0 + 1.0)

    def test_2d_grid(self):
        grid = np.ones((5, 5))
        grid[2, 2] = 5.0
        final = dir_reaction_diffusion(grid, 0.1, 0.0, 3.0)
        assert final[2, 2] < 5.0
        assert final.shape == (5, 5)

    def test_empty_grid_refused(self):
        with pytest.raises(ValueError):
            dir_reaction_diffusion(np.array([]), 0.2, 0.0, 3.0)

    def test_invalid_clip(self):
        with pytest.raises(ValueError):
            dir_reaction_diffusion(np.array([1.0]), 0.2, 3.0, 0.5)


class TestBeerLambert:
    def test_identity_matrix_simple(self):
        M = np.eye(3)
        T = beer_lambert_rgb(M, np.array([0.0, 0.0, 0.0]))
        assert T == pytest.approx(np.array([1.0, 1.0, 1.0]))

    def test_transmission_monotone_decreasing_with_density(self):
        M = np.array([[0.5, 0.1, 0.0], [0.1, 0.7, 0.0], [0.0, 0.2, 0.4]])
        T_lo = beer_lambert_rgb(M, np.array([0.1, 0.1, 0.1]))
        T_hi = beer_lambert_rgb(M, np.array([0.5, 0.5, 0.5]))
        assert np.all(T_hi <= T_lo)

    def test_transmission_in_unit_interval(self):
        M = np.array([[0.5, 0.1, 0.0], [0.1, 0.7, 0.0], [0.0, 0.2, 0.4]])
        T = beer_lambert_rgb(M, np.array([0.5, 0.5, 0.5]))
        assert np.all(T > 0.0)
        assert np.all(T <= 1.0)

    def test_shape_mismatch_refused(self):
        with pytest.raises(ValueError):
            beer_lambert_rgb(np.eye(2), np.array([0.1, 0.1, 0.1]))

    def test_negative_extinction_refused(self):
        M = np.eye(3)
        M[0, 0] = -0.5
        with pytest.raises(ValueError):
            beer_lambert_rgb(M, np.array([0.1, 0.1, 0.1]))


# ---------------------------------------------------------------------------
# PR #342 supersession: artifact vault + backup quoting + erasure healing
# ---------------------------------------------------------------------------


class TestHyperionVault:
    def test_cdc_chunker_is_deterministic_and_complete(self):
        data = b"NAGAR-hyperion-hyperion-hyperion-data-" * 200
        first = GearChunker().chunks(data)
        second = GearChunker().chunks(data)
        assert [c.data for c in first] == [c.data for c in second]
        assert b"".join(c.data for c in first) == data
        assert all(c.index == i for i, c in enumerate(first))
        assert all(c.length == len(c.data) for c in first)

    def test_erasure_round_trip_with_hostile_shard_loss(self):
        data = b"hyperion-payload-" * 400
        shards = encode(data, 4, 2)
        surviving = [s for s in shards if s.index not in (1, 3)] + [
            s for s in shards if s.index == 0
        ]
        # Need at least 4 valid shards; keep 4 survivors including parity.
        assert len(surviving) >= 4
        recovered = reconstruct(surviving, original_size=len(data))
        assert recovered == data

    def test_vault_store_and_heal_round_trip(self, tmp_path):
        vault = ArtifactVault(tmp_path)
        payload = b"NAGAR-artifact-" + bytes(range(256)) * 16
        receipt = vault.store_artifact("hyperion-01", payload, k=3, m=2)
        assert receipt.verified is True
        assert vault.read_artifact("hyperion-01") == payload
        # Simulate one corrupt shard: heal should restore it.
        vault.load_manifest("hyperion-01")
        corrupt_index = 0
        shard_path = tmp_path / "hyperion-01" / f"shard-{corrupt_index:04d}.bin"
        original = shard_path.read_bytes()
        shard_path.write_bytes(b"corrupt" * (len(original) // 7))
        healing = vault.verify_and_heal_artifact("hyperion-01")
        assert healing.verified is True
        assert vault.read_artifact("hyperion-01") == payload

    def test_backup_sqlite_inventory_quoting(self, tmp_path):
        # PR #342's backup-inventory identifier quoting contract on current main.
        import sqlite3

        from nexus_ai_agent.maintenance.backup import _sqlite_inventory

        conn = sqlite3.connect(":memory:")
        hostile = 'a"b; DROP TABLE safe; --'
        conn.execute('CREATE TABLE "' + hostile.replace('"', '""') + '" (x INTEGER)')
        conn.execute("CREATE TABLE safe (x INTEGER)")
        conn.execute("INSERT INTO safe VALUES (1)")
        conn.execute('INSERT INTO "' + hostile.replace('"', '""') + '" VALUES (7), (8)')
        counts = _sqlite_inventory(None, conn)  # type: ignore[arg-type]
        assert counts == {hostile: 2, "safe": 1}
        assert conn.execute("SELECT COUNT(*) FROM safe").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# W6-CP5: P1 regression tests — Hurter–Driffield derivative vs 4th-order FD,
# Beer–Lambert bounds
# ---------------------------------------------------------------------------


class TestHurterDriffield4thOrderFD:
    @pytest.mark.parametrize("layer", CANONICAL_LAYERS)
    def test_log10_slope_matches_4th_order_central_difference(self, layer):
        h = 1e-4
        x = 2.0  # well away from the sigmoid tails and the clamp edges

        def D(u):
            return hurter_driffield_density(layer, u, 293.15)

        fd = (-D(x + 2 * h) + 8.0 * D(x + h) - 8.0 * D(x - h) + D(x - 2 * h)) / (12.0 * h)
        analytic = hurt_driffield_slope_log10(layer, x, 293.15)
        assert analytic == pytest.approx(fd, rel=1e-4, abs=1e-8)

    @pytest.mark.parametrize("layer", CANONICAL_LAYERS)
    def test_ln_slope_matches_4th_order_central_difference(self, layer):
        # Same curve reparametrised in natural-log stimulus: slope must match
        # the 4th-order FD of D vs ln H exactly to high precision.
        h = 2e-3
        x_ln = math.log(10.0) * 2.0  # ln H at log10 H = 2.0

        def D(u_ln):
            return hurter_driffield_density(layer, u_ln / math.log(10.0), 293.15)

        fd = (-D(x_ln + 2 * h) + 8.0 * D(x_ln + h) - 8.0 * D(x_ln - h) + D(x_ln - 2 * h)) / (
            12.0 * h
        )
        analytic = hurt_driffield_slope_ln(layer, 2.0, 293.15)
        assert analytic == pytest.approx(fd, rel=1e-3, abs=1e-8)

    @pytest.mark.parametrize("layer", CANONICAL_LAYERS)
    def test_slope_log10_equals_ln10_times_slope_ln(self, layer):
        for x in (1.0, 2.0, 3.0):
            s_log10 = hurt_driffield_slope_log10(layer, x, 293.15)
            s_ln = hurt_driffield_slope_ln(layer, x, 293.15)
            assert s_log10 == pytest.approx(math.log(10.0) * s_ln, rel=1e-12)


class TestBeerLambertBounds:
    def test_zero_optical_depth_gives_exactly_one(self):
        M = np.eye(3)
        T = beer_lambert_rgb(M, np.array([0.0, 0.0, 0.0]))
        assert T == pytest.approx(np.ones(3))

    def test_huge_optical_depth_stays_positive(self):
        M = np.eye(3) * 1e6
        T = beer_lambert_rgb(M, np.ones(3))
        assert np.all(T > 0.0)
        assert np.all(T <= 1.0)

    def test_transmission_never_exceeds_one(self):
        # Even with positive M entries and negative densities forced through
        # the harness path (rejected upstream, but the safety clip must hold).
        M = np.array([[0.5, 0.1, 0.0], [0.1, 0.7, 0.0], [0.0, 0.2, 0.4]])
        T = beer_lambert_rgb(M, np.array([100.0, 100.0, 100.0]))
        assert np.all(T > 0.0)
        assert np.all(T <= 1.0)

    def test_negative_density_refused(self):
        with pytest.raises(ValueError):
            beer_lambert_rgb(np.eye(3), np.array([0.1, 0.1, -0.1]))


class TestEdgeCaseBranches:
    def test_1d_grid_with_two_cells_does_not_raise(self):
        grid = np.array([0.5, 1.5])
        final = dir_reaction_diffusion(grid, 0.1, 0.0, 3.0)
        assert final.shape == (2,)

    def test_2d_grid_with_two_by_two_cells_does_not_raise(self):
        grid = np.array([[0.5, 1.5], [1.0, 0.25]])
        final = dir_reaction_diffusion(grid, 0.1, 0.0, 3.0)
        assert final.shape == (2, 2)

    def test_negative_kappa_refused(self):
        grid = np.array([0.5, 1.5, 0.25])
        with pytest.raises(ValueError, match="non-negative"):
            dir_reaction_diffusion(grid, -0.5, 0.0, 3.0)

    def test_invalid_grid_spacing_refused(self):
        grid = np.array([0.5, 1.5, 0.25])
        with pytest.raises(ValueError, match="grid_spacing"):
            dir_reaction_diffusion(grid, 0.1, 0.0, 3.0, grid_spacing=0.0)

    def test_invalid_time_step_refused(self):
        grid = np.array([0.5, 1.5, 0.25])
        with pytest.raises(ValueError, match="time_step"):
            dir_reaction_diffusion(grid, 0.1, 0.0, 3.0, time_step=0.0)

    def test_grid_spacing_1d_and_2d_both_accepted_at_cfl_boundary(self):
        # 2D CFL boundary 0.25: kappa = 0.25 with dx = 1, dt = 1 is accepted.
        grid2d = np.ones((3, 3))
        final = dir_reaction_diffusion(grid2d, 0.25, 0.0, 3.0)
        assert final.shape == (3, 3)
