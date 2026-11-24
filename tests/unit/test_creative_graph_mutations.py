"""Targeted AST mutation campaign for Durable Creative-Graph invariants (Law R28).

Verifies that 12 surgical AST mutations attacking canonicalization, strict DAG
acyclicity, Lexicographic Kahn uniqueness, epistemic proof gates, transitive
causal invalidation, append-only hash-chain linkage, boot verification, and
sealed snapshot integrity are ALL killed (0 survived) under strict anti-.pyc
hygiene (``sys.dont_write_bytecode = True``, ``PYTHONDONTWRITEBYTECODE=1``,
``__pycache__`` purge before/after each mutant, and SHA-256-verified byte-exact
restore in ``finally``).
"""

from __future__ import annotations

import ast
import hashlib
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET_FILE = REPO_ROOT / "src" / "nexus_ai_agent" / "provenance" / "graph.py"
TEST_FILE = REPO_ROOT / "tests" / "unit" / "test_creative_graph.py"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _purge_pycache(target: Path) -> None:
    cache_dir = target.parent / "__pycache__"
    if not cache_dir.exists():
        return
    for pyc in cache_dir.glob("*.pyc"):
        pyc.unlink(missing_ok=True)


class _ScopedMutator(ast.NodeTransformer):
    """Apply a single AST transformation inside one named function."""

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
            prev = self._in_scope
            self._in_scope = True
            try:
                return self.generic_visit(node)
            finally:
                self._in_scope = prev
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
    func_name: str
    test_selector: str
    predicate: Callable[[ast.AST], bool]
    transform: Callable[[ast.AST], ast.AST]


def _is_if_unparsing(node: ast.AST, expected_test: str) -> bool:
    return isinstance(node, ast.If) and ast.unparse(node.test) == expected_test


def _weaken_if_to_false(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.If)
    node.test = ast.Constant(value=False)
    return node


def _return_float_instead_of_raising(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.If)
    node.body = [ast.Return(value=ast.Name(id="value", ctx=ast.Load()))]
    return node


def _bypass_verified_proof_gate(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.If)
    node.body = [
        ast.Return(
            value=ast.Tuple(
                elts=[
                    ast.Name(id="resolved_evidence", ctx=ast.Load()),
                    ast.Name(id="proof", ctx=ast.Load()),
                ],
                ctx=ast.Load(),
            )
        )
    ]
    return node


def _is_heappop_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and ast.unparse(node) == "heapq.heappop(heap)"


def _replace_heappop_with_lifo_pop(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.Call)
    return ast.Call(
        func=ast.Attribute(
            value=ast.Name(id="heap", ctx=ast.Load()),
            attr="pop",
            ctx=ast.Load(),
        ),
        args=[],
        keywords=[],
    )


def _is_frontier_advance(node: ast.AST) -> bool:
    return isinstance(node, ast.Assign) and ast.unparse(node) == "frontier = next_frontier"


def _halt_frontier_after_depth_one(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.Assign)
    node.value = ast.Dict(keys=[], values=[])
    return node


