#!/usr/bin/env python3
"""W4 deterministic, function-scoped source mutants validated by Python AST.

Run serially against a clean checkout; do not run pytest/other mutation campaigns
concurrently. Baseline and final targeted tests must be green. Only an exact
pytest test-failure exit and a named target failure with the expected diagnostic
count as a kill. Infrastructure errors/timeouts never count. Every restoration
is byte-for-byte and SHA-256 checked, including interrupted campaigns.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
STUDIO = "src/nexus_ai_agent/creative/studio/"
ADAPTER = "src/nexus_ai_agent/adapters/"
CP1 = "tests/unit/test_w4_cp1_durable_dedupe_and_approvals.py::"
CP4 = "tests/unit/test_w4_cp4_coverage.py::"
DEST = "tests/integration/test_w4_fenced_filesystem_destination.py::"
IR = "tests/unit/test_w4_plan_ir.py::"
INGRESS = "tests/integration/test_w4_ingress_e2e.py::"
OWN = "tests/unit/test_w4_mutations.py::"
APPROVAL = (
    "tests/unit/test_w4_approval_contracts.py::"
    "test_approval_binding_requires_trusted_verifier_and_current_matching_claims"
)


@dataclass(frozen=True)
class Mutation:
    id: str
    source: str
    scope: str
    old: str
    new: str
    target_test: str
    expected_failure_pattern: str = r"AssertionError|Failed:|\nE\s+assert "


# The MAC is delegated to IBPC; M11 mutates that delegation, not an invented
# compare_digest call. M13 targets the shared approval-binding port.
REGISTRY = (
    Mutation(
        "W4-M01",
        ADAPTER + "durable_plan_dedupe.py",
        "DurablePlanDedupeStore.repair_in_doubt",
        "SET consumed=1",
        "SET consumed=0",
        CP1 + "test_evidence_driven_recovery_fences_old_writer_and_consumes_nonce",
    ),
    Mutation(
        "W4-M02",
        ADAPTER + "durable_plan_dedupe.py",
        "DurablePlanDedupeStore.inspect_recovery",
        "now + 60.0",
        "now + 600.0",
        CP1 + "test_recovery_evidence_adversarial_transactions[clock_expiry]",
    ),
    Mutation(
        "W4-M03",
        ADAPTER + "durable_plan_dedupe.py",
        "DurablePlanDedupeStore.reserve",
        "self._now() < expiry",
        "self._now() > expiry",
        "tests/unit/test_w4_durable_bus_and_dedupe.py::test_live_lease_duplicate_is_refused_without_disrupting_owner",
        r"PlanInDoubtQuarantineError: plan is not safely replayable",
    ),
    Mutation(
        "W4-M04",
        ADAPTER + "durable_plan_dedupe.py",
        "DurablePlanDedupeStore.repair_in_doubt",
        'expected_attempt + int(takeover or resolution_action == "compensate_present")',
        "expected_attempt",
        CP1 + "test_evidence_driven_recovery_fences_old_writer_and_consumes_nonce",
    ),
    Mutation(
        "W4-M05",
        ADAPTER + "durable_plan_dedupe.py",
        "DurablePlanDedupeStore._store_lock",
        'getattr(os, "O_NOFOLLOW", 0)',
        "0",
        OWN + "test_store_lock_rejects_replaced_symlink",
    ),
    Mutation(
        "W4-M06",
        ADAPTER + "fenced_filesystem_destination.py",
        "FencedFilesystemEffectDestination.apply",
        'fencing_epoch <= data["tombstone_epoch"] or fencing_epoch < data["highest_epoch"]',
        "False",
        DEST + "test_absent_tombstone_survives_restart_and_blocks_same_and_older_epochs",
    ),
    Mutation(
        "W4-M07",
        ADAPTER + "fenced_filesystem_destination.py",
        "FencedFilesystemEffectDestination.compensate",
        "self._write_metadata(key, data)",
        "pass",
        CP4 + "test_artifact_preconditions_and_compensation",
    ),
    Mutation(
        "W4-M08",
        ADAPTER + "fenced_filesystem_destination.py",
        "FilesystemObservationVerifier.verify_payload",
        ") == expected_digest and observation",
        ") is not None and observation",
        INGRESS + "test_independent_postcondition_checks_actual_payload",
    ),
    Mutation(
        "W4-M09",
        ADAPTER + "fenced_filesystem_destination.py",
        "FencedFilesystemEffectDestination._published_digest",
        'data["payload_digest"] != digest',
        "False",
        DEST + "test_evidence_and_filesystem_corruption_fail_closed",
    ),
    Mutation(
        "W4-M10",
        ADAPTER + "fenced_filesystem_destination.py",
        "FencedFilesystemEffectDestination.apply",
        'data["payload_digest"] not in (None, digest)',
        "False",
        DEST + "test_idempotent_publication_and_project_isolation",
    ),
    Mutation(
        "W4-M11",
        ADAPTER + "ibpc_plan_approval.py",
        "IBPCPlanApprovalVerifier.verify",
        "verify_envelope(self._provider, envelope, asdict(binding), now=now.isoformat())",
        "True",
        CP1 + "test_ibpc_mac_verifier_binds_entire_approval_and_bus_execution",
    ),
    Mutation(
        "W4-M12",
        ADAPTER + "ibpc_plan_approval.py",
        "IBPCPlanApprovalVerifier.verify",
        "now = self._now()",
        "now = datetime.fromtimestamp(binding.issued_at, timezone.utc)",
        CP1 + "test_ibpc_mac_verifier_binds_entire_approval_and_bus_execution",
    ),
    Mutation(
        "W4-M13",
        STUDIO + "durable_dedupe.py",
        "verify_approval_binding",
        "binding.project_id != project_id",
        "False",
        APPROVAL,
    ),
    Mutation(
        "W4-M14",
        STUDIO + "plan_ir.py",
        "PlanIR.__post_init__",
        "not 1 <= len(self.nodes) <= MAX_NODES",
        "False",
        OWN + "test_graph_node_and_edge_limits",
    ),
    Mutation(
        "W4-M15",
        STUDIO + "plan_ir.py",
        "PlanIR.topological_nodes",
        "sum(len(node.dependencies) for node in self.nodes) > MAX_EDGES",
        "False",
        OWN + "test_graph_node_and_edge_limits",
    ),
    Mutation(
        "W4-M16",
        STUDIO + "plan_ir.py",
        "PlanIR.topological_nodes",
        "len(ordered) != len(self.nodes)",
        "False",
        IR + "test_invalid_graphs",
    ),
    Mutation(
        "W4-M17",
        STUDIO + "plan_ir.py",
        "PlanIR.topological_nodes",
        "depth[child] > MAX_DEPTH",
        "False",
        IR + "test_invalid_graphs",
    ),
    Mutation(
        "W4-M18",
        STUDIO + "plan_ir.py",
        "PlanIR.topological_nodes",
        "ready.sort(reverse=True)",
        "ready.sort(reverse=False)",
        IR + "test_order_and_hash_are_independent_of_input_order",
    ),
    Mutation(
        "W4-M19",
        STUDIO + "plan_ir.py",
        "PlanIR.canonical_bytes",
        '"dependencies": sorted(node.dependencies)',
        '"dependencies": []',
        IR + "test_dag_structure_is_bound_into_bus_approval_hash",
    ),
    Mutation(
        "W4-M20",
        STUDIO + "plan_execution.py",
        "compile_plan",
        "command_fingerprint(command) != node.command_digest",
        "False",
        OWN + "test_compiler_rejects_same_operation_changed_digest",
    ),
    Mutation(
        "W4-M21",
        STUDIO + "bus.py",
        "CommandBus.dispatch_durable_plan",
        "reversed(completed)",
        "completed",
        CP1 + "test_unknown_middle_effect_quarantines_and_compensates_earlier_effects_lifo",
    ),
    Mutation(
        "W4-M22",
        STUDIO + "bus.py",
        "CommandBus._dispatch_locked",
        "prior.fingerprint != fingerprint",
        "False",
        OWN + "test_idempotency_and_permission_gates",
    ),
    Mutation(
        "W4-M23",
        STUDIO + "bus.py",
        "PlanTransaction._step_input_hash",
        'evidence["admitted_ir_hash"] = self.admitted_ir_hash',
        'evidence["admitted_ir_hash"] = None',
        IR + "test_dag_structure_is_bound_into_bus_approval_hash",
    ),
    Mutation(
        "W4-M24",
        STUDIO + "bus.py",
        "CommandBus._validate_dispatch",
        "if not decision.allowed:",
        "if False:",
        OWN + "test_idempotency_and_permission_gates",
    ),
    Mutation(
        "W4-M25",
        STUDIO + "bus.py",
        "PlanTransaction._run_compensations",
        "reversed(succeeded)",
        "succeeded",
        "tests/unit/test_plan_transaction.py::test_failing_step_emergency_compensation_runs_first",
    ),
    Mutation(
        "W4-M26",
        ADAPTER + "durable_studio_runtime.py",
        "ProvisionedPrincipalAuthority.authenticate",
        "hmac.compare_digest(expected, digest)",
        "True",
        INGRESS + "test_authenticated_ir_approval_execution_and_cold_replay",
    ),
    Mutation(
        "W4-M27",
        ADAPTER + "durable_studio_runtime.py",
        "ProvisionedPrincipalAuthority.repair_session",
        'access.require_permissions(("project:repair",))',
        "pass",
        INGRESS + "test_repair_scope_requires_permission_and_cannot_leak_to_another_thread",
    ),
    Mutation(
        "W4-M28",
        ADAPTER + "durable_studio_runtime.py",
        "_artifact_verifier",
        "reader.verify_payload(observation, hashlib.sha256(content).hexdigest())",
        "True",
        OWN + "test_runtime_postcondition_checks_disk_not_claimed_digest",
    ),
    Mutation(
        "W4-M29",
        ADAPTER + "durable_studio_runtime.py",
        "ProvisionedPrincipalAuthority.authorize_repair",
        "self._repair_scope.get() == (project_id, operator_id)",
        "True",
        INGRESS + "test_repair_scope_requires_permission_and_cannot_leak_to_another_thread",
    ),
    Mutation(
        "W4-M30",
        STUDIO + "runtime.py",
        "DurableStudioExecutionRuntime.__post_init__",
        "self._store is None or not self._bus.uses_durable_store(self._store)",
        "False",
        INGRESS + "test_runtime_port_wiring_and_unauthorized_repair",
    ),
)


def mutated_bytes(original: bytes, mutation: Mutation) -> bytes:
    source = original.decode("utf-8")
    tree: ast.AST = ast.parse(source)
    for name in mutation.scope.split("."):
        matches = [
            node
            for node in getattr(tree, "body", [])
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == name
        ]
        if len(matches) != 1:
            raise ValueError(f"{mutation.id}: scope is not unique")
        tree = matches[0]
    assert isinstance(tree, (ast.ClassDef, ast.FunctionDef))
    lines = source.splitlines(keepends=True)
    begin, end = tree.lineno - 1, tree.end_lineno
    assert end is not None
    body = "".join(lines[begin:end])
    if body.count(mutation.old) != 1:
        raise ValueError(f"{mutation.id}: anchor count is {body.count(mutation.old)}")
    result = (
        "".join(lines[:begin]) + body.replace(mutation.old, mutation.new, 1) + "".join(lines[end:])
    )
    ast.parse(result)
    if ast.dump(ast.parse(source)) == ast.dump(ast.parse(result)):
        raise ValueError("mutation did not change the AST")
    return result.encode("utf-8")


def _purge_bytecode_cache(source_path: Path) -> None:
    Path(importlib.util.cache_from_source(str(source_path))).unlink(missing_ok=True)
    cache = source_path.parent / "__pycache__"
    if cache.is_dir():
        for candidate in cache.glob(f"{source_path.stem}.*.pyc"):
            candidate.unlink(missing_ok=True)
        if not any(cache.iterdir()):
            cache.rmdir()
    importlib.invalidate_caches()


@contextmanager
def installed_mutant(path: Path, mutation: Mutation) -> Iterator[str]:
    original = path.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    changed = mutated_bytes(original, mutation)
    try:
        path.write_bytes(changed)
        _purge_bytecode_cache(path)
        yield digest
    finally:
        path.write_bytes(original)
        _purge_bytecode_cache(path)
        restored = path.read_bytes()
        if restored != original or hashlib.sha256(restored).hexdigest() != digest:
            raise RuntimeError(f"source restoration failed: {path}")


def run_tests(targets: list[str]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "pytest_asyncio.plugin",
            "-q",
            "--tb=short",
            *targets,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def classify(returncode: int, output: str, mutation: Mutation) -> str:
    if returncode == 0:
        return "SURVIVED"
    forbidden = (
        "SyntaxError",
        "IndentationError",
        "ImportError",
        "ModuleNotFoundError",
        "collected 0 items",
        "no tests ran",
        "INTERNALERROR",
        "ERROR collecting",
        "ERROR at setup",
        "ERROR at teardown",
    )
    if returncode != 1 or any(token in output for token in forbidden):
        return "INVALID"
    named_failure = any(
        re.match(r"^FAILED " + re.escape(mutation.target_test) + r"(?:$|\s|\[)", line)
        for line in output.splitlines()
    )
    if named_failure and re.search(mutation.expected_failure_pattern, output):
        return "KILLED"
    return "INVALID"


def main() -> int:
    start = time.perf_counter()
    targets = sorted({mutation.target_test for mutation in REGISTRY})
    for source in {mutation.source for mutation in REGISTRY}:
        _purge_bytecode_cache(ROOT / source)
    baseline = run_tests(targets)
    if baseline.returncode != 0:
        print("BASELINE FAILED\n" + baseline.stdout + baseline.stderr)
        return 2
    results = []
    for mutation in REGISTRY:
        path = ROOT / mutation.source
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        try:
            with installed_mutant(path, mutation):
                proc = run_tests([mutation.target_test])
                output = proc.stdout + proc.stderr
                outcome = classify(proc.returncode, output, mutation)
        except (subprocess.TimeoutExpired, ValueError) as exc:
            outcome, output = "INVALID", str(exc)
        after = hashlib.sha256(path.read_bytes()).hexdigest()
        if before != after:
            raise RuntimeError("restoration hash mismatch")
        results.append(
            {
                **asdict(mutation),
                "outcome": outcome,
                "before_sha256": before,
                "after_sha256": after,
                "output": output,
            }
        )
        print(mutation.id, outcome, flush=True)
    final = run_tests(targets)
    counts = {
        key: sum(row["outcome"] == key for row in results)
        for key in ("KILLED", "SURVIVED", "INVALID")
    }
    report = {
        "results": results,
        "seconds": time.perf_counter() - start,
        "baseline_returncode": baseline.returncode,
        "final_returncode": final.returncode,
    }
    output_path = ROOT / "ci-artifacts/w4/mutations.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"SUMMARY total=30 killed={counts['KILLED']} survived={counts['SURVIVED']} "
        f"invalid={counts['INVALID']} not_run={30 - len(results)}"
    )
    return 0 if counts["KILLED"] == 30 and final.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
