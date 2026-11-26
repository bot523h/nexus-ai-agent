"""Targeted AST mutation campaign for Durable Creative-Graph invariants (Law R28).

Verifies that 20 surgical AST mutations attacking canonicalization, strict DAG
acyclicity, Lexicographic Kahn uniqueness, epistemic proof gates, keyed HMAC
key revocation, transitive causal invalidation, terminal state absorption,
source journal chain validation, logical backup restore checks, historical row
auditing, append-only hash-chain linkage, boot verification, and sealed
snapshot integrity — plus the W11X CP-1 hardening (read-only restore source, terminal
epistemic promotion guard, revision_count/chain parity GRAPH034) — are ALL validly killed
(0 survived, 0 invalid) under strict
anti-.pyc hygiene (``sys.dont_write_bytecode = True``,
``PYTHONDONTWRITEBYTECODE=1``, ``__pycache__`` purge before/after each mutant,
and SHA-256-verified byte-exact restore in ``finally``).

Strict kill oracle (F06):
A mutant is classified as ``KILLED`` ONLY when:
1. ``proc.returncode == 1`` (exact pytest test failure; never 2/3/4/5 or crash);
2. pytest collected >= 1 test and reported at least one failed test;
3. stdout/stderr contains zero ``SyntaxError``, ``IndentationError``,
   ``ImportError``, ``ModuleNotFoundError``, or ``INTERNALERROR``; and
4. at least one expected invariant failure token appears in the failure output.
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
TARGET_FILE = REPO_ROOT / "src" / "nexus_ai_agent" / "provenance" / "graph.py"
TEST_FILE = REPO_ROOT / "tests" / "unit" / "test_creative_graph.py"

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
    expected_error_tokens: tuple[str, ...]
    predicate: Callable[[ast.AST], bool]
    transform: Callable[[ast.AST], ast.AST]


def _is_if_unparsing(node: ast.AST, expected_test: str) -> bool:
    return isinstance(node, ast.If) and ast.unparse(node.test) == expected_test


def _weaken_if_to_false(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.If)
    node.test = ast.Constant(value=False)
    return node


def _downgrade_readonly_uri(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.Constant) and node.value == "?mode=ro"
    node.value = "?mode=rwc"
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


def _drop_revoked_key_check(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.If)
    node.test = ast.Compare(
        left=ast.Name(id="proof_verification_keys", ctx=ast.Load()),
        ops=[ast.Is()],
        comparators=[ast.Constant(value=None)],
    )
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
        expected_error_tokens=("DID NOT RAISE", "CanonicalValueError"),
        predicate=lambda n: _is_if_unparsing(n, "type(value) is float"),
        transform=_return_float_instead_of_raising,
    ),
    MutationSpec(
        mutant_id="M02_dag_allow_self_loop",
        invariant="Strict DAG acyclicity refuses self-loops",
        func_name="_put_edge_tx",
        test_selector="test_strict_dag_refuses_self_loops_and_multihop_cycles",
        expected_error_tokens=("DID NOT RAISE", "GraphCycleError"),
        predicate=lambda n: _is_if_unparsing(n, "src == dst"),
        transform=_weaken_if_to_false,
    ),
    MutationSpec(
        mutant_id="M03_dag_allow_multihop_cycle",
        invariant="Strict DAG acyclicity refuses multi-hop cycles",
        func_name="_put_edge_tx",
        test_selector="test_strict_dag_refuses_self_loops_and_multihop_cycles",
        expected_error_tokens=("DID NOT RAISE", "GraphCycleError"),
        predicate=lambda n: _is_if_unparsing(n, "self._path_exists_tx(connection, dst, src)"),
        transform=_weaken_if_to_false,
    ),
    MutationSpec(
        mutant_id="M04_kahn_break_lexicographic_heap",
        invariant="Deterministic Lexicographic Kahn min-heap tie-breaking",
        func_name="_lexicographic_kahn",
        test_selector="test_lexicographic_kahn_and_graph_digest_are_permutation_invariant",
        expected_error_tokens=("AssertionError", "min(ready)"),
        predicate=_is_heappop_call,
        transform=_replace_heappop_with_lifo_pop,
    ),
    MutationSpec(
        mutant_id="M05_kahn_skip_cycle_check",
        invariant="Topological sort fails closed when raw-SQL cycle exists",
        func_name="_lexicographic_kahn",
        test_selector="test_integrity_detects_all_disk_tampering_classes_and_fails_on_reopen",
        expected_error_tokens=("AssertionError", "GRAPH040", "verify_acyclic"),
        predicate=lambda n: _is_if_unparsing(n, "len(ordered) != len(node_ids)"),
        transform=_weaken_if_to_false,
    ),
    MutationSpec(
        mutant_id="M06_epistemic_skip_measured_evidence_gate",
        invariant="MEASURED_EVIDENCE requires valid evidence_digest",
        func_name="_validate_epistemic_requirements",
        test_selector="test_epistemic_proof_gates_on_nodes_and_edges",
        expected_error_tokens=("DID NOT RAISE", "EpistemicProofError"),
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
        expected_error_tokens=("DID NOT RAISE", "EpistemicProofError"),
        predicate=lambda n: _is_if_unparsing(n, "proof is None"),
        transform=_bypass_verified_proof_gate,
    ),
    MutationSpec(
        mutant_id="M08_epistemic_skip_signature_verification",
        invariant="VERIFIED_COMMIT binds keyed HMAC-SHA256 proof to subject digest",
        func_name="_validate_epistemic_requirements",
        test_selector="test_epistemic_proof_gates_on_nodes_and_edges",
        expected_error_tokens=("DID NOT RAISE", "EpistemicProofError"),
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
        expected_error_tokens=("AssertionError", "impacted_node_ids", "affected_count"),
        predicate=_is_frontier_advance,
        transform=_halt_frontier_after_depth_one,
    ),
    MutationSpec(
        mutant_id="M10_chain_skip_prev_hash_linkage",
        invariant="Append-only hash chain verifies prev_hash continuity",
        func_name="_verify_chain_conn",
        test_selector="test_integrity_detects_all_disk_tampering_classes_and_fails_on_reopen",
        expected_error_tokens=("AssertionError", "GRAPH050"),
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
        expected_error_tokens=("DID NOT RAISE", "GraphIntegrityError"),
        predicate=lambda n: _is_if_unparsing(n, "verify_on_open"),
        transform=_weaken_if_to_false,
    ),
    MutationSpec(
        mutant_id="M12_snapshot_skip_seal_verification",
        invariant="Sealed snapshots detect byte tampering via RFC 8785 JCS seal",
        func_name="verify_sealed_snapshot",
        test_selector="test_restart_persistence_in_fresh_subprocess_and_sealed_snapshot",
        expected_error_tokens=("DID NOT RAISE", "GraphIntegrityError"),
        predicate=lambda n: _is_if_unparsing(n, "not hmac.compare_digest(actual, seal)"),
        transform=_weaken_if_to_false,
    ),
    MutationSpec(
        mutant_id="M13_fsm_allow_terminal_resurrection",
        invariant="F01: Absorbing terminal state matrix refuses resurrecting terminal nodes",
        func_name="_set_status_tx",
        test_selector="test_terminal_state_resurrection_and_reassignment_refused",
        expected_error_tokens=("DID NOT RAISE", "GraphStateTransitionError"),
        predicate=lambda n: _is_if_unparsing(n, "status not in allowed_targets"),
        transform=_weaken_if_to_false,
    ),
    MutationSpec(
        mutant_id="M14_journal_skip_source_chain_validation",
        invariant="F03: rebuild_from_journal verifies source journal hash chain before commit",
        func_name="_validate_source_journal_records",
        test_selector="test_rebuild_from_journal_refuses_tampered_or_malformed_source_chain",
        expected_error_tokens=("DID NOT RAISE", "GraphIntegrityError"),
        predicate=lambda n: _is_if_unparsing(n, "not verdict.ok"),
        transform=_weaken_if_to_false,
    ),
    MutationSpec(
        mutant_id="M15_epistemic_skip_revoked_key_check",
        invariant="F08: Revoked HMAC key IDs are refused fail-closed",
        func_name="_resolve_verification_key",
        test_selector="test_epistemic_proof_gates_on_nodes_and_edges",
        expected_error_tokens=("DID NOT RAISE", "EpistemicProofError", "AssertionError"),
        predicate=lambda n: _is_if_unparsing(
            n, "key_id in revoked_proof_key_ids or proof_verification_keys is None"
        ),
        transform=_drop_revoked_key_check,
    ),
    MutationSpec(
        mutant_id="M16_restore_skip_logical_integrity_check",
        invariant="F05: restore() enforces logical graph & chain integrity before touching target",
        func_name="restore",
        test_selector="test_backup_restore_scope_guard_and_memory_rollback",
        expected_error_tokens=("DID NOT RAISE", "GraphIntegrityError"),
        predicate=lambda n: _is_if_unparsing(n, "not source_report.ok"),
        transform=_weaken_if_to_false,
    ),
    MutationSpec(
        mutant_id="M17_history_skip_payload_digest_audit",
        invariant="F09: integrity_check() audits historical row payload digests (GRAPH031)",
        func_name="_integrity_check_conn",
        test_selector="test_integrity_full_history_row_and_canonical_id_derivation_audit",
        expected_error_tokens=(
            "DID NOT RAISE",
            "GraphIntegrityError",
            "GRAPH031",
            "AssertionError",
        ),
        predicate=lambda n: _is_if_unparsing(
            n, "h_payload is None or _digest_stored_normalized(h_payload) != h_payload_digest"
        ),
        transform=_weaken_if_to_false,
    ),
    MutationSpec(
        mutant_id="M18_epistemic_promotion_allow_terminal_subject",
        invariant="W11X-1B: promote_epistemic_status refuses a terminal (absorbing) node status",
        func_name="promote_epistemic_status",
        test_selector="test_epistemic_promotion_refused_on_terminal_node",
        expected_error_tokens=("DID NOT RAISE", "GraphStateTransitionError"),
        predicate=lambda n: _is_if_unparsing(n, "node.status in TERMINAL_STATUSES"),
        transform=_weaken_if_to_false,
    ),
    MutationSpec(
        mutant_id="M19_restore_source_uri_read_write",
        invariant="W11X-1A: restore() opens its source read-only and never creates it",
        func_name="restore",
        test_selector="test_restore_opens_backup_source_read_only",
        expected_error_tokens=(
            "DID NOT RAISE",
            "Regex pattern did not match",
            "AssertionError",
        ),
        predicate=lambda n: isinstance(n, ast.Constant) and n.value == "?mode=ro",
        transform=_downgrade_readonly_uri,
    ),
    MutationSpec(
        mutant_id="M20_integrity_skip_revision_count_parity",
        invariant="W11X-1C: integrity_check() proves revision_count/chain parity (GRAPH034)",
        func_name="_integrity_check_conn",
        test_selector="test_revision_count_chain_parity_is_enforced_graph034",
        expected_error_tokens=(
            "DID NOT RAISE",
            "GraphIntegrityError",
            "GRAPH034",
            "AssertionError",
        ),
        predicate=lambda n: _is_if_unparsing(n, "declared_revisions != observed_revisions"),
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


def _classify_mutant_outcome(
    spec: MutationSpec, returncode: int, combined_output: str
) -> tuple[str, str]:
    """Strict F06 kill oracle: ONLY returncode==1 + valid test failure counts as KILLED."""
    if returncode == 0:
        return "SURVIVED", "pytest exited 0 (mutant not detected)"
    if returncode != 1:
        return "INVALID", f"unexpected pytest exit code {returncode} (only 1 is a valid test kill)"
    for forbidden in _FORBIDDEN_CRASH_TOKENS:
        if forbidden in combined_output:
            return "INVALID", f"mutant triggered non-assertion crash token {forbidden!r}"
    failed_match = _FAILED_RE.search(combined_output)
    if failed_match is None or int(failed_match.group(1)) < 1:
        return "INVALID", "pytest exited 1 without reporting >= 1 failed test"
    matched_tokens = [tok for tok in spec.expected_error_tokens if tok in combined_output]
    if not matched_tokens:
        return (
            "INVALID",
            f"failure output lacked expected invariant token(s) {spec.expected_error_tokens}",
        )
    return "KILLED", f"rc=1, matched={matched_tokens[0]!r}"


def test_creative_graph_ast_mutation_campaign_zero_survived() -> None:
    """Every targeted AST mutation on CreativeGraph invariants must be validly killed (rc==1)."""
    sys.dont_write_bytecode = True
    original_bytes = TARGET_FILE.read_bytes()
    original_sha256 = _sha256_bytes(original_bytes)
    original_source = original_bytes.decode("utf-8")

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT / "src")

    survived: list[str] = []
    invalid: list[str] = []
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
                    "--tb=short",
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
            combined = proc.stdout + "\n" + proc.stderr
            verdict, detail = _classify_mutant_outcome(spec, proc.returncode, combined)
            if verdict == "KILLED":
                killed.append(f"{spec.mutant_id} ({detail})")
            elif verdict == "SURVIVED":
                survived.append(f"{spec.mutant_id}: {detail}")
            else:
                invalid.append(f"{spec.mutant_id}: {detail}")
    finally:
        TARGET_FILE.write_bytes(original_bytes)
        _purge_pycache(TARGET_FILE)
        restored_sha256 = _sha256_bytes(TARGET_FILE.read_bytes())
        assert restored_sha256 == original_sha256, (
            f"SHA-256 mismatch after restoring {TARGET_FILE}: "
            f"expected {original_sha256}, got {restored_sha256}"
        )

    assert len(MUTATIONS) == 20
    assert invalid == [], f"Invalid mutants ({len(invalid)}): {invalid}"
    assert survived == [], f"Survived mutants ({len(survived)}): {survived}"
    assert len(killed) == len(MUTATIONS)
