#!/usr/bin/env python3
"""Adversarial mutation harness for the Wave 6 science modules.

Evidence rule: an invariant that has never been attacked is not evidence.  Each
mutation rewrites one shipped guard/formula, re-runs the targeted suite, and
requires it to turn RED.  Every source file is restored afterwards — including
on failure — and the final run must be GREEN again.

All mutations are verified to parse as valid Python and to occur exactly once in
the target file before they are applied (AST-verified string rewrites).

Usage::

    python scripts/w6_mutations.py          # run every mutation
    python scripts/w6_mutations.py --list

Exit code 0 means "N/N mutants killed".
"""

from __future__ import annotations

import argparse
import ast
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


@dataclass(frozen=True)
class Mutation:
    name: str
    path: Path
    old: str
    new: str
    why: str
    tests: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "W6_M01_arrhenius_slope_sign_flip",
        SCIENCE / "photochemical_emulsion.py",
        "(1.0 / temperature - 1.0 / t_ref)",
        "(1.0 / t_ref - 1.0 / temperature)",
        "inverting the Arrhenius direction flips the sign of the slope term",
        CP1,
    ),
    Mutation(
        "W6_M02_hurter_driffield_denominator_sign",
        SCIENCE / "photochemical_emulsion.py",
        "delta / (1.0 + math.exp(clipped))",
        "delta / (1.0 - math.exp(clipped))",
        "flipping the sign of the Hill term inverts the D-logH branch",
        CP1,
    ),
    Mutation(
        "W6_M03_schwarzschild_lower_bound_removed",
        SCIENCE / "photochemical_emulsion.py",
        "if not PAIR_LOW <= p <= PAIR_HIGH:",
        "if not p <= PAIR_HIGH:",
        "removing the Schwarzschild lower bound admits reciprocity failure",
        CP1,
    ),
    Mutation(
        "W6_M04_dir_reaction_sign_flip",
        SCIENCE / "photochemical_emulsion.py",
        "changed = arr - kappa * laplacian",
        "changed = arr + kappa * laplacian",
        "inverting the reaction term excites rather than quenches structure",
        CP1,
    ),
    Mutation(
        "W6_M05_planck_exponent_sign_flip",
        SCIENCE / "physical_optics_radiometry.py",
        "(math.exp(x) - 1.0)",
        "(math.exp(x) + 1.0)",
        "inverting the Planck denominator changes the radiance fundamentally",
        CP2,
    ),
    Mutation(
        "W6_M06_wien_wavelength_inverse",
        SCIENCE / "physical_optics_radiometry.py",
        "return WIEN_CONSTANT_M_K / temperature_k",
        "return WIEN_CONSTANT_M_K * temperature_k",
        "swapping the Wien displacement law inverts T-law behaviour",
        CP2,
    ),
    Mutation(
        "W6_M07_hg_numerator_sign_flip",
        SCIENCE / "physical_optics_radiometry.py",
        "(1.0 - g**2) / (4.0 * math.pi * denominator)",
        "(1.0 + g**2) / (4.0 * math.pi * denominator)",
        "flipping the Henyey-Greenstein numerator breaks normalization",
        CP2,
    ),
    Mutation(
        "W6_M08_thin_film_phase_doubling_removed",
        SCIENCE / "physical_optics_radiometry.py",
        "numerator = r01**2 + r12**2 + 2.0 * r01 * r12 * math.cos(2.0 * delta)",
        "numerator = r01**2 + r12**2 + 2.0 * r01 * r12 * math.cos(delta)",
        "dropping the 2*delta factor inverts the interference term",
        CP2,
    ),
    Mutation(
        "W6_M09_volumetric_decay_sign_flip",
        SCIENCE / "physical_optics_radiometry.py",
        "decay = math.exp(-optical_depth)",
        "decay = math.exp(optical_depth)",
        "inverting the optical decay is an unbounded growth model",
        CP2,
    ),
    Mutation(
        "W6_M10_critical_angle_asin_acos",
        SCIENCE / "physical_optics_radiometry.py",
        "return float(math.degrees(math.asin(ratio)))",
        "return float(math.degrees(math.acos(ratio)))",
        "flipping asin to acos inverts the critical-angle meaning",
        CP2,
    ),
    Mutation(
        "W6_M11_matrix_square_root_anti_symmetrize",
        SCIENCE / "optimal_transport_spd.py",
        "return np.asarray(0.5 * (root + root.T), dtype=np.float64)",
        "return np.asarray(0.5 * (root - root.T), dtype=np.float64)",
        "keeping an anti-symmetric root destroys SPD structure",
        CP3,
    ),
    Mutation(
        "W6_M12_bures_cov_term_sign_flip",
        SCIENCE / "optimal_transport_spd.py",
        "- 2.0 * np.trace(root_product)",
        "+ 2.0 * np.trace(root_product)",
        "flipping the Bures-Wasserstein covariance term sign",
        CP3,
    ),
    Mutation(
        "W6_M13_monge_transport_construction",
        SCIENCE / "optimal_transport_spd.py",
        "a = c1_inv_half @ root_product @ c1_inv_half",
        "a = c1_inv_half @ c2 @ c1_inv_half",
        "dropping the matrix square root in the Monge construction",
        CP3,
    ),
    Mutation(
        "W6_M14_mccann_interpolation_direction_flip",
        SCIENCE / "optimal_transport_spd.py",
        "mean_t = (1.0 - t) * m1 + t * m2",
        "mean_t = t * m1 + (1.0 - t) * m2",
        "interpolating in the wrong direction",
        CP3,
    ),
    Mutation(
        "W6_M15_hamiltonian_kinetic_factor",
        SCIENCE / "symplectic_hamiltonian_pipeline.py",
        "kinetic = float(arr_p @ arr_p) / (2.0 * spec.mass)",
        "kinetic = float(arr_p @ arr_p) / spec.mass",
        "dropping the 1/2 kinetic factor doubles the kinetic energy",
        CP4,
    ),
    Mutation(
        "W6_M16_verlet_half_step_sign_flip",
        SCIENCE / "symplectic_hamiltonian_pipeline.py",
        "p_half = arr_p - 0.5 * dt * (grad_q + drag)",
        "p_half = arr_p + 0.5 * dt * (grad_q + drag)",
        "flipping the half-step sign breaks symplecticity",
        CP4,
    ),
    Mutation(
        "W6_M17_gradient_potential_sign_flip",
        SCIENCE / "symplectic_hamiltonian_pipeline.py",
        "grad = k @ arr_q + spec.quartic_coupling * float(arr_q @ arr_q) * arr_q",
        "grad = k @ arr_q - spec.quartic_coupling * float(arr_q @ arr_q) * arr_q",
        "inverting the quartic gradient term replaces harmonic attraction by repulsion",
        CP4,
    ),
    Mutation(
        "W6_M18_symplectic_volume_error_sign_flip",
        SCIENCE / "symplectic_hamiltonian_pipeline.py",
        "return float(abs(det - 1.0))",
        "return float(abs(det + 1.0))",
        "inverting the volume-preservation error sign makes the check vacuous",
        CP4,
    ),
    Mutation(
        "W6_M19_identity_version_decrement",
        CHARACTER / "model.py",
        "identity_version=self.identity_version + 1,",
        "identity_version=self.identity_version - 1,",
        "minting identity versions in the wrong direction makes identity versioning meaningless",
        CP4,
    ),
    Mutation(
        "W6_M20_continuity_drift_comparison_inverted",
        CHARACTER / "continuity.py",
        "if distance >= threshold.drift_min:",
        "if distance < threshold.drift_min:",
        "inverting the drift comparison marks small distances as drift",
        CP4,
    ),
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _run_suite(tests: str) -> int:
    result = subprocess.run(
        [".venv/bin/python", "-m", "pytest", tests, "-q"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for mutation in MUTATIONS:
            print(f"{mutation.name}  ({mutation.tests})  - {mutation.why}")
        return 0

    # sanity: the tree must be green before we start
    for tests in (CP1, CP2, CP3, CP4):
        code = _run_suite(tests)
        if code != 0:
            print(f"FATAL: suite {tests} is not green before mutation testing", file=sys.stderr)
            return 2

    killed = 0
    survivors: list[str] = []
    for mutation in MUTATIONS:
        original = _read(mutation.path)
        if original.count(mutation.old) != 1:
            print(f"  SKIP {mutation.name}: anchor occurs {original.count(mutation.old)} times")
            survivors.append(f"{mutation.name} (anchor mismatch)")
            continue
        mutated = original.replace(mutation.old, mutation.new, 1)
        try:
            ast.parse(mutated)
        except SyntaxError as exc:
            print(f"  SKIP {mutation.name}: mutation does not parse ({exc})")
            survivors.append(f"{mutation.name} (parse failure)")
            continue
        try:
            mutation.path.write_text(mutated, encoding="utf-8")
            code = _run_suite(mutation.tests)
            if code != 0:
                killed += 1
                print(f"  KILLED {mutation.name} ({mutation.tests})")
            else:
                survivors.append(mutation.name)
                print(f"  SURVIVED {mutation.name} ({mutation.tests})")
        finally:
            mutation.path.write_text(original, encoding="utf-8")

    # final: everything must be green again
    for tests in (CP1, CP2, CP3, CP4):
        if _run_suite(tests) != 0:
            print("FATAL: suite still red after restore", file=sys.stderr)
            return 3

    total = len(MUTATIONS)
    print(f"\n{killed}/{total} mutants killed")
    if survivors:
        print(f"survivors: {', '.join(survivors)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
