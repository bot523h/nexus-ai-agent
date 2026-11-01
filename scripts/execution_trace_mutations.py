#!/usr/bin/env python3
"""Mutation probes for the proof-carrying execution spine (task-215).

The harness copies the package to a temporary directory, weakens one causal
invariant at a time, and runs the matching deterministic test against that
copy. It never edits the working tree. A green baseline, every mutant killed,
and a green restored copy are required for exit status 0.

There is one mutant per invariant in the mission's Phase 5 list, plus one for
the refusal ladder, because a refusal that can be promoted is the same class
of defect as a failure that can be promoted.

Usage::

    python scripts/execution_trace_mutations.py
    python scripts/execution_trace_mutations.py --list
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "nexus_ai_agent"
TRACE_RELATIVE = Path("application") / "execution_trace.py"
RENDER_JOBS_RELATIVE = Path("creative") / "render_jobs.py"
TEST_FILE = "tests/unit/test_execution_trace_causality.py"
WIRING_TEST_FILE = "tests/unit/test_creative_render_jobs.py"


@dataclass(frozen=True)
class Mutation:
    name: str
    old: str
    new: str
    test: str
    invariant: str
    target: Path = TRACE_RELATIVE
    test_file: str = TEST_FILE


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "trace_identity_ignores_the_logical_key",
        '    material = f"{surface}{_SEPARATOR}{idempotency_key}".encode()',
        '    material = f"{surface}".encode()',
        "test_unrelated_executions_never_share_a_trace_id",
        "I1: unrelated executions cannot share a trace accidentally",
    ),
    Mutation(
        "child_parent_link_is_never_checked",
        "    if child.parent_trace_id != parent.trace_id:\n"
        '        raise TraceLinkError("child parent link does not resolve to the given parent")',
        "    if False:\n"
        '        raise TraceLinkError("child parent link does not resolve to the given parent")',
        "test_a_child_link_resolves_to_its_parent",
        "I2: a missing/mis-resolved parent reference is detectable",
    ),
    Mutation(
        "execution_reference_is_not_required_for_success",
        "        if self.execution_ref is None:\n            return TraceOutcome.UNKNOWN\n",
        "",
        "test_a_trace_without_an_execution_is_never_successful",
        "I3: success without a result is invalid",
    ),
    Mutation(
        "empty_evidence_claim_is_accepted",
        "        if not refs:\n"
        '            raise TraceEvidenceError("evidence_refs must not be empty")\n',
        "",
        "test_an_empty_evidence_claim_is_refused",
        "I4: a result claiming evidence without evidence is invalid",
    ),
    Mutation(
        "failure_ladder_rung_removed",
        "        if self.failure_code is not None:\n            return TraceOutcome.FAILED\n",
        "",
        "test_a_failure_is_not_promotable_by_evidence",
        "I5: a failed execution cannot be represented as successful",
    ),
    Mutation(
        "trace_identity_is_derived_from_a_timestamp",
        '    material = f"{surface}{_SEPARATOR}{idempotency_key}".encode()',
        "    material = (\n"
        '        f"{surface}{_SEPARATOR}{idempotency_key}{_SEPARATOR}{_now_iso()}"\n'
        "    ).encode()",
        "test_trace_identity_does_not_depend_on_the_clock",
        "I6: idempotent retries preserve the same causal identity",
    ),
    Mutation(
        "minted_trace_id_carries_a_per_call_suffix",
        "    trace_id = trace_id_for(surface, idempotency_key)",
        '    trace_id = f"{trace_id_for(surface, idempotency_key)}:{id(object())}"',
        "test_a_retry_of_the_same_request_keeps_one_identity",
        "I6 (retry arm): a retry of one request must not mint a second identity",
    ),
    Mutation(
        "queue_row_can_mint_execution_identity",
        "    if trace_id != expected:\n"
        "        raise TraceLinkError(\n"
        '            "trace_id is not bound to (surface, idempotency_key); "\n'
        '            "a queue row cannot mint execution identity"\n'
        "        )",
        "    if False:\n"
        "        raise TraceLinkError(\n"
        '            "trace_id is not bound to (surface, idempotency_key); "\n'
        '            "a queue row cannot mint execution identity"\n'
        "        )",
        "test_a_queue_row_cannot_mint_execution_identity",
        "I7: surface-specific code cannot fabricate execution truth",
    ),
    Mutation(
        "refusal_ladder_rung_removed",
        "        if self.refusal_code is not None:\n            return TraceOutcome.REFUSED\n",
        "",
        "test_a_refusal_is_not_promotable_by_evidence",
        "I5 (refusal arm): a refusal cannot be promoted to a success",
    ),
    Mutation(
        "truncated_sha256_is_accepted_as_evidence",
        '            if ref.startswith("sha256:"):\n'
        "                if not _SHA256_REF_PATTERN.match(ref):\n"
        '                    raise ValueError(f"malformed sha256 evidence reference: {ref!r}")',
        '            if ref.startswith("sha256:") and False:\n'
        "                if not _SHA256_REF_PATTERN.match(ref):\n"
        '                    raise ValueError(f"malformed sha256 evidence reference: {ref!r}")',
        "test_a_malformed_evidence_reference_is_refused",
        "I4 (namespace arm): a fake content address is not measured evidence",
    ),
    Mutation(
        "typed_command_loses_the_trace",
        "        trace_id=trace_id,\n"
        '        request_context=RequestContext(channel="telegram", request_id=trace_id),',
        '        request_context=RequestContext(channel="telegram", request_id=trace_id),',
        "test_the_command_bus_receives_the_bound_trace",
        "wiring: the trace must cross onto the typed command, not stop at the payload",
        target=RENDER_JOBS_RELATIVE,
        test_file=WIRING_TEST_FILE,
    ),
    Mutation(
        "worker_trusts_any_row_trace_id",
        "        return verify_bound_trace_id(\n"
        "            surface=CREATIVE_SURFACE,\n"
        "            idempotency_key=payload.idempotency_key,\n"
        "            trace_id=payload.trace_id,\n"
        "        )",
        "        return payload.trace_id",
        "test_a_row_that_forged_its_trace_is_refused_before_any_engine",
        "wiring: a queue row cannot mint execution identity",
        target=RENDER_JOBS_RELATIVE,
        test_file=WIRING_TEST_FILE,
    ),
)


def _run_pytest(package_root: Path, target: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "--no-header", target],
        cwd=ROOT,
        env={"PYTHONPATH": str(package_root), "PATH": "/usr/bin:/bin", "HOME": "/tmp"},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _tail(output: str, lines: int = 8) -> str:
    return "\n".join(output.strip().splitlines()[-lines:])


def _is_test_failure(result: subprocess.CompletedProcess[str]) -> bool:
    output = result.stdout + result.stderr
    return result.returncode == 1 and re.search(r"(?m)^\d+ failed(?:,|\s|$)", output) is not None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for mutation in MUTATIONS:
            print(f"{mutation.name}: {mutation.invariant}")
        return 0

    with tempfile.TemporaryDirectory(prefix="nexus-execution-trace-mutations-") as temporary:
        package_root = Path(temporary) / "src"
        shutil.copytree(PACKAGE, package_root / "nexus_ai_agent")
        trace_path = package_root / "nexus_ai_agent" / TRACE_RELATIVE
        render_jobs_path = package_root / "nexus_ai_agent" / RENDER_JOBS_RELATIVE

        print("baseline ... ", end="", flush=True)
        baseline = _run_pytest(package_root, TEST_FILE)
        if baseline.returncode != 0:
            print("RED — refusing to mutate a failing baseline")
            print(_tail(baseline.stdout + baseline.stderr, lines=30))
            return 2
        print("GREEN")

        killed = 0
        survivors: list[str] = []
        for mutation in MUTATIONS:
            target_path = trace_path if mutation.target == TRACE_RELATIVE else render_jobs_path
            current = target_path.read_text(encoding="utf-8")
            if mutation.old not in current:
                print(f"{mutation.name}: DOES NOT APPLY — source drifted")
                survivors.append(mutation.name)
                continue

            target_path.write_text(current.replace(mutation.old, mutation.new, 1), encoding="utf-8")
            try:
                result = _run_pytest(package_root, f"{mutation.test_file}::{mutation.test}")
            finally:
                target_path.write_text(current, encoding="utf-8")

            if result.returncode == 0:
                print(f"{mutation.name}: SURVIVED — {mutation.invariant}")
                survivors.append(mutation.name)
            elif _is_test_failure(result):
                killed += 1
                print(f"{mutation.name}: killed")
            else:
                print(f"{mutation.name}: ERROR — mutant did not produce a pytest failure")
                print(_tail(result.stdout + result.stderr, lines=30))
                return 4

        print("restored baseline ... ", end="", flush=True)
        restored = _run_pytest(package_root, TEST_FILE)
        if restored.returncode != 0:
            print("RED — restored copy failed")
            print(_tail(restored.stdout + restored.stderr, lines=30))
            return 3
        print("GREEN")

    if survivors:
        print(f"{killed}/{len(MUTATIONS)} mutants killed; survivors: {', '.join(survivors)}")
        return 1
    print(f"{killed}/{len(MUTATIONS)} mutants killed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
