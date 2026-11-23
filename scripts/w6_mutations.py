#!/usr/bin/env python3
"""Adversarial mutation harness for the Wave 6 science and character modules.

Evidence rule: an invariant that has never been attacked is not evidence.
Each mutation rewrites one shipped guard/formula, re-runs the exact failing
test node IDs, and is credited VALID_KILL **only** if:

* the anchor occurs **exactly once** in the source file and the mutated file
  parses as valid Python (``ast.parse``), and
* pytest exits with code 1 (tests ran and failed), and
* pytest output contains no infrastructure markers (no ``INTERNALERROR``,
  ``ImportError``, ``ModuleNotFoundError``, ``SyntaxError``,
  ``IndentationError``, ``collected 0 items``, ``no tests ran``), and
* at least one expected node ID appears on a ``FAILED <node_id>`` line, and
* the output carries a valid failure effect (``AssertionError``,
  ``ValueError``, ``TypeError`` or ``Failed:``).

Every mutated file is restored in a ``finally`` block: the restored content is
byte-compared (and re-verified by SHA-256) against its pre-mutation digest;
a mismatch aborts the campaign with exit code 4.  The whole campaign must
start green and finish green again.

Usage::

    python scripts/w6_mutations.py          # run every mutation
    python scripts/w6_mutations.py --list

Exit code 0 means "N/N mutants killed".
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCIENCE = ROOT / "src" / "nexus_ai_agent" / "creative" / "science"
CHARACTER = ROOT / "src" / "nexus_ai_agent" / "creative" / "character"

CP1 = "tests/unit/test_w6_cp1_photochemistry_and_pr1.py"
CP2 = "tests/unit/test_w6_cp2_physical_optics_and_pr2.py"
CP3 = "tests/unit/test_w6_cp3_optimal_transport_and_pr3.py"
CP4 = "tests/unit/test_w6_cp4_symplectic_pipeline_and_prs4_5.py"

#: Exact ``FAILED`` node IDs observed when each mutation is applied to the
#: source under test (the kill target-set for each mutation).
CP1_NODES = "tests/unit/test_w6_cp1_photochemistry_and_pr1.py"
CP2_NODES = "tests/unit/test_w6_cp2_physical_optics_and_pr2.py"
CP3_NODES = "tests/unit/test_w6_cp3_optimal_transport_and_pr3.py"
CP4_NODES = "tests/unit/test_w6_cp4_symplectic_pipeline_and_prs4_5.py"


@dataclass(frozen=True)
class Mutation:
    name: str
    path: Path
    old: str
    new: str
    why: str
    expected_node_ids: tuple[str, ...]


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "W6_M01_arrhenius_slope_sign_flip",
        SCIENCE / "photochemical_emulsion.py",
        "(1.0 / temperature - 1.0 / t_ref)",
        "(1.0 / t_ref - 1.0 / temperature)",
        "inverting the Arrhenius direction flips the sign of the slope term",
        (
            f"{CP1_NODES}::TestArrhenius::test_gamma_rises_with_temperature",
            f"{CP1_NODES}::TestArrhenius::test_higher_activation_barrier_amplifies_temperature_response",
            f"{CP1_NODES}::TestArrhenius::test_negative_ea_decreases_with_temperature",
        ),
    ),
    Mutation(
        "W6_M02_hurter_driffield_denominator_sign",
        SCIENCE / "photochemical_emulsion.py",
        "delta / (1.0 + math.exp(clipped))",
        "delta / (1.0 - math.exp(clipped))",
        "flipping the sign of the Hill term inverts the D-logH branch",
        (
            f"{CP1_NODES}::TestHurterDriffield4thOrderFD::test_ln_slope_matches_4th_order_central_difference[layer0]",
            f"{CP1_NODES}::TestHurterDriffield4thOrderFD::test_ln_slope_matches_4th_order_central_difference[layer1]",
            f"{CP1_NODES}::TestHurterDriffield4thOrderFD::test_ln_slope_matches_4th_order_central_difference[layer2]",
            f"{CP1_NODES}::TestHurterDriffield4thOrderFD::test_log10_slope_matches_4th_order_central_difference[layer0]",
            f"{CP1_NODES}::TestHurterDriffield4thOrderFD::test_log10_slope_matches_4th_order_central_difference[layer1]",
            f"{CP1_NODES}::TestHurterDriffield4thOrderFD::test_log10_slope_matches_4th_order_central_difference[layer2]",
            f"{CP1_NODES}::TestHurterDriffield::test_curve_is_monotone_in_stimulus",
            f"{CP1_NODES}::TestHurterDriffield::test_explicit_layer_at_inflection",
            f"{CP1_NODES}::TestHurterDriffield::test_inflection_point_is_midpoint[cyan]",
            f"{CP1_NODES}::TestHurterDriffield::test_inflection_point_is_midpoint[magenta]",
            f"{CP1_NODES}::TestHurterDriffield::test_inflection_point_is_midpoint[yellow]",
        ),
    ),
    Mutation(
        "W6_M03_schwarzschild_lower_bound_removed",
        SCIENCE / "photochemical_emulsion.py",
        "if not PAIR_LOW <= p <= PAIR_HIGH:",
        "if not p <= PAIR_HIGH:",
        "removing the Schwarzschild lower bound admits reciprocity failure",
        (f"{CP1_NODES}::TestSchwarzschildReciprocity::test_invalid_p_refused",),
    ),
    Mutation(
        "W6_M04_dir_reaction_sign_flip",
        SCIENCE / "photochemical_emulsion.py",
        "changed = arr + k * dt * laplacian",
        "changed = arr - k * dt * laplacian",
        "flipping the diffusion sign re-opens the anti-diffusion defect",
        (
            f"{CP1_NODES}::TestDirReactionDiffusion::test_diffusion_is_physical_not_anti_diffusive",
            f"{CP1_NODES}::TestDirReactionDiffusion::test_reaction_term_moves_in_right_direction_and_clips",
        ),
    ),
    Mutation(
        "W6_M05_planck_exponent_sign_flip",
        SCIENCE / "physical_optics_radiometry.py",
        "math.expm1(x)",
        "math.exp(x) + 1.0",
        "using exp(x) + 1.0 as the Planck denominator changes the radiance fundamentally",
        (f"{CP2_NODES}::TestPlanck::test_blackbody_known_answer_value",),
    ),
    Mutation(
        "W6_M06_wien_wavelength_inverse",
        SCIENCE / "physical_optics_radiometry.py",
        "return WIEN_CONSTANT_M_K / temperature_k",
        "return WIEN_CONSTANT_M_K * temperature_k",
        "swapping the Wien displacement law inverts T-law behaviour",
        (f"{CP2_NODES}::TestPlanck::test_wien_peak_wavelength_matches",),
    ),
    Mutation(
        "W6_M07_hg_numerator_sign_flip",
        SCIENCE / "physical_optics_radiometry.py",
        "(1.0 - g**2) / (4.0 * math.pi * denominator)",
        "(1.0 + g**2) / (4.0 * math.pi * denominator)",
        "flipping the Henyey-Greenstein numerator breaks normalization",
        (
            f"{CP2_NODES}::TestHGNormalizationOnUnitSphere::test_unit_sphere_integral_equals_one_to_high_precision[-0.1]",
            f"{CP2_NODES}::TestHGNormalizationOnUnitSphere::test_unit_sphere_integral_equals_one_to_high_precision[-0.5]",
            f"{CP2_NODES}::TestHGNormalizationOnUnitSphere::test_unit_sphere_integral_equals_one_to_high_precision[-0.9]",
            f"{CP2_NODES}::TestHGNormalizationOnUnitSphere::test_unit_sphere_integral_equals_one_to_high_precision[0.1]",
            f"{CP2_NODES}::TestHGNormalizationOnUnitSphere::test_unit_sphere_integral_equals_one_to_high_precision[0.5]",
            f"{CP2_NODES}::TestHGNormalizationOnUnitSphere::test_unit_sphere_integral_equals_one_to_high_precision[0.9]",
            f"{CP2_NODES}::TestHenyeyGreenstein::test_phase_function_integrates_to_one[-0.5]",
            f"{CP2_NODES}::TestHenyeyGreenstein::test_phase_function_integrates_to_one[-0.9]",
            f"{CP2_NODES}::TestHenyeyGreenstein::test_phase_function_integrates_to_one[0.4]",
            f"{CP2_NODES}::TestHenyeyGreenstein::test_phase_function_integrates_to_one[0.7]",
            f"{CP2_NODES}::TestHenyeyGreenstein::test_phase_function_integrates_to_one[0.9]",
        ),
    ),
    Mutation(
        "W6_M08_thin_film_phase_doubling_removed",
        SCIENCE / "physical_optics_radiometry.py",
        "numerator = r01**2 + r12**2 + 2.0 * r01 * r12 * math.cos(2.0 * delta)",
        "numerator = r01**2 + r12**2 + 2.0 * r01 * r12 * math.cos(delta)",
        "dropping the 2*delta factor inverts the interference term",
        (f"{CP2_NODES}::TestThinFilm::test_extreme_amplitude_still_yields_unit_interval",),
    ),
    Mutation(
        "W6_M09_volumetric_decay_sign_flip",
        SCIENCE / "physical_optics_radiometry.py",
        "decay = math.exp(-optical_depth)",
        "decay = math.exp(optical_depth)",
        "inverting the optical decay is an unbounded growth model",
        (
            f"{CP2_NODES}::TestTransfer::test_deep_medium_converges_to_source_function",
            f"{CP2_NODES}::TestTransfer::test_optical_depth_one_g_is_half",
        ),
    ),
    Mutation(
        "W6_M10_critical_angle_asin_acos",
        SCIENCE / "physical_optics_radiometry.py",
        "return float(math.degrees(math.asin(ratio)))",
        "return float(math.degrees(math.acos(ratio)))",
        "flipping asin to acos inverts the critical-angle meaning",
        (f"{CP2_NODES}::TestFresnel::test_tsr_angle_and_total_internal_reflection",),
    ),
    Mutation(
        "W6_M11_matrix_square_root_anti_symmetrize",
        SCIENCE / "optimal_transport_spd.py",
        (
            "root = eigvecs @ np.diag(np.sqrt(eigvals)) @ eigvecs.T\n"
            "    root = np.asarray(0.5 * (root + root.T), dtype=np.float64)"
        ),
        (
            "root = eigvecs @ np.diag(np.sqrt(eigvals)) @ eigvecs.T\n"
            "    root = np.asarray(0.5 * (root - root.T), dtype=np.float64)"
        ),
        "keeping an anti-symmetric root destroys SPD structure",
        (
            f"{CP3_NODES}::TestBuresNegativeClamp::test_identical_gaussians_give_machine_noise_zero_clamped_to_zero",
            f"{CP3_NODES}::TestBuresNegativeClamp::test_identical_with_mean_shift_positive",
            f"{CP3_NODES}::TestBuresWasserstein::test_distance_equals_mean_shift_for_equal_covariances",
            f"{CP3_NODES}::TestBuresWasserstein::test_distance_is_non_negative",
            f"{CP3_NODES}::TestBuresWasserstein::test_distance_is_symmetric",
            f"{CP3_NODES}::TestBuresWasserstein::test_distance_is_zero_for_identical_gaussians",
            f"{CP3_NODES}::TestBuresWasserstein::test_distance_positive_for_scale_step",
            f"{CP3_NODES}::TestBuresWasserstein::test_triangle_inequality_holds_numerically",
            f"{CP3_NODES}::TestEigendecompositionRoots::test_eigenvector_degeneracy_still_symmetric_root",
            f"{CP3_NODES}::TestEigendecompositionRoots::test_inverse_square_root_inverts_root",
            f"{CP3_NODES}::TestEigendecompositionRoots::test_root_square_identity",
            f"{CP3_NODES}::TestMcCannGeodesic::test_geodesic_distance_scales_quadratically",
            f"{CP3_NODES}::TestMcCannGeodesic::test_geodesic_mean_is_linear_interpolation",
            f"{CP3_NODES}::TestMcCannGeodesic::test_t_one_is_target",
            f"{CP3_NODES}::TestMcCannGeodesic::test_t_zero_is_source",
            f"{CP3_NODES}::TestMcCannGeodesicSpotChecks::test_geodesic_at_t_half_matches_endpoints",
            f"{CP3_NODES}::TestMcCannGeodesicSpotChecks::test_geodesic_endpoints_pass_through_source_and_target",
            f"{CP3_NODES}::TestMongeMap::test_affine_map_produces_target_mean",
            f"{CP3_NODES}::TestMongeMap::test_transport_matrix_is_symmetric_positive_definite",
            f"{CP3_NODES}::TestMongeMap::test_transport_matrix_is_target_first_for_isotropic_covariance",
            f"{CP3_NODES}::TestMongeMap::test_transport_matrix_satisfies_covariance_equation",
            f"{CP3_NODES}::TestMongeMapStructuralChecks::test_non_commuting_covariances_satisfy_map_equation",
            f"{CP3_NODES}::TestMongeMapStructuralChecks::test_transport_matrix_is_positive_definite_and_symmetric",
            f"{CP3_NODES}::TestSPDAlgebra::test_square_root_of_identity_is_identity",
            f"{CP3_NODES}::TestSymmetryBeforeSymmetrization::test_repeated_eigenvalues_still_valid",
        ),
    ),
    Mutation(
        "W6_M12_bures_cov_term_sign_flip",
        SCIENCE / "optimal_transport_spd.py",
        "- 2.0 * np.trace(root_product)",
        "+ 2.0 * np.trace(root_product)",
        "flipping the Bures-Wasserstein covariance term sign",
        (
            f"{CP3_NODES}::TestBuresNegativeClamp::test_identical_gaussians_give_machine_noise_zero_clamped_to_zero",
            f"{CP3_NODES}::TestBuresNegativeClamp::test_identical_with_mean_shift_positive",
            f"{CP3_NODES}::TestBuresWasserstein::test_distance_equals_mean_shift_for_equal_covariances",
            f"{CP3_NODES}::TestBuresWasserstein::test_distance_is_zero_for_identical_gaussians",
            f"{CP3_NODES}::TestMcCannGeodesic::test_geodesic_distance_scales_quadratically",
        ),
    ),
    Mutation(
        "W6_M13_monge_transport_construction",
        SCIENCE / "optimal_transport_spd.py",
        "a = c1_inv_half @ root_product @ c1_inv_half",
        "a = c1_inv_half @ c2 @ c1_inv_half",
        "dropping the matrix square root in the Monge construction",
        (
            f"{CP3_NODES}::TestMcCannGeodesic::test_geodesic_distance_scales_quadratically",
            f"{CP3_NODES}::TestMcCannGeodesic::test_geodesic_mean_is_linear_interpolation",
            f"{CP3_NODES}::TestMcCannGeodesic::test_t_one_is_target",
            f"{CP3_NODES}::TestMcCannGeodesic::test_t_zero_is_source",
            f"{CP3_NODES}::TestMcCannGeodesicSpotChecks::test_geodesic_at_t_half_matches_endpoints",
            f"{CP3_NODES}::TestMcCannGeodesicSpotChecks::test_geodesic_endpoints_pass_through_source_and_target",
            f"{CP3_NODES}::TestMongeMap::test_affine_map_produces_target_mean",
            f"{CP3_NODES}::TestMongeMap::test_transport_matrix_is_symmetric_positive_definite",
            f"{CP3_NODES}::TestMongeMap::test_transport_matrix_is_target_first_for_isotropic_covariance",
            f"{CP3_NODES}::TestMongeMap::test_transport_matrix_satisfies_covariance_equation",
            f"{CP3_NODES}::TestMongeMapStructuralChecks::test_non_commuting_covariances_satisfy_map_equation",
            f"{CP3_NODES}::TestMongeMapStructuralChecks::test_transport_matrix_is_positive_definite_and_symmetric",
        ),
    ),
    Mutation(
        "W6_M14_mccann_interpolation_direction_flip",
        SCIENCE / "optimal_transport_spd.py",
        "mean_t = (1.0 - t) * m1 + t * m2",
        "mean_t = t * m1 + (1.0 - t) * m2",
        "interpolating in the wrong direction",
        (
            f"{CP3_NODES}::TestMcCannGeodesic::test_geodesic_distance_scales_quadratically",
            f"{CP3_NODES}::TestMcCannGeodesic::test_geodesic_mean_is_linear_interpolation",
            f"{CP3_NODES}::TestMcCannGeodesic::test_t_one_is_target",
            f"{CP3_NODES}::TestMcCannGeodesic::test_t_zero_is_source",
            f"{CP3_NODES}::TestMcCannGeodesicSpotChecks::test_geodesic_at_t_half_matches_endpoints",
            f"{CP3_NODES}::TestMcCannGeodesicSpotChecks::test_geodesic_endpoints_pass_through_source_and_target",
        ),
    ),
    Mutation(
        "W6_M15_hamiltonian_kinetic_factor",
        SCIENCE / "symplectic_hamiltonian_pipeline.py",
        "kinetic = float(arr_p @ arr_p) / (2.0 * spec.mass)",
        "kinetic = float(arr_p @ arr_p) / spec.mass",
        "dropping the 1/2 kinetic factor doubles the kinetic energy",
        (
            f"{CP4_NODES}::TestExactSymplecticity::test_second_order_convergence_of_energy",
            f"{CP4_NODES}::TestRayleighDampingTheory::test_zero_damping_recovers_symplectic_flow",
            f"{CP4_NODES}::TestSymplectic::test_energy_bounded_without_damping",
            f"{CP4_NODES}::TestSymplectic::test_hamiltonian_known_answer",
            f"{CP4_NODES}::TestSymplectic::test_rayleigh_damping_converges",
        ),
    ),
    Mutation(
        "W6_M16_verlet_half_step_sign_flip",
        SCIENCE / "symplectic_hamiltonian_pipeline.py",
        "p_next = p_half2 - 0.5 * dt * grad_q_next",
        "p_next = p_half2 + 0.5 * dt * grad_q_next",
        "flipping the second half-step sign breaks symplecticity",
        (
            f"{CP4_NODES}::TestAnalyticJacobianFDVerification::test_analytic_matches_fd_central[0.0-0.0]",
            f"{CP4_NODES}::TestAnalyticJacobianFDVerification::test_analytic_matches_fd_central[0.0-0.3]",
            f"{CP4_NODES}::TestAnalyticJacobianFDVerification::test_analytic_matches_fd_central[0.4-0.0]",
            f"{CP4_NODES}::TestAnalyticJacobianFDVerification::test_analytic_matches_fd_central[0.4-0.3]",
            f"{CP4_NODES}::TestExactSymplecticity::test_reversibility_recovers_initial_state",
            f"{CP4_NODES}::TestExactSymplecticity::test_second_order_convergence_of_energy",
            f"{CP4_NODES}::TestRayleighDampingTheory::test_continuous_dH_dt_matches_c_over_m_squared",
            f"{CP4_NODES}::TestRayleighDampingTheory::test_zero_damping_recovers_symplectic_flow",
            f"{CP4_NODES}::TestSymplectic::test_energy_bounded_without_damping",
        ),
    ),
    Mutation(
        "W6_M17_gradient_potential_sign_flip",
        SCIENCE / "symplectic_hamiltonian_pipeline.py",
        "grad = k @ arr_q + spec.quartic_coupling * float(arr_q @ arr_q) * arr_q",
        "grad = k @ arr_q - spec.quartic_coupling * float(arr_q @ arr_q) * arr_q",
        "inverting the quartic gradient term replaces harmonic attraction by repulsion",
        (
            f"{CP4_NODES}::TestAnalyticJacobianFDVerification::test_analytic_matches_fd_central[0.0-0.3]",
            f"{CP4_NODES}::TestAnalyticJacobianFDVerification::test_analytic_matches_fd_central[0.4-0.3]",
            f"{CP4_NODES}::TestExactSymplecticity::test_second_order_convergence_of_energy",
            f"{CP4_NODES}::TestSymplectic::test_energy_bounded_without_damping",
            f"{CP4_NODES}::TestSymplectic::test_gradient_potential_is_k_plus_alpha_coupling",
            f"{CP4_NODES}::TestSymplectic::test_rayleigh_damping_converges",
        ),
    ),
    Mutation(
        "W6_M18_symplectic_volume_error_sign_flip",
        SCIENCE / "symplectic_hamiltonian_pipeline.py",
        "return float(abs(det - 1.0))",
        "return float(abs(det + 1.0))",
        "inverting the volume-preservation error sign makes the check vacuous",
        (f"{CP4_NODES}::TestSymplectic::test_volume_preservation_is_near_exact",),
    ),
    Mutation(
        "W6_M19_identity_version_decrement",
        CHARACTER / "model.py",
        "identity_version=self.identity_version + 1,",
        "identity_version=self.identity_version - 1,",
        "minting identity versions in the wrong direction makes identity versioning meaningless",
        (
            f"{CP4_NODES}::TestCharacterCapsuleSupersession::test_identity_version_is_monotone",
            f"{CP4_NODES}::TestCharacterCapsuleSupersession::test_identity_version_mints_and_bumps_only_identity_fields",
        ),
    ),
    Mutation(
        "W6_M20_continuity_drift_comparison_inverted",
        CHARACTER / "continuity.py",
        "if distance >= threshold.drift_min:",
        "if distance < threshold.drift_min:",
        "inverting the drift comparison marks small distances as drift",
        (
            f"{CP4_NODES}::TestCharacterCapsuleSupersession::test_continuity_verifier_three_state_bands",
        ),
    ),
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _is_valid_kill(
    result: subprocess.CompletedProcess[str], expected_node_ids: tuple[str, ...]
) -> tuple[bool, str]:
    """Five-condition VALID_KILL rule.

    (1) is enforced by the caller (anchor count == 1 + ``ast.parse``).
    (2) pytest exits 1.
    (3) no infrastructure markers in the output.
    (4) at least one expected node ID appears on a ``FAILED`` line.
    (5) the output carries a valid failure effect.
    """
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 1:
        return False, f"pytest exit code {result.returncode} (not 1)"
    lowered = output.lower()
    for marker in (
        "internalerror",
        "importerror",
        "modulenotfounderror",
        "syntaxerror",
        "indentationerror",
        "collected 0 items",
        "no tests ran",
    ):
        if marker in lowered:
            return False, f"infrastructure failure in output ({marker})"
    failed_nodes: set[str] = set()
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("FAILED") and "::" in stripped:
            m = re.search(r"FAILED\s+(\S+)", stripped)
            if m:
                failed_nodes.add(m.group(1))
    matched = [node for node in expected_node_ids if node in failed_nodes]
    if not matched:
        return False, (
            f"none of the expected node IDs failed (matched: {sorted(matched)}; "
            f"failed: {sorted(failed_nodes)})"
        )
    # The valid failure effects (a real test failure, not infrastructure noise).
    # ``ValidationError`` is the character module's own ``ValueError`` subclass:
    # it carries a genuine ValueError failure, so it counts alongside ValueError.
    if not any(
        effect in output
        for effect in ("AssertionError", "ValueError", "TypeError", "Failed:", "ValidationError")
    ):
        return (
            False,
            "no valid failure effect in output (AssertionError/ValueError/TypeError/Failed:)",
        )
    return True, ""


def _run_pytest(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args, "-q", "-o", "addopts="],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=600,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for mutation in MUTATIONS:
            node_count = len(mutation.expected_node_ids)
            print(f"{mutation.name}  ({node_count} expected nodes)  - {mutation.why}")
        return 0

    # sanity: the tree must be green before we start
    for tests in (CP1, CP2, CP3, CP4):
        code = _run_pytest([tests]).returncode
        if code != 0:
            print(f"FATAL: suite {tests} is not green before mutation testing", file=sys.stderr)
            return 2

    killed = 0
    valid_kills: list[str] = []
    survivors: list[str] = []
    restore_failed: str | None = None
    for mutation in MUTATIONS:
        restore_failed = None
        path = mutation.path
        original_bytes = path.read_bytes()
        original_digest = _sha256_bytes(original_bytes)
        original_text = original_bytes.decode("utf-8")
        if original_text.count(mutation.old) != 1:
            print(
                f"  SKIP {mutation.name}: anchor occurs {original_text.count(mutation.old)} times"
            )
            survivors.append(f"{mutation.name} (anchor mismatch)")
            continue
        mutated = original_text.replace(mutation.old, mutation.new, 1)
        try:
            ast.parse(mutated)
        except SyntaxError as exc:
            print(f"  SKIP {mutation.name}: mutation does not parse ({exc})")
            survivors.append(f"{mutation.name} (parse failure)")
            continue
        try:
            path.write_bytes(mutated.encode("utf-8"))
            result = _run_pytest(list(mutation.expected_node_ids))
            ok, reason = _is_valid_kill(result, mutation.expected_node_ids)
            if ok:
                killed += 1
                valid_kills.append(mutation.name)
                print(f"  VALID_KILL {mutation.name}")
            else:
                survivors.append(f"{mutation.name} ({reason})")
                print(f"  SURVIVED {mutation.name}: {reason}")
        finally:
            path.write_bytes(original_bytes)
            restored_bytes = path.read_bytes()
            restored_digest = _sha256_bytes(restored_bytes)
            if restored_bytes != original_bytes or restored_digest != original_digest:
                restore_failed = mutation.name
            else:
                restore_failed = None
        if restore_failed is not None:
            print(f"  FATAL: file not restored for {restore_failed}", file=sys.stderr)
            return 4

    # final: everything must be green again
    for tests in (CP1, CP2, CP3, CP4):
        if _run_pytest([tests]).returncode != 0:
            print("FATAL: suite still red after restore", file=sys.stderr)
            return 3

    total = len(MUTATIONS)
    print(f"\n{killed}/{total} mutants killed (VALID_KILL)")
    if survivors:
        print(f"survivors: {', '.join(survivors)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
