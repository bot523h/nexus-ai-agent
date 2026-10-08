#!/usr/bin/env python3
# ruff: noqa: E501  (the anchors/mutants are verbatim source lines, kept intact)
"""NEXUS V1 execution-core mutation probes (M1–M6).

A targeted mutation harness in the same shape as the established
``scripts/gate5_mutation_probes.py``: for each correctness property this
closure added, a mutant is applied to the exact source line, the targeted
tests must flip GREEN -> RED, the source bytes are restored, and the tests
must be GREEN again.  The run is idempotent and leaves the tree byte-identical.

====  ============================================  ============================
M1    drop the attempt fence from the final commit  double-commit / stale races
M2    make stale cancel ignore the fencing token    stale-cancel test
M3    reconcile uses unscoped startup takeover      reconcile isolation tests
M4    publish through a symlinked staged source     staging security tests
M5    quarantine through a symlinked destination    quarantine boundary tests
M6    notify success before the durable commit      notification ordering test
====  ============================================  ============================

Usage:  .venv/bin/python scripts/execution_core_mutations.py
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:warnings", "--tb=no"]

QUEUE = REPO / "src/nexus_ai_agent/adapters/in_process_job_queue.py"
BACKEND = REPO / "src/nexus_ai_agent/adapters/native_local_backend.py"
STAGING = REPO / "src/nexus_ai_agent/execution/staging.py"

RACES = "tests/integration/test_execution_races.py::"
BACKEND_T = "tests/integration/test_execution_native_backend.py::"
STAGING_T = "tests/unit/test_execution_staging.py::"


@dataclass(frozen=True)
class Probe:
    name: str
    target: Path
    anchor: str
    mutant: str
    tests: tuple[str, ...]


PROBES: tuple[Probe, ...] = (
    Probe(
        name="M1 final completion CAS ignores the attempt fence",
        target=QUEUE,
        anchor=(
            "            row = connection.execute(\n"
            '                """\n'
            "                SELECT attempt_history_json FROM nexus_job_queue\n"
            "                WHERE id = ? AND status IN (?, ?) AND attempt = ?\n"
            '                """,\n'
            "                (\n"
            "                    claim.job_id,\n"
            "                    JobStatus.PROCESSING.value,\n"
            "                    JobStatus.VERIFYING.value,\n"
            "                    claim.attempt,\n"
            "                ),\n"
            "            ).fetchone()\n"
            "            if row is None:\n"
            "                return False\n"
            '            history = self._decode_attempt_history(str(row["attempt_history_json"] or "[]"))\n'
            "            attempt = next(\n"
            "                (\n"
            "                    entry\n"
            "                    for entry in history\n"
            "                    if self._attempt_number(entry) == claim.attempt\n"
            '                    and entry.get("status") in {"processing", "verifying", "verified"}\n'
            "                ),\n"
            "                None,\n"
            "            )\n"
            "            if attempt is None:\n"
            "                return False\n"
            "            now = _now()\n"
            '            attempt["status"] = attempt_status\n'
            '            attempt["finished_at"] = now\n'
            '            attempt["error"] = None\n'
            '            if attempt_status == "reconciled":\n'
            '                attempt["reconciled_from_attempt_id"] = reconciled_from_attempt_id\n'
            "            else:\n"
            '                attempt["execution_result_json"] = result_json\n'
            "                if stored_passport is not None:\n"
            '                    attempt["passport_json"] = stored_passport\n'
            "            cursor = connection.execute(\n"
            '                """\n'
            "                UPDATE nexus_job_queue\n"
            "                SET status = ?, result_json = ?, artifact_passport_json = ?,\n"
            "                    attempt_history_json = ?, error = NULL, finished_at = ?\n"
            "                WHERE id = ? AND status IN (?, ?) AND attempt = ?\n"
            '                """,\n'
            "                (\n"
            "                    JobStatus.COMPLETED.value,\n"
            "                    result_json,\n"
            "                    stored_passport,\n"
            "                    canonical_json(history),\n"
            "                    now,\n"
            "                    claim.job_id,\n"
            "                    JobStatus.PROCESSING.value,\n"
            "                    JobStatus.VERIFYING.value,\n"
            "                    claim.attempt,\n"
            "                ),\n"
            "            )\n"
            "            return cursor.rowcount == 1"
        ),
        mutant=(
            "            row = connection.execute(\n"
            '                """\n'
            "                SELECT attempt_history_json FROM nexus_job_queue\n"
            "                WHERE id = ? AND status IN (?, ?) AND ? IS NOT NULL\n"
            '                """,\n'
            "                (\n"
            "                    claim.job_id,\n"
            "                    JobStatus.PROCESSING.value,\n"
            "                    JobStatus.VERIFYING.value,\n"
            "                    claim.attempt,\n"
            "                ),\n"
            "            ).fetchone()\n"
            "            if row is None:\n"
            "                return False\n"
            '            history = self._decode_attempt_history(str(row["attempt_history_json"] or "[]"))\n'
            "            attempt = next(\n"
            "                (\n"
            "                    entry\n"
            "                    for entry in history\n"
            "                    if True  # MUTATION: no attempt/status ledger fence\n"
            "                ),\n"
            "                None,\n"
            "            )\n"
            "            if attempt is None:\n"
            "                return False\n"
            "            now = _now()\n"
            '            attempt["status"] = attempt_status\n'
            '            attempt["finished_at"] = now\n'
            '            attempt["error"] = None\n'
            '            if attempt_status == "reconciled":\n'
            '                attempt["reconciled_from_attempt_id"] = reconciled_from_attempt_id\n'
            "            else:\n"
            '                attempt["execution_result_json"] = result_json\n'
            "                if stored_passport is not None:\n"
            '                    attempt["passport_json"] = stored_passport\n'
            "            cursor = connection.execute(\n"
            '                """\n'
            "                UPDATE nexus_job_queue\n"
            "                SET status = ?, result_json = ?, artifact_passport_json = ?,\n"
            "                    attempt_history_json = ?, error = NULL, finished_at = ?\n"
            "                WHERE id = ? AND status IN (?, ?) AND ? IS NOT NULL\n"
            '                """,\n'
            "                (\n"
            "                    JobStatus.COMPLETED.value,\n"
            "                    result_json,\n"
            "                    stored_passport,\n"
            "                    canonical_json(history),\n"
            "                    now,\n"
            "                    claim.job_id,\n"
            "                    JobStatus.PROCESSING.value,\n"
            "                    JobStatus.VERIFYING.value,\n"
            "                    claim.attempt,\n"
            "                ),\n"
            "            )\n"
            "            return cursor.rowcount == 1"
        ),
        tests=(
            f"{RACES}test_same_attempt_double_commit_has_exactly_one_winner",
            f"{BACKEND_T}test_stale_attempt_cannot_complete_after_takeover",
        ),
    ),
    Probe(
        name="M2 stale cancel ignores the fencing token",
        target=QUEUE,
        anchor="        if expected_attempt is not None and attempt != expected_attempt:",
        mutant="        if False:  # MUTATION: stale identity cancels a newer attempt",
        tests=(
            f"{RACES}test_stale_identity_cannot_cancel_a_newer_attempt",
            f"{RACES}test_current_identity_can_cancel_its_own_attempt",
        ),
    ),
    Probe(
        name="M3 reconcile uses unscoped startup takeover",
        target=BACKEND,
        anchor=("        if observation.state in _IN_FLIGHT and self._stale_after is not None:"),
        mutant=(
            "        if observation.state in _IN_FLIGHT:  # MUTATION: startup takeover at runtime\n"
            "            await self._queue.resume_pending(stale_after=self._stale_after)"
        ),
        tests=(
            f"{BACKEND_T}test_reconcile_without_a_stale_policy_never_takes_over",
            f"{BACKEND_T}test_reconcile_of_one_job_never_touches_an_unrelated_job",
        ),
    ),
    Probe(
        name="M4 publish follows a symlinked staged source",
        target=STAGING,
        anchor=(
            "        source_rel = self._source_relative(staged)\n"
            "        try:\n"
            "            self._fs.require_regular_file(source_rel)\n"
            "        except FilesystemBoundaryError as exc:\n"
            "            raise StagingBoundaryError(str(exc)) from exc\n"
            "        parts = _relative_parts(target_rel)\n"
            "        destination = self.final_root.joinpath(*parts)\n"
            "        resolved_destination = destination.resolve(strict=False)\n"
            "        if not resolved_destination.is_relative_to(self.final_root):\n"
            '            raise StagingBoundaryError("publish target escapes the declared final root")\n'
            "        if resolved_destination.is_relative_to(self.root):\n"
            '            raise StagingBoundaryError("publish target must not be inside the staging root")\n'
            "        _assert_no_symlink_components(self.final_root, parts)\n"
            '        dest_rel = "/".join(parts)\n'
            "        try:\n"
            "            self.final_root.mkdir(parents=True, exist_ok=True)\n"
            "            if len(parts) > 1:\n"
            '                self._final_fs.ensure_directory("/".join(parts[:-1]))\n'
            "            self._fs.rename_into(self._final_fs, source_rel, dest_rel)\n"
            "        except (FilesystemBoundaryError, OSError) as exc:\n"
            "            raise StagingBoundaryError(str(exc)) from exc\n"
            "        return resolved_destination"
        ),
        mutant=(
            "        resolved_source = Path(staged).resolve(strict=False)  # MUTATION: follows symlinks\n"
            "        if not resolved_source.is_relative_to(self.staging_dir):\n"
            '            raise StagingBoundaryError("staged source must live inside this attempt\'s staging dir")\n'
            "        if not resolved_source.is_file():\n"
            '            raise StagingBoundaryError("staged source does not exist")\n'
            "        parts = _relative_parts(target_rel)\n"
            "        destination = self.final_root.joinpath(*parts)\n"
            "        resolved_destination = destination.resolve(strict=False)\n"
            "        if not resolved_destination.is_relative_to(self.final_root):\n"
            '            raise StagingBoundaryError("publish target escapes the declared final root")\n'
            "        if resolved_destination.is_relative_to(self.root):\n"
            '            raise StagingBoundaryError("publish target must not be inside the staging root")\n'
            "        _assert_no_symlink_components(self.final_root, parts)\n"
            "        destination.parent.mkdir(parents=True, exist_ok=True)\n"
            "        os.replace(resolved_source, resolved_destination)  # MUTATION: symlink-following move\n"
            "        return resolved_destination"
        ),
        tests=(
            f"{STAGING_T}test_publish_rejects_a_symlink_substitution_inside_staging",
            f"{STAGING_T}test_publish_rejects_a_symlinked_staged_source",
        ),
    ),
    Probe(
        name="M5 quarantine crosses a symlinked destination",
        target=STAGING,
        anchor=(
            "        _assert_no_symlink_components(self.root, (self.job_id, self.attempt_id))\n"
            '        _assert_no_symlink_components(self.root, ("_quarantine", self.job_id))\n'
            "        try:\n"
            "            self.root.mkdir(parents=True, exist_ok=True)\n"
            '            self._fs.ensure_directory(f"_quarantine/{self.job_id}")\n'
            "            self._remove_destination(dest_rel)\n"
            "            self._fs.rename_within(self._attempt_rel(), dest_rel)\n"
            "        except (FilesystemBoundaryError, OSError) as exc:\n"
            "            raise StagingBoundaryError(str(exc)) from exc"
        ),
        mutant=(
            "        attempt_dir = self.root / self.job_id / self.attempt_id  # MUTATION: unsafe move\n"
            "        destination = (\n"
            '            self.root / "_quarantine" / self.job_id / f"{self.attempt_id}.{component}"\n'
            "        )\n"
            "        destination.parent.mkdir(parents=True, exist_ok=True)\n"
            "        if attempt_dir.exists() and not attempt_dir.is_symlink():\n"
            "            os.replace(attempt_dir, destination)"
        ),
        tests=(
            f"{STAGING_T}test_quarantine_rejects_a_symlinked_quarantine_root",
            f"{STAGING_T}test_quarantine_rejects_a_symlinked_quarantine_job_directory",
        ),
    ),
    Probe(
        name="M6 success notification before the durable commit",
        target=QUEUE,
        anchor=(
            "            if not await asyncio.to_thread(self._mark_completed, claim, final_result):\n"
            "                self._log_rejected(job_id, claim, JobStatus.COMPLETED)\n"
            "                return"
        ),
        mutant=(
            "            if not await asyncio.to_thread(self._mark_completed, claim, final_result):\n"
            "                # MUTATION: notify success even though the commit was refused\n"
            "                self._log_rejected(job_id, claim, JobStatus.COMPLETED)\n"
            "                await self._notify_completion(\n"
            "                    JobCompletion(\n"
            "                        job_id=job_id,\n"
            "                        job_type=job_type,\n"
            "                        status=JobStatus.COMPLETED,\n"
            "                        result=final_result,\n"
            "                        error=None,\n"
            "                        payload=payload,\n"
            "                    )\n"
            "                )\n"
            "                return"
        ),
        tests=(
            f"{RACES}test_refused_commit_emits_no_success_notification",
            f"{RACES}test_success_notification_only_after_the_commit",
        ),
    ),
)


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
            probe.target.write_bytes(original)
            continue
        print("  baseline: GREEN")

        _apply(probe)
        try:
            mutated = _run_tests(probe.tests)
        finally:
            probe.target.write_bytes(original)
        after_sha = hashlib.sha256(probe.target.read_bytes()).hexdigest()
        if after_sha != before_sha:
            failures.append(f"{probe.name}: source not restored byte-identically")
            print("  restore: FAILED (bytes differ)")
            continue

        if mutated == 0:
            failures.append(f"{probe.name}: mutation NOT caught (tests stayed GREEN)")
            print("  mutant: NOT CAUGHT — tests stayed GREEN")
            continue

        # Restored bytes must be GREEN again.
        restored = _run_tests(probe.tests)
        if restored != 0:
            failures.append(f"{probe.name}: restored tree not GREEN (exit {restored})")
            print(f"  restored: NOT GREEN (exit {restored})")
            continue
        caught += 1
        print(f"  mutant: CAUGHT (exit {mutated}); restored: GREEN; sha {before_sha[:12]}")

    print(f"\n=== summary: {caught}/{len(PROBES)} mutations caught")
    if failures:
        for failure in failures:
            print(f"  FAIL: {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
