"""Targeted AST mutation campaign for Provenance Outbox invariants (Wave 2).

Verifies that 6 surgical AST mutations attacking outbox duplicate-event
idempotency, pending-to-applied state transitions, tampered payload digest
rejection, epistemic non-downgrade preservation (MEASURED_EVIDENCE /
VERIFIED_COMMIT), and terminal-node replay idempotency are ALL validly killed
(0 survived, 0 invalid) under strict anti-.pyc hygiene and SHA-256-verified
byte-exact restoration.
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

REPO_ROOT = Path(__file__).resolve().parents[3]
TARGET_FILE = REPO_ROOT / "src" / "nexus_ai_agent" / "provenance" / "outbox.py"
TEST_FILE = REPO_ROOT / "tests" / "unit" / "provenance" / "test_outbox_reconciliation.py"

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


def _drop_on_conflict_ignore(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.Constant) and isinstance(node.value, str)
    node.value = node.value.replace("ON CONFLICT(outbox_id) DO NOTHING", "")
    return node


def _keep_pending_instead_of_applied(node: ast.AST) -> ast.AST:
    assert isinstance(node, ast.Constant) and isinstance(node.value, str)
    node.value = node.value.replace("SET status = 'APPLIED'", "SET status = 'PENDING'")
    return node


MUTATIONS: tuple[MutationSpec, ...] = (
    MutationSpec(
        mutant_id="MO1_duplicate_outbox_id_not_idempotent",
        invariant="Duplicate (job_id, attempt_epoch, event_type) outbox enqueue is idempotent",
        func_name="insert_outbox_event_tx",
        test_selector="test_outbox_reconciler_crash_replay_and_idempotency",
        expected_error_tokens=("IntegrityError", "UNIQUE constraint failed"),
        predicate=lambda n: (
            isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and "ON CONFLICT(outbox_id) DO NOTHING" in n.value
        ),
        transform=_drop_on_conflict_ignore,
    ),
    MutationSpec(
        mutant_id="MO2_mark_applied_skips_pending_to_applied_transition",
        invariant="mark_applied transitions PENDING -> APPLIED and clears pending queue",
        func_name="mark_applied",
        test_selector="test_outbox_reconciler_crash_replay_and_idempotency",
        expected_error_tokens=("AssertionError",),
        predicate=lambda n: (
            isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and "SET status = 'APPLIED'" in n.value
        ),
        transform=_keep_pending_instead_of_applied,
    ),
    MutationSpec(
        mutant_id="MO3_reconciler_skips_payload_digest_verification",
        invariant="OutboxReconciler fails closed to FAILED_DEAD on payload_digest tamper",
        func_name="reconcile_pending",
        test_selector="test_outbox_reconciler_rejects_tampered_payload_fail_closed",
        expected_error_tokens=("AssertionError", "assert 1 == 0"),
        predicate=lambda n: _is_if_unparsing(n, "digest_of(payload) != event.payload_digest"),
        transform=_weaken_if_to_false,
    ),
    MutationSpec(
        mutant_id="MO4_attempt_epistemic_non_downgrade_guard_removed",
        invariant="Subsequent non-evidence outbox events never downgrade MEASURED_EVIDENCE",
        func_name="_apply_event",
        test_selector="test_outbox_store_validation_memory_and_error_paths",
        expected_error_tokens=("AssertionError", "cannot downgrade epistemic status"),
        predicate=lambda n: _is_if_unparsing(
            n,
            "existing_attempt is not None "
            "and existing_attempt.epistemic_status is not EpistemicStatus.HYPOTHESIS "
            "and (attempt_status_tier is EpistemicStatus.HYPOTHESIS)",
        ),
        transform=_weaken_if_to_false,
    ),
    MutationSpec(
        mutant_id="MO5_verified_commit_artifact_downgraded_on_replay",
        invariant="VERIFIED_COMMIT artifact nodes are preserved on outbox crash replay",
        func_name="_apply_event",
        test_selector="test_outbox_reconciler_crash_replay_and_idempotency",
        expected_error_tokens=("AssertionError", "cannot downgrade epistemic status"),
        predicate=lambda n: _is_if_unparsing(
            n,
            "existing_art is not None and (existing_art.status in TERMINAL_STATUSES "
            "or existing_art.epistemic_status is EpistemicStatus.VERIFIED_COMMIT)",
        ),
        transform=_weaken_if_to_false,
    ),
    MutationSpec(
        mutant_id="MO6_terminal_job_node_reopened_on_replay",
        invariant="Terminal COMMAND nodes are not mutated on outbox replay",
        func_name="_apply_event",
        test_selector="test_outbox_store_validation_memory_and_error_paths",
        expected_error_tokens=("AssertionError", "terminal"),
        predicate=lambda n: _is_if_unparsing(
            n, "existing_job is not None and existing_job.status in TERMINAL_STATUSES"
        ),
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
    """Strict kill oracle: ONLY returncode==1 + valid test failure counts as KILLED."""
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


def test_outbox_ast_mutation_campaign_zero_survived() -> None:
    """Every targeted AST mutation on ProvenanceOutboxStore/OutboxReconciler is killed."""
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

    assert len(MUTATIONS) == 6
    assert invalid == [], f"Invalid mutants ({len(invalid)}): {invalid}"
    assert survived == [], f"Survived mutants ({len(survived)}): {survived}"
    assert len(killed) == len(MUTATIONS)
