#!/usr/bin/env python3
"""Gate 5 mutation probes (task-181) — a small, deterministic mutation harness.

Six adversarial mutations of the invariants this closure exists to protect:

======  ==========================================  ==============================
probe   mutation                                     killed by
======  ==========================================  ==============================
#1      remove verification (empty default registry)  zero-byte/lying-claim tests
#2      force COMPLETED on typed failure (both layers) typed-failure regression
#3      skip atomic publish (non-atomic + leak)        publication-order tests
#4      drop job_id from the trace binding             trace regression
#5      notifier trusts result.success                 lying-result regression
#6      bypass failure_status() classification         classification tests
======  ==========================================  ==============================

Protocol per probe (task-181 acceptance): baseline GREEN → apply mutant →
expect RED → restore bytes → SHA restored → GREEN again.  Nothing is left
mutated; the run is idempotent.  Honesty rule: a probe "caught" only when the
targeted tests actually flip GREEN → RED (exit 1/2), never on warnings.

Usage:  .venv/bin/python scripts/gate5_mutation_probes.py
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:warnings", "--tb=no"]


@dataclass(frozen=True)
class Probe:
    name: str
    target: Path  # file to mutate
    anchor: str  # exact text to replace (must be unique in the file)
    mutant: str  # replacement
    tests: tuple[str, ...]  # targeted tests that must flip GREEN → RED


PROBES: tuple[Probe, ...] = (
    Probe(
        name="#1 remove verification",
        target=REPO / "src/nexus_ai_agent/adapters/in_process_job_queue.py",
        anchor=('    return {\n        "creative_render": creative_render_verifier,'),
        mutant=(
            "    return {}  # MUTATION: no verifier registered at all"
            '\n    return {\n        "creative_render": creative_render_verifier,'
        ),
        tests=(
            "tests/integration/test_gate5_closure.py::test_verification_failure_notification_is_never_success",
            "tests/integration/test_job_lifecycle_queue.py::test_m3_zero_byte_artifact_cannot_complete",
        ),
    ),
    Probe(
        name="#2 force COMPLETED on typed failure",
        target=REPO / "src/nexus_ai_agent/jobs/failure_semantics.py",
        anchor=(
            '    code = result.get("error_code")\n'
            '    return result.get("success") is False and isinstance(code, str) and bool(code)'
        ),
        mutant="    return False  # MUTATION: typed failures no longer detected",
        tests=(
            "tests/unit/test_job_lifecycle.py::test_creative_verifier_typed_user_failure_is_refused_fail_closed",
            "tests/integration/test_gate5_closure.py::test_typed_render_failure_lands_in_a_failure_status_never_completed",
        ),
    ),
    Probe(
        name="#3 skip atomic publish",
        target=REPO / "src/nexus_ai_agent/jobs/feature_verification.py",
        anchor="        os.replace(staged, published)",
        mutant=(
            "        published.write_bytes(staged.read_bytes())  "
            "# MUTATION: non-atomic, leaks staged"
        ),
        tests=(
            "tests/integration/test_gate5_closure.py::test_happy_path_publishes_then_reprobes_then_completes",
            "tests/integration/test_gate5_closure.py::test_refused_publication_preserves_old_artifact_and_cleans_staging",
        ),
    ),
    Probe(
        name="#4 drop job_id from the trace",
        target=REPO / "src/nexus_ai_agent/adapters/in_process_job_queue.py",
        anchor="        structlog.contextvars.bind_contextvars(job_id=job_id)",
        mutant="        pass  # MUTATION: job_id no longer bound to the trace",
        tests=(
            "tests/integration/test_gate5_closure.py::test_every_lifecycle_trace_event_carries_job_id",
        ),
    ),
    Probe(
        name="#5 notifier trusts result.success",
        target=REPO / "src/nexus_ai_agent/bot/app.py",
        anchor=(
            "        failed = completion.status is not JobStatus.COMPLETED "
            'or result.get("success") is False'
        ),
        mutant='        failed = result.get("success") is False  # MUTATION: claim beats status',
        tests=(
            "tests/integration/test_gate5_closure.py::test_lying_success_result_cannot_turn_failure_into_success",
        ),
    ),
    Probe(
        name="#6 bypass failure_status()",
        target=REPO / "src/nexus_ai_agent/jobs/failure_semantics.py",
        anchor=(
            "    if failure is FailureClass.RETRYABLE:\n        return JobStatus.FAILED_RETRYABLE\n"
            "    return JobStatus.FAILED_TERMINAL"
        ),
        mutant="    return JobStatus.FAILED_TERMINAL  # MUTATION: classification ignored",
        tests=(
            "tests/unit/test_failure_semantics.py::test_failure_status_maps_class_to_durable_state",
            "tests/integration/test_gate5_closure.py::test_typed_render_failure_lands_in_a_failure_status_never_completed",
        ),
    ),
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_tests(tests: tuple[str, ...]) -> int:
    result = subprocess.run([*PYTEST, *tests], cwd=REPO, capture_output=True, text=True)
    return result.returncode


def _apply(probe: Probe) -> None:
    text = probe.target.read_text()
    if text.count(probe.anchor) != 1:
        raise SystemExit(
            f"anchor for {probe.name} is not unique in {probe.target}: "
            f"{text.count(probe.anchor)} matches"
        )
    probe.target.write_text(text.replace(probe.anchor, probe.mutant))


def _restore(probe: Probe, original: bytes) -> None:
    probe.target.write_bytes(original)


def main() -> int:
    failures: list[str] = []
    caught = 0
    for probe in PROBES:
        original = probe.target.read_bytes()
        before_sha = hashlib.sha256(original).hexdigest()
        print(f"\n=== {probe.name} ({probe.target.relative_to(REPO)})")

        baseline = _run_tests(probe.tests)
        if baseline != 0:
            failures.append(f"{probe.name}: baseline not GREEN (exit {baseline})")
            print(f"  baseline: NOT GREEN (exit {baseline}) — probe invalid")
            continue
        print("  baseline: GREEN")

        try:
            _apply(probe)
        except SystemExit as exc:
            failures.append(f"{probe.name}: {exc}")
            print(f"  mutant: NOT APPLIED — {exc}")
            continue
        mutant_exit = _run_tests(probe.tests)
        if mutant_exit == 0:
            failures.append(f"{probe.name}: mutant survived (tests stayed GREEN)")
            print("  mutant:  SURVIVED (tests still green) — invariant NOT protected")
        else:
            caught += 1
            print(f"  mutant:  RED (exit {mutant_exit}) — caught")

        _restore(probe, original)
        after_sha = _sha(probe.target)
        if after_sha != before_sha:
            failures.append(f"{probe.name}: restore changed the file ({after_sha} != {before_sha})")
            print(f"  restore: SHA MISMATCH {after_sha} != {before_sha}")
            continue
        print(f"  restore: SHA restored {before_sha[:12]}…")
        restored = _run_tests(probe.tests)
        if restored != 0:
            failures.append(f"{probe.name}: not GREEN after restore (exit {restored})")
            print(f"  rerun:   NOT GREEN (exit {restored})")
        else:
            print("  rerun:   GREEN")

    print(f"\n=== summary: {caught}/{len(PROBES)} mutants caught ===")
    for failure in failures:
        print(f"FAIL: {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
