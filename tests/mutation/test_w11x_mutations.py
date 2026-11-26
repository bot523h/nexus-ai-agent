"""W11X wave-5 AST mutation campaign: GUM uncertainty, adapters and admission gate.

Fifteen surgical mutations, each attacking one mathematical invariant of the
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
GUM_TESTS = "tests/unit/test_uncertainty_gum.py"
ADAPTER_TESTS = "tests/unit/test_uncertainty_adapters.py"

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
        mutant_id="W02_negative_variance_unguarded",
        invariant="a materially negative g^T Sigma g raises GUMCovarianceError",
        target_file=GUM_FILE,
        func_name="combine",
        test_file=GUM_TESTS,
        test_selector="test_materially_negative_variance_is_a_covariance_error",
        expected_error_tokens=("DID NOT RAISE", "GUMCovarianceError", "math domain"),
        predicate=lambda n: _is_if(n, "variance < 0.0"),
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
        predicate=lambda n: isinstance(n, ast.Return) and ast.unparse(n.value).startswith("min("),
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
        target_file=ADAPTERS_FILE,
        func_name="loudness_uncertainty",
        test_file=ADAPTER_TESTS,
        test_selector="test_loudness_uncertainty_is_positive_and_shrinks_with_more_samples",
        expected_error_tokens=("assert",),
        predicate=lambda n: _is_assign(n, "mean_square_deviation"),
        transform=_drop_sample_normalisation,
    ),
    MutationSpec(
        mutant_id="W12_js_gradient_flat",
        invariant="the JS divergence gradient is non-zero for unequal histograms",
        target_file=ADAPTERS_FILE,
        func_name="_js_gradient",
        test_file=ADAPTER_TESTS,
        test_selector="test_scene_cut_uncertainty_shrinks_with_more_pixels",
        expected_error_tokens=("assert", "0.0"),
        predicate=lambda n: (
            isinstance(n, ast.Return) and ast.unparse(n.value).startswith("tuple(gradient")
        ),
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
        test_selector="test_chromophore_uncertainty_detects_parallel_extinction_curves",
        expected_error_tokens=("ZeroDivisionError", "GUMInputError"),
        predicate=lambda n: _is_if(n, "determinant <= 0.0"),
        transform=_weaken_to_false,
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

    assert len(MUTATIONS) == 15
    assert invalid == [], f"Invalid mutants ({len(invalid)}): {invalid}"
    assert survived == [], f"Survived mutants ({len(survived)}): {survived}"
    assert len(killed) == len(MUTATIONS)
