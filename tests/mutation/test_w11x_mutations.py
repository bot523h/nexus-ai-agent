"""W11X phase-2 AST mutation campaign: GUM math, adapters, gate and pack wiring.

Twenty-three surgical mutations, each attacking one mathematical invariant of the
JCGM 100:2008 propagation, the three-valued guard band, or the licence/GUM
admission composition. The kill oracle is the strict one used by the
creative-graph campaign: ``KILLED`` requires ``returncode == 1``, at least one
reported test failure, no crash/collection tokens, and a failure message that
carries the expected invariant token.

Hygiene is identical to the other campaigns: ``sys.dont_write_bytecode``,
``PYTHONDONTWRITEBYTECODE=1``, a ``__pycache__`` purge before and after every
mutant, and a SHA-256-verified byte-exact restore in ``finally``.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFICATION_DIR = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "verification"
GUM_FILE = VERIFICATION_DIR / "uncertainty_gum.py"
ADAPTERS_FILE = VERIFICATION_DIR / "adapters.py"
GATE_FILE = VERIFICATION_DIR / "perception_gate.py"
LOUDNESS_FILE = VERIFICATION_DIR / "loudness.py"
OPERATIONS_FILE = REPO_ROOT / "src/nexus_ai_agent/creative/packs/perception/operations.py"
GUM_TESTS = "tests/unit/test_uncertainty_gum.py"
ADAPTER_TESTS = "tests/unit/test_uncertainty_adapters.py"
GATE_TESTS = "tests/unit/test_perception_gate.py"
LOUDNESS_TESTS = "tests/unit/test_loudness_model.py"
RUNTIME_TESTS = "tests/unit/test_perception_gate_runtime.py"

_FORBIDDEN_CRASH_TOKENS: tuple[str, ...] = (
    "SyntaxError",
    "IndentationError",
    "ImportError",
    "ModuleNotFoundError",
    "INTERNALERROR",
    "no tests ran",
)
_FAILED_RE = re.compile(r"\b(\d+)\s+failed\b")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _purge_pycache(target: Path) -> None:
    cache_dir = target.parent / "__pycache__"
    if not cache_dir.exists():
        return
    for pyc in cache_dir.glob("*.pyc"):
        pyc.unlink(missing_ok=True)


class _ScopedMutator(ast.NodeTransformer):
    def __init__(
        self,
        func_name: str,
        predicate: Callable[[ast.AST], bool],
        transform: Callable[[ast.AST], ast.AST],
    ) -> None:
        self._func_name = func_name
        self._predicate = predicate
        self._transform = transform
        self._in_scope = False
        self.applied = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        if node.name == self._func_name:
            previous = self._in_scope
            self._in_scope = True
            try:
                return self.generic_visit(node)
            finally:
                self._in_scope = previous
        return self.generic_visit(node)

    def generic_visit(self, node: ast.AST) -> ast.AST:
        if self._in_scope and self.applied == 0 and self._predicate(node):
            self.applied += 1
            return self._transform(node)
        return super().generic_visit(node)


@dataclass(frozen=True)
class MutationSpec:
    mutant_id: str
    invariant: str
    target_file: Path
    func_name: str
    test_file: str
    test_selector: str
    expected_error_tokens: tuple[str, ...]
    predicate: Callable[[ast.AST], bool]
    transform: Callable[[ast.AST], ast.AST]


def _is_if(node: ast.AST, expected: str) -> bool:
    return isinstance(node, ast.If) and ast.unparse(node.test) == expected


def _is_assign(node: ast.AST, target: str) -> bool:
    return (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == target
    )


def _is_return_starting(node: ast.AST, prefix: str) -> bool:
    return (
        isinstance(node, ast.Return)
        and node.value is not None
        and ast.unparse(node.value).startswith(prefix)
    )


def _is_call_starting(node: ast.AST, prefix: str) -> bool:
    return isinstance(node, ast.Call) and ast.unparse(node).startswith(prefix)


def _weaken_to_false(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.If)
    node.test = ast.Constant(value=False)
    return node


def _weaken_to_true(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.If)
    node.test = ast.Constant(value=True)
    return node


def _zero_first_argument(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.Call)
    node.args[0] = ast.Constant(value=0.0)
    return node


def _drop_cap(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.Return)
    node.value = ast.Name(id="effective", ctx=ast.Load())
    return node


def _force_true(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.Assign)
    node.value = ast.Constant(value=True)
    return node


def _zero_covariance(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.Call)
    replacement = ast.parse(
        "tuple(tuple(0.0 for _line in line) for line in covariance)", mode="eval"
    ).body
    for keyword in node.keywords:
        if keyword.arg == "covariance":
            keyword.value = replacement
            return node
    raise AssertionError("no covariance keyword found")


def _flatten_gradient(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.Return)
    node.value = ast.parse("tuple(0.0 for _value in gradient)", mode="eval").body
    return node


def _swap_direction(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
    node.test.comparators[0] = ast.Constant(value="min")
    return node


def _drop_sample_normalisation(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.Assign)
    node.value = ast.parse("math.sqrt(sample_variance)", mode="eval").body
    return node


def _require_both_histograms(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.If)
    node.test = ast.parse("p_value > 0.0 and q_value > 0.0", mode="eval").body
    return node


def _condition_from_inverse_entries(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.Assign)
    node.value = ast.parse(
        "math.inf if min(inverse_mel, inverse_hem) <= 0.0 "
        "else max(inverse_mel, inverse_hem, abs(inverse_cross)) / min(inverse_mel, inverse_hem)",
        mode="eval",
    ).body
    return node


def _drop_level_term(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.Assign)
    node.value = ast.parse("quantisation_step_value**2 * power", mode="eval").body
    return node


def _restore_sample_formula(node: ast.AST) -> ast.AST:
    """Restore the old, level-blind ``u(M) = sqrt(q^2/(12N))`` sample formula."""
    assert isinstance(node, ast.Return)
    node.value = ast.parse(
        "math.sqrt(quantisation_step_value**2 / (12.0 * samples))", mode="eval"
    ).body
    return node


def _set_constant(node: ast.AST, value: float) -> ast.AST:
    assert isinstance(node, ast.Assign)
    node.value = ast.Constant(value=value)
    return node


def _use_objective(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.Assign)
    node.value = ast.Name(id="objective", ctx=ast.Load())
    return node


MUTATIONS: tuple[MutationSpec, ...] = (
    MutationSpec(
        mutant_id="W01_covariance_ignored",
        invariant="correlated inputs contribute 2 g_i g_j cov_ij",
        target_file=GUM_FILE,
        func_name="combine",
        test_file=GUM_TESTS,
        test_selector="test_correlated_propagation_includes_the_cross_terms",
        expected_error_tokens=("assert", "0.0"),
        predicate=lambda n: _is_call_starting(n, "cross.append("),
        transform=_zero_first_argument,
    ),
    MutationSpec(
        mutant_id="W02_covariance_psd_unchecked",
        invariant="a 3-input covariance with lambda_min < 0 must be refused",
        target_file=GUM_FILE,
        func_name="__post_init__",
        test_file=GUM_TESTS,
        test_selector="test_pairwise_cauchy_schwarz_is_not_enough_for_three_inputs",
        expected_error_tokens=("DID NOT RAISE", "GUMCovarianceError"),
        predicate=lambda n: _is_if(n, "size >= 3 and _gershgorin_floor(matrix) < 0.0"),
        transform=_weaken_to_false,
    ),
    MutationSpec(
        mutant_id="W03_containment_not_strict",
        invariant="only a strictly contained interval passes",
        target_file=GUM_FILE,
        func_name="evaluate_guard_band",
        test_file=GUM_TESTS,
        test_selector="test_interval_touching_the_limit_abstains_instead_of_passing",
        expected_error_tokens=("ABSTAIN", "PASS"),
        predicate=lambda n: _is_assign(n, "inside"),
        transform=_force_true,
    ),
    MutationSpec(
        mutant_id="W04_abstain_becomes_pass",
        invariant="a straddling interval must abstain, never pass",
        target_file=GUM_FILE,
        func_name="evaluate_guard_band",
        test_file=GUM_TESTS,
        test_selector="test_interval_straddling_the_limit_abstains",
        expected_error_tokens=("ABSTAIN", "PASS"),
        predicate=lambda n: _is_if(n, "inside"),
        transform=_weaken_to_true,
    ),
    MutationSpec(
        mutant_id="W05_welch_cap_removed",
        invariant="nu_eff is capped by the sum of the input freedoms",
        target_file=GUM_FILE,
        func_name="welch_satterthwaite",
        test_file=GUM_TESTS,
        test_selector="test_welch_satterthwaite_is_capped_by_the_freedom_sum",
        expected_error_tokens=("assert", "1.0"),
        predicate=lambda n: _is_return_starting(n, "min("),
        transform=_drop_cap,
    ),
    MutationSpec(
        mutant_id="W06_bool_accepted_as_real",
        invariant="bool is not a real number",
        target_file=GUM_FILE,
        func_name="_finite",
        test_file=GUM_TESTS,
        test_selector="test_boolean_estimates_are_rejected",
        expected_error_tokens=("DID NOT RAISE", "GUMInputError"),
        predicate=lambda n: (
            isinstance(n, ast.If) and "isinstance(value, bool)" in ast.unparse(n.test)
        ),
        transform=_weaken_to_false,
    ),
    MutationSpec(
        mutant_id="W07_limits_may_be_empty",
        invariant="at least one specification limit is required",
        target_file=GUM_FILE,
        func_name="__post_init__",
        test_file=GUM_TESTS,
        test_selector="test_limits_require_at_least_one_bound",
        expected_error_tokens=("DID NOT RAISE", "GUMInputError"),
        predicate=lambda n: _is_if(n, "self.lower is None and self.upper is None"),
        transform=_weaken_to_false,
    ),
    MutationSpec(
        mutant_id="W08_cauchy_schwarz_unchecked",
        invariant="a covariance entry may not exceed sqrt(v_i v_j)",
        target_file=GUM_FILE,
        func_name="__post_init__",
        test_file=GUM_TESTS,
        test_selector="test_cauchy_schwarz_violation_is_rejected",
        expected_error_tokens=("DID NOT RAISE", "GUMCovarianceError"),
        predicate=lambda n: isinstance(n, ast.If) and "_tolerance(bound)" in ast.unparse(n.test),
        transform=_weaken_to_false,
    ),
    MutationSpec(
        mutant_id="W09_covariance_zeroed",
        invariant="from_covariance preserves the supplied off-diagonal terms",
        target_file=GUM_FILE,
        func_name="from_covariance",
        test_file=GUM_TESTS,
        test_selector="test_correlated_propagation_includes_the_cross_terms",
        expected_error_tokens=("assert", "0.0"),
        predicate=lambda n: _is_call_starting(n, "cls("),
        transform=_zero_covariance,
    ),
    MutationSpec(
        mutant_id="W10_threshold_direction_swapped",
        invariant="'max' gates the upper limit and 'min' the lower one",
        target_file=GUM_FILE,
        func_name="evaluate_threshold",
        test_file=GUM_TESTS,
        test_selector="test_threshold_lower_direction_passes_and_fails",
        expected_error_tokens=("assert", "PASS", "FAIL"),
        predicate=lambda n: _is_if(n, "direction == 'max'"),
        transform=_swap_direction,
    ),
    MutationSpec(
        mutant_id="W11_loudness_not_sample_averaged",
        invariant="quantisation noise shrinks as 1/sqrt(N)",
        target_file=LOUDNESS_FILE,
        func_name="mean_square_deviation",
        test_file=LOUDNESS_TESTS,
        test_selector="test_the_mean_square_deviation_shrinks_as_one_over_sqrt_samples",
        expected_error_tokens=("assert",),
        predicate=lambda n: _is_assign(n, "head"),
        transform=_drop_level_term,
    ),
    MutationSpec(
        mutant_id="W12_js_gradient_flat",
        invariant="the JS divergence gradient is non-zero for unequal histograms",
        target_file=ADAPTERS_FILE,
        func_name="_js_gradient",
        test_file=ADAPTER_TESTS,
        test_selector="test_the_scene_cut_budget_matches_the_closed_form_two_block_propagation",
        expected_error_tokens=("assert",),
        predicate=lambda n: _is_return_starting(n, "tuple(gradient"),
        transform=_flatten_gradient,
    ),
    MutationSpec(
        mutant_id="W13_divergence_crosscheck_skipped",
        invariant="the reported divergence must match the scored histograms",
        target_file=ADAPTERS_FILE,
        func_name="scene_cut_uncertainty",
        test_file=ADAPTER_TESTS,
        test_selector="test_scene_cut_uncertainty_cross_checks_the_reported_divergence",
        expected_error_tokens=("DID NOT RAISE", "GUMInputError"),
        predicate=lambda n: _is_if(n, "reported_divergence is not None"),
        transform=_weaken_to_false,
    ),
    MutationSpec(
        mutant_id="W14_burn_allowance_wrong",
        invariant="burn rate is p / (1 - target), the engine's own law",
        target_file=ADAPTERS_FILE,
        func_name="slo_burn_uncertainty",
        test_file=ADAPTER_TESTS,
        test_selector="test_slo_burn_uncertainty_matches_the_binomial_model",
        expected_error_tokens=("GUMInputError", "disagrees"),
        predicate=lambda n: _is_assign(n, "allowance"),
        transform=_use_objective,
    ),
    MutationSpec(
        mutant_id="W15_singular_gramian_unguarded",
        invariant="a singular extinction Gramian fails closed",
        target_file=ADAPTERS_FILE,
        func_name="_gramian_inverse",
        test_file=ADAPTER_TESTS,
        test_selector=("test_a_parallel_gramian_fails_closed_before_the_condition_is_reported"),
        expected_error_tokens=("ZeroDivisionError", "GUMInputError"),
        predicate=lambda n: _is_if(n, "determinant <= 0.0"),
        transform=_weaken_to_false,
    ),
    MutationSpec(
        mutant_id="W16_js_bin_guard_restored",
        invariant="a bin populated in only one histogram still contributes",
        target_file=ADAPTERS_FILE,
        func_name="_js_bin_contribution",
        test_file=ADAPTER_TESTS,
        test_selector="test_disjoint_histograms_are_maximally_different",
        expected_error_tokens=("assert",),
        predicate=lambda n: _is_if(n, "q_value > 0.0"),
        transform=_require_both_histograms,
    ),
    MutationSpec(
        mutant_id="W17_after_block_dropped",
        invariant="the after-histogram's sampling noise is propagated too",
        target_file=ADAPTERS_FILE,
        func_name="_js_gradient_after",
        test_file=ADAPTER_TESTS,
        test_selector="test_the_scene_cut_budget_matches_the_closed_form_two_block_propagation",
        expected_error_tokens=("assert",),
        predicate=lambda n: _is_return_starting(n, "tuple(gradient"),
        transform=_flatten_gradient,
    ),
    MutationSpec(
        mutant_id="W18_loudness_sample_formula_restored",
        invariant="u(M) = sqrt(q^2 M/(3N) + q^4/(180N)), not the level-blind sqrt(q^2/(12N))",
        target_file=LOUDNESS_FILE,
        func_name="mean_square_deviation",
        test_file=LOUDNESS_TESTS,
        test_selector="test_the_mean_square_deviation_has_the_level_term_the_sample_model_lacked",
        expected_error_tokens=("assert",),
        predicate=lambda n: _is_return_starting(n, "math.sqrt(math.fsum"),
        transform=_restore_sample_formula,
    ),
    MutationSpec(
        mutant_id="W19_condition_from_inverse_entries",
        invariant="kappa_2 is lambda_max/lambda_min of the Gramian, not of its inverse",
        target_file=ADAPTERS_FILE,
        func_name="chromophore_uncertainty",
        test_file=ADAPTER_TESTS,
        test_selector="test_the_chromophore_condition_number_is_the_spectral_ratio",
        expected_error_tokens=("assert",),
        predicate=lambda n: _is_assign(n, "condition"),
        transform=_condition_from_inverse_entries,
    ),
    MutationSpec(
        mutant_id="W20_gate_license_refusal_ignored",
        invariant="an incompatible license supremum refuses before the gain",
        target_file=GATE_FILE,
        func_name="gate_loudness",
        test_file=GATE_TESTS,
        test_selector="test_a_license_conflict_refuses_before_the_gain_is_considered",
        expected_error_tokens=("assert", "license_conflict"),
        predicate=lambda n: _is_if(n, "combination is not None and (not combination.compatible)"),
        transform=_weaken_to_false,
    ),
    MutationSpec(
        mutant_id="W21_gate_abstain_ignored",
        invariant="a straddling interval abstains instead of publishing a gain",
        target_file=GATE_FILE,
        func_name="gate_loudness",
        test_file=GATE_TESTS,
        test_selector="test_a_straddling_measurement_abstains_instead_of_guessing",
        expected_error_tokens=("assert",),
        predicate=lambda n: _is_if(
            n, "decision.outcome is VerificationOutcome.ABSTAIN_INSUFFICIENT_EVIDENCE"
        ),
        transform=_weaken_to_false,
    ),
    MutationSpec(
        mutant_id="W22_marker_refusal_ignored",
        invariant="a refused admission publishes no markers",
        target_file=OPERATIONS_FILE,
        func_name="_mark_silences",
        test_file=RUNTIME_TESTS,
        test_selector="test_a_license_conflict_publishes_no_markers_through_the_runtime",
        expected_error_tokens=("assert", "markers_added"),
        predicate=lambda n: _is_if(n, "admission.get('admitted') is False"),
        transform=_weaken_to_false,
    ),
    MutationSpec(
        mutant_id="W23_loudness_sensitivity_dropped",
        invariant="u(L) = (10/ln10) u(M)/M carries the level through the sensitivity",
        target_file=LOUDNESS_FILE,
        func_name="loudness_budget",
        test_file=LOUDNESS_TESTS,
        test_selector="test_the_budget_is_the_propagation_of_the_mean_square_deviation",
        expected_error_tokens=("assert",),
        predicate=lambda n: _is_assign(n, "sensitivity"),
        transform=lambda n: _set_constant(n, 1.0),
    ),
)


def _apply_mutation(source: str, spec: MutationSpec) -> str:
    tree = ast.parse(source)
    mutator = _ScopedMutator(spec.func_name, spec.predicate, spec.transform)
    mutated = mutator.visit(tree)
    assert mutator.applied == 1, (
        f"Mutation {spec.mutant_id} matched {mutator.applied} nodes "
        f"in {spec.func_name} (expected 1)"
    )
    ast.fix_missing_locations(mutated)
    return ast.unparse(mutated) + "\n"


def _classify(spec: MutationSpec, returncode: int, output: str) -> tuple[str, str]:
    if returncode == 0:
        return "SURVIVED", "pytest exited 0 (mutant not detected)"
    if returncode != 1:
        return "INVALID", f"unexpected pytest exit code {returncode}"
    for forbidden in _FORBIDDEN_CRASH_TOKENS:
        if forbidden in output:
            return "INVALID", f"non-assertion crash token {forbidden!r}"
    failed = _FAILED_RE.search(output)
    if failed is None or int(failed.group(1)) < 1:
        return "INVALID", "pytest exited 1 without reporting a failed test"
    matched = [token for token in spec.expected_error_tokens if token in output]
    if not matched:
        return "INVALID", f"failure output lacked {spec.expected_error_tokens}"
    return "KILLED", f"rc=1, matched={matched[0]!r}"


def test_w11x_mutation_campaign_zero_survived() -> None:
    """Every wave-5 uncertainty mutation must be validly killed."""
    sys.dont_write_bytecode = True
    originals = {spec.target_file for spec in MUTATIONS}
    sources = {path: path.read_bytes() for path in originals}
    digests = {path: _sha256_bytes(data) for path, data in sources.items()}

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT / "src")

    killed: list[str] = []
    survived: list[str] = []
    invalid: list[str] = []

    try:
        for spec in MUTATIONS:
            original_source = sources[spec.target_file].decode("utf-8")
            mutated_source = _apply_mutation(original_source, spec)
            assert mutated_source != original_source, f"{spec.mutant_id} was a no-op"

            _purge_pycache(spec.target_file)
            spec.target_file.write_text(mutated_source, encoding="utf-8")
            _purge_pycache(spec.target_file)

            proc = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "pytest",
                    "-x",
                    "--tb=short",
                    "-o",
                    "addopts=",
                    spec.test_file,
                    "-k",
                    spec.test_selector,
                ],
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            verdict, detail = _classify(spec, proc.returncode, proc.stdout + "\n" + proc.stderr)
            if verdict == "KILLED":
                killed.append(f"{spec.mutant_id} ({detail})")
            elif verdict == "SURVIVED":
                survived.append(f"{spec.mutant_id}: {detail}")
            else:
                invalid.append(f"{spec.mutant_id}: {detail}")
    finally:
        for path, data in sources.items():
            path.write_bytes(data)
            _purge_pycache(path)
            assert _sha256_bytes(path.read_bytes()) == digests[path], (
                f"SHA-256 mismatch after restoring {path}"
            )

    # The pin is deliberate: a silently dropped mutant would otherwise look like
    # a clean campaign. Raise it whenever a mutant is added, never lower it.
    assert len(MUTATIONS) == 23
    assert invalid == [], f"Invalid mutants ({len(invalid)}): {invalid}"
    assert survived == [], f"Survived mutants ({len(survived)}): {survived}"
    assert len(killed) == len(MUTATIONS)