MUTATIONS: tuple[MutationSpec, ...] = (
    MutationSpec(
        mutant_id="M01_canonical_allow_float",
        invariant="RFC 8785 JCS canonicalization rejects floats fail-closed",
        func_name="_normalize_recursive",
        test_selector="test_canonical_serialization_refuses_floats_and_invalid_structures",
        predicate=lambda n: _is_if_unparsing(n, "type(value) is float"),
        transform=_return_float_instead_of_raising,
    ),
    MutationSpec(
        mutant_id="M02_dag_allow_self_loop",
        invariant="Strict DAG acyclicity refuses self-loops",
        func_name="_put_edge_tx",
        test_selector="test_strict_dag_refuses_self_loops_and_multihop_cycles",
        predicate=lambda n: _is_if_unparsing(n, "src == dst"),
        transform=_weaken_if_to_false,
    ),
    MutationSpec(
        mutant_id="M03_dag_allow_multihop_cycle",
        invariant="Strict DAG acyclicity refuses multi-hop cycles",
        func_name="_put_edge_tx",
        test_selector="test_strict_dag_refuses_self_loops_and_multihop_cycles",
        predicate=lambda n: _is_if_unparsing(n, "self._path_exists_tx(connection, dst, src)"),
        transform=_weaken_if_to_false,
    ),
    MutationSpec(
        mutant_id="M04_kahn_break_lexicographic_heap",
        invariant="Deterministic Lexicographic Kahn min-heap tie-breaking",
        func_name="_lexicographic_kahn",
        test_selector="test_lexicographic_kahn_and_graph_digest_are_permutation_invariant",
        predicate=_is_heappop_call,
        transform=_replace_heappop_with_lifo_pop,
    ),
    MutationSpec(
        mutant_id="M05_kahn_skip_cycle_check",
        invariant="Topological sort fails closed when raw-SQL cycle exists",
        func_name="_lexicographic_kahn",
        test_selector="test_integrity_detects_all_disk_tampering_classes_and_fails_on_reopen",
        predicate=lambda n: _is_if_unparsing(n, "len(ordered) != len(node_ids)"),
        transform=_weaken_if_to_false,
    ),
    MutationSpec(
        mutant_id="M06_epistemic_skip_measured_evidence_gate",
        invariant="MEASURED_EVIDENCE requires valid evidence_digest",
        func_name="_validate_epistemic_requirements",
        test_selector="test_epistemic_proof_gates_on_nodes_and_edges",
        predicate=lambda n: _is_if_unparsing(
            n,
            "epistemic_status is EpistemicStatus.MEASURED_EVIDENCE and resolved_evidence is None",
        ),
        transform=_weaken_if_to_false,
    ),
    MutationSpec(
        mutant_id="M07_epistemic_skip_verified_proof_gate",
        invariant="VERIFIED_COMMIT requires EpistemicProof",
        func_name="_validate_epistemic_requirements",
        test_selector="test_epistemic_proof_gates_on_nodes_and_edges",
        predicate=lambda n: _is_if_unparsing(n, "proof is None"),
        transform=_bypass_verified_proof_gate,
    ),
    MutationSpec(
        mutant_id="M08_epistemic_skip_signature_verification",
        invariant="VERIFIED_COMMIT binds cryptographic proof signature to subject digest",
        func_name="_validate_epistemic_requirements",
        test_selector="test_epistemic_proof_gates_on_nodes_and_edges",
        predicate=lambda n: _is_if_unparsing(
            n,
            "not verify_epistemic_proof("
            "proof, subject_id=subject_id, subject_digest=subject_digest, "
            "proof_verification_keys=proof_verification_keys, "
            "revoked_proof_key_ids=revoked_proof_key_ids)",
        ),
        transform=_weaken_if_to_false,
    ),
    MutationSpec(
        mutant_id="M09_causal_impact_drop_transitive_frontier",
        invariant="Counterfactual causal impact propagates transitively across multi-hop DAG",
        func_name="_compute_causal_impact_set_conn",
        test_selector="test_compute_causal_impact_set_diamond_and_atomic_propagation",
        predicate=_is_frontier_advance,
        transform=_halt_frontier_after_depth_one,
    ),
    MutationSpec(
        mutant_id="M10_chain_skip_prev_hash_linkage",
        invariant="Append-only hash chain verifies prev_hash continuity",
        func_name="verify_chain",
        test_selector="test_integrity_detects_all_disk_tampering_classes_and_fails_on_reopen",
        predicate=lambda n: _is_if_unparsing(
            n, "not hmac.compare_digest(entry.prev_hash, expected_prev)"
        ),
        transform=_weaken_if_to_false,
    ),
    MutationSpec(
        mutant_id="M11_init_skip_verify_on_open",
        invariant="verify_on_open enforces fail-closed whole-chain verification on reload",
        func_name="__init__",
        test_selector="test_integrity_detects_all_disk_tampering_classes_and_fails_on_reopen",
        predicate=lambda n: _is_if_unparsing(n, "verify_on_open"),
        transform=_weaken_if_to_false,
    ),
    MutationSpec(
        mutant_id="M12_snapshot_skip_seal_verification",
        invariant="Sealed snapshots detect byte tampering via RFC 8785 JCS seal",
        func_name="verify_sealed_snapshot",
        test_selector="test_restart_persistence_in_fresh_subprocess_and_sealed_snapshot",
        predicate=lambda n: _is_if_unparsing(n, "not hmac.compare_digest(actual, seal)"),
        transform=_weaken_if_to_false,
    ),
)


def _apply_mutation(source: str, spec: MutationSpec) -> str:
    tree = ast.parse(source)
    mutator = _ScopedMutator(spec.func_name, spec.predicate, spec.transform)
    mutated_tree = mutator.visit(tree)
    assert mutator.applied == 1, (
        f"Mutation {spec.mutant_id} matched {mutator.applied} nodes "
        f"in {spec.func_name} (expected 1)"
    )
    ast.fix_missing_locations(mutated_tree)
    return ast.unparse(mutated_tree) + "\n"


def test_creative_graph_ast_mutation_campaign_zero_survived() -> None:
    """Every targeted AST mutation on CreativeGraph invariants must be killed."""
    sys.dont_write_bytecode = True
    original_bytes = TARGET_FILE.read_bytes()
    original_sha256 = _sha256_bytes(original_bytes)
    original_source = original_bytes.decode("utf-8")

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT / "src")

    survived: list[str] = []
    killed: list[str] = []

    try:
        for spec in MUTATIONS:
            mutated_source = _apply_mutation(original_source, spec)
            assert mutated_source != original_source, f"{spec.mutant_id} produced identical source"

            _purge_pycache(TARGET_FILE)
            TARGET_FILE.write_text(mutated_source, encoding="utf-8")
            _purge_pycache(TARGET_FILE)

            proc = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "pytest",
                    "-x",
                    "-q",
                    "-o",
                    "addopts=",
                    str(TEST_FILE),
                    "-k",
                    spec.test_selector,
                ],
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if proc.returncode == 0:
                survived.append(f"{spec.mutant_id} ({spec.invariant})")
            else:
                killed.append(spec.mutant_id)
    finally:
        TARGET_FILE.write_bytes(original_bytes)
        _purge_pycache(TARGET_FILE)
        restored_sha256 = _sha256_bytes(TARGET_FILE.read_bytes())
        assert restored_sha256 == original_sha256, (
            f"SHA-256 mismatch after restoring {TARGET_FILE}: "
            f"expected {original_sha256}, got {restored_sha256}"
        )

    assert len(MUTATIONS) >= 12
    assert survived == [], f"Survived mutants ({len(survived)}): {survived}"
    assert len(killed) == len(MUTATIONS)
