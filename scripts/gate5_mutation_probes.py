#!/usr/bin/env python3
"""Gate 5 mutation probes (task-181) — a small, deterministic mutation harness.

Seventeen adversarial mutations of the invariants this closure exists to protect
(six original closure probes + ten execution-ownership / commit-fencing probes
M1–M10 from the final Gate 5 repair):

======  ==========================================  ==============================
probe   mutation                                     killed by
======  ==========================================  ==============================
#1      remove verification (empty default registry)  zero-byte/lying-claim tests
#2      force COMPLETED on typed failure (both layers) typed-failure regression
#3      skip atomic publish (non-atomic + leak)        publication-order tests
#4      drop job_id from the trace binding             trace regression
#5      notifier trusts result.success                 lying-result regression
#6      bypass failure_status() classification         classification tests
M1      reservation re-claims a PROCESSING row        T5
M2–M4   _mark_pending/_completed/_failed unfenced     T1+T1b / T2 / T3
M5a/b   success notified without a committed row      T15
M6      publication skips the ownership re-check      T8 / T8b
M7/M8   retract drops bytes / publish keeps no backup T9 / T9+T10
M9      resume_pending ignores the stale_after cutoff  T7
M10     bare success=False treated as success          R4
======  ==========================================  ==============================

GOV-2 (2026-10-15) accounting: 12 + 5 re-anchored probes = 17 (the earlier
"11 + 5 = 16" was a counting error).  M2–M4 are *multi-edit* probes: every
``_mark_*`` CAS is guarded three times — a fenced SELECT, the attempt-ledger
status filter and the fenced UPDATE — so a probe that weakens only one layer
is an equivalent mutant, not an attack; the probe now weakens all three and
the targeted tests flip RED.  Mutants keep the SQL binding count (``AND ? >= 0``
instead of dropping the placeholder) so the tests fail for the right reason.
The harness pins ``PYTHONPATH`` to the tree under mutation: run in an isolated
worktree (``git worktree add --detach``) an editable install would otherwise
shadow every mutation and the campaign would report 0/17 while touching nothing.

Protocol per probe (task-181 acceptance): baseline GREEN → apply mutant →
expect RED → restore bytes → SHA restored → GREEN again.  Nothing is left
mutated; the run is idempotent.  Honesty rule: a probe "caught" only when the
targeted tests actually flip GREEN → RED (exit 1/2), never on warnings.

Usage:  .venv/bin/python scripts/gate5_mutation_probes.py
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTEST = [
    sys.executable,
    "-m",
    "pytest",
    "-q",
    "-p",
    "no:warnings",
    "-p",
    "no:cacheprovider",
    "--tb=no",
]


@dataclass(frozen=True)
class Probe:
    name: str
    target: Path  # file to mutate
    anchor: str  # exact text to replace (must be unique in the file)
    mutant: str  # replacement
    tests: tuple[str, ...]  # targeted tests that must flip GREEN → RED
    #: Further (anchor, mutant) pairs applied with the first one. A fence that
    #: is enforced twice (a fenced SELECT *and* a fenced UPDATE) is only
    #: "forgotten" when both are weakened — weakening one layer and watching
    #: the other hold is not an attack on the invariant (GOV-2, 2026-10-15).
    extra_edits: tuple[tuple[str, str], ...] = ()

    def edits(self) -> tuple[tuple[str, str], ...]:
        return ((self.anchor, self.mutant), *self.extra_edits)


#: The fenced read each ``_mark_*`` CAS performs before its fenced UPDATE
#: (20-space indent inside ``_mark_pending``'s ``try``, 16 elsewhere).
_FENCED_SELECT_20 = (
    "                    SELECT attempt_history_json FROM nexus_job_queue\n"
    "                    WHERE id = ? AND status IN (?, ?) AND attempt = ?"
)
_UNFENCED_SELECT_20 = _FENCED_SELECT_20.replace("AND attempt = ?", "AND ? >= 0  -- MUTATION")
_FENCED_SELECT_16 = (
    "                SELECT attempt_history_json FROM nexus_job_queue\n"
    "                WHERE id = ? AND status IN (?, ?) AND attempt = ?"
)
_UNFENCED_SELECT_16 = _FENCED_SELECT_16.replace("AND attempt = ?", "AND ? >= 0  -- MUTATION")

F = "tests/integration/test_gate5_execution_fencing.py::"
QUEUE = REPO / "src/nexus_ai_agent/adapters/in_process_job_queue.py"

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
    # ---- execution-ownership / commit-fencing probes (Gate 5 final repair) ----
    Probe(
        name="M1 reservation re-claims a PROCESSING row",
        target=REPO / "src/nexus_ai_agent/adapters/in_process_job_queue.py",
        anchor=(
            "                WHERE id = ? AND status = ?\n"
            '                """,\n'
            "                (JobStatus.PROCESSING.value, _now(), job_id, JobStatus.PENDING.value),"
        ),
        mutant=(
            "                WHERE id = ? AND status IN (?, 'processing')  -- MUTATION\n"
            '                """,\n'
            "                (JobStatus.PROCESSING.value, _now(), job_id, JobStatus.PENDING.value),"
        ),
        tests=(f"{F}test_t5_two_processes_cannot_both_own_one_execution",),
    ),
    Probe(
        name="M2 _mark_pending forgets the fence",
        target=REPO / "src/nexus_ai_agent/adapters/in_process_job_queue.py",
        anchor=(
            "                    SET status = ?, started_at = NULL, attempt_history_json = ?\n"
            "                    WHERE id = ? AND status IN (?, ?) AND attempt = ?"
        ),
        mutant=(
            "                    SET status = ?, started_at = NULL, attempt_history_json = ?\n"
            "                    WHERE id = ? AND status IN (?, ?) AND ? >= 0  -- MUTATION"
        ),
        tests=(
            f"{F}test_t1_cancelled_old_worker_cannot_reopen_a_newer_execution",
            f"{F}test_t1b_cancelled_old_worker_cannot_reset_the_new_owners_processing_row",
        ),
        extra_edits=(
            (
                '"""PROCESSING/VERIFYING → PENDING for OUR execution only (fenced CAS)."""\n'
                "        try:\n"
                "            with self._db_lock, self._connection() as connection:\n"
                "                row = connection.execute(\n"
                '                    """\n' + _FENCED_SELECT_20,
                '"""PROCESSING/VERIFYING → PENDING for OUR execution only (fenced CAS)."""\n'
                "        try:\n"
                "            with self._db_lock, self._connection() as connection:\n"
                "                row = connection.execute(\n"
                '                    """\n' + _UNFENCED_SELECT_20,
            ),
            (
                (
                    '                elif attempt.get("status") != "verified":\n'
                    "                    raise _AttemptTransitionError("
                    '"attempt is not recoverable")'
                ),
                (
                    "                elif False:  # MUTATION: ledger status ignored\n"
                    "                    raise _AttemptTransitionError("
                    '"attempt is not recoverable")'
                ),
            ),
        ),
    ),
    Probe(
        name="M3 _mark_completed forgets the fence",
        target=REPO / "src/nexus_ai_agent/adapters/in_process_job_queue.py",
        anchor=(
            "                    attempt_history_json = ?, error = NULL, finished_at = ?\n"
            "                WHERE id = ? AND status IN (?, ?) AND attempt = ?"
        ),
        mutant=(
            "                    attempt_history_json = ?, error = NULL, finished_at = ?\n"
            "                WHERE id = ? AND status IN (?, ?) AND ? >= 0  -- MUTATION: unfenced"
        ),
        tests=(f"{F}test_t2_old_worker_cannot_mark_newer_attempt_completed",),
        extra_edits=(
            (
                "        result_json = canonical_json(result)\n"
                "        with self._db_lock, self._connection() as connection:\n"
                "            row = connection.execute(\n"
                '                """\n' + _FENCED_SELECT_16,
                "        result_json = canonical_json(result)\n"
                "        with self._db_lock, self._connection() as connection:\n"
                "            row = connection.execute(\n"
                '                """\n' + _UNFENCED_SELECT_16,
            ),
            (
                (
                    '                    and entry.get("status") in '
                    '{"processing", "verifying", "verified"}\n'
                    "                ),\n"
                    "                None,\n"
                    "            )\n"
                    "            if attempt is None:\n"
                    "                return False\n"
                    "            now = _now()"
                ),
                (
                    "                    and True  # MUTATION: ledger status ignored\n"
                    "                ),\n"
                    "                None,\n"
                    "            )\n"
                    "            if attempt is None:\n"
                    "                return False\n"
                    "            now = _now()"
                ),
            ),
        ),
    ),
    Probe(
        name="M4 _mark_failed forgets the fence",
        target=REPO / "src/nexus_ai_agent/adapters/in_process_job_queue.py",
        anchor=(
            "                    attempt_history_json = ?, finished_at = ?\n"
            "                WHERE id = ? AND status IN (?, ?) AND attempt = ?"
        ),
        mutant=(
            "                    attempt_history_json = ?, finished_at = ?\n"
            "                WHERE id = ? AND status IN (?, ?) AND ? >= 0  -- MUTATION: unfenced"
        ),
        tests=(f"{F}test_t3_old_worker_cannot_mark_newer_attempt_failed",),
        extra_edits=(
            (
                "        result_json = canonical_json(result) if result is not None else None\n"
                "        with self._db_lock, self._connection() as connection:\n"
                "            row = connection.execute(\n"
                '                """\n' + _FENCED_SELECT_16,
                "        result_json = canonical_json(result) if result is not None else None\n"
                "        with self._db_lock, self._connection() as connection:\n"
                "            row = connection.execute(\n"
                '                """\n' + _UNFENCED_SELECT_16,
            ),
            (
                (
                    '                    and entry.get("status") in '
                    '{"processing", "verifying", "verified"}\n'
                    "                ),\n"
                    "                None,\n"
                    "            )\n"
                    "            if attempt is None:\n"
                    "                return False\n"
                    '            attempt["status"] = status.value'
                ),
                (
                    "                    and True  # MUTATION: ledger status ignored\n"
                    "                ),\n"
                    "                None,\n"
                    "            )\n"
                    "            if attempt is None:\n"
                    "                return False\n"
                    '            attempt["status"] = status.value'
                ),
            ),
        ),
    ),
    Probe(
        name="M5a success notified without a committed COMPLETED (verified lane)",
        target=QUEUE,
        anchor=(
            "                if not committed:\n"
            "                    # Durable commit refused: we are no longer the owner."
        ),
        mutant=(
            "                if not (committed or True):  # MUTATION: notify unconditionally\n"
            "                    # Durable commit refused: we are no longer the owner."
        ),
        tests=(f"{F}test_t15_no_success_notification_without_durable_completed_state[verified]",),
    ),
    Probe(
        name="M5b success notified without a committed COMPLETED (unverified lane)",
        target=QUEUE,
        anchor=(
            "            if not await asyncio.to_thread("
            "self._mark_completed, claim, final_result):\n"
            "                self._log_rejected(job_id, claim, JobStatus.COMPLETED)"
        ),
        mutant=(
            "            if not (\n"
            "                await asyncio.to_thread(self._mark_completed, claim, final_result)\n"
            "                or True  # MUTATION: notify regardless of the commit\n"
            "            ):\n"
            "                self._log_rejected(job_id, claim, JobStatus.COMPLETED)"
        ),
        tests=(f"{F}test_t15_no_success_notification_without_durable_completed_state[unverified]",),
    ),
    Probe(
        name="M6 publication skips the ownership re-check",
        target=REPO / "src/nexus_ai_agent/adapters/in_process_job_queue.py",
        anchor=(
            "        if not await asyncio.to_thread("
            "self._owns_execution, claim, JobStatus.VERIFYING):"
        ),
        mutant="        if False:  # MUTATION: publish without re-checking ownership",
        tests=(
            f"{F}test_t8_stale_worker_publication_cannot_overwrite_current_owners_artifact",
            f"{F}test_t8b_publication_is_fenced_immediately_before_the_rename",
        ),
    ),
    Probe(
        name="M7 retract drops the refused bytes instead of restoring the previous artifact",
        target=REPO / "src/nexus_ai_agent/jobs/feature_verification.py",
        anchor=(
            "    if backup.exists():\n"
            "        os.replace(backup, published)  # atomic restore of the previous bytes\n"
            "    else:\n"
            "        published.unlink(missing_ok=True)"
        ),
        mutant=(
            "    backup.unlink(missing_ok=True)  # MUTATION: previous artifact lost\n"
            "    published.unlink(missing_ok=True)"
        ),
        tests=(f"{F}test_t9_reprobe_failure_restores_the_previous_artifact",),
    ),
    Probe(
        name="M8 publish keeps no backup of the previous artifact",
        target=REPO / "src/nexus_ai_agent/jobs/feature_verification.py",
        anchor="        _keep_previous(published, backup)\n        os.replace(staged, published)",
        mutant="        os.replace(staged, published)  # MUTATION: no backup before the rename",
        tests=(
            f"{F}test_t9_reprobe_failure_restores_the_previous_artifact",
            f"{F}test_t10_publish_failure_midway_preserves_previous_artifact",
        ),
    ),
    Probe(
        name="M9 resume_pending ignores the stale_after window",
        target=REPO / "src/nexus_ai_agent/adapters/in_process_job_queue.py",
        anchor=(
            "                        if started_at is None or started_at >= cutoff:\n"
            "                            continue"
        ),
        mutant=(
            "                        if False:  # MUTATION: every in-flight row is taken over\n"
            "                            continue"
        ),
        tests=(f"{F}test_t7_takeover_without_valid_expiry_is_rejected",),
    ),
    Probe(
        name="M10 bare success=False treated as success",
        target=REPO / "src/nexus_ai_agent/jobs/failure_semantics.py",
        anchor=(
            "    if is_typed_user_failure(result):\n"
            '        return str(result["error_code"])\n'
            "    return UNSPECIFIED_FAILURE_CODE"
        ),
        mutant=(
            "    if is_typed_user_failure(result):\n"
            '        return str(result["error_code"])\n'
            "    return None  # MUTATION: untyped failure completes"
        ),
        tests=(f"{F}test_r4_bare_success_false_is_a_failure_not_a_completion",),
    ),
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_tests(tests: tuple[str, ...]) -> int:
    # ``PYTHONPATH`` pins the *mutated* tree: when the campaign runs in an
    # isolated worktree (the only safe place for it — a crash must never leave
    # a production checkout mutated) an editable install of the package would
    # otherwise shadow the mutation and every probe would "survive" (GOV-2).
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    result = subprocess.run(
        [*PYTEST, *tests], cwd=REPO, capture_output=True, text=True, env=env, check=False
    )
    return result.returncode


def _apply(probe: Probe) -> None:
    text = probe.target.read_text()
    for anchor, _mutant in probe.edits():
        if text.count(anchor) != 1:
            raise SystemExit(
                f"anchor for {probe.name} is not unique in {probe.target}: "
                f"{text.count(anchor)} matches"
            )
    for anchor, mutant in probe.edits():
        text = text.replace(anchor, mutant, 1)
    probe.target.write_text(text)


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
