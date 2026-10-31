#!/usr/bin/env python3
"""task-283 — mutation probes for the queue-ownership / publication closure.

A test that passes is not evidence that the invariant is *load-bearing*.  For
each guard this closure added, this harness replaces the exact source line with
a mutant that removes the guard, requires the targeted tests to flip
GREEN -> RED, restores the original bytes, and requires GREEN again.  The run
is idempotent and leaves the tree byte-identical.

=====  ==============================================  ==============================
M1     ownership acquisition never takes the OS lock   single-owner exclusion
M2     release never closes the descriptor             descriptor leak
M3     release unlinks the lock anchor                 two owners on two inodes
M4     unreadable sidecar is treated as ownership      fail-closed publication
M5     attempt identity is not compared               stale-attempt retraction
M6     publication writes no ownership evidence        proof-before-mutation
M7     published namespace is flat, not per job        cross-job overwrite
M8     containment uses a string prefix                sibling-prefix misclassification
=====  ==============================================  ==============================

Layered defense (same convention as ``scripts/execution_core_mutations.py``):
cross-job retraction is refused by *two* independent layers — the per-job
published namespace (M7) and the sidecar's ``job_id`` comparison.  Removing
only one is survivable **by design**, so neither is listed as its own probe;
M7 proves the property is load-bearing and the job-identity comparison is kept
as defence in depth.  M5 covers the *attempt* fence, which has no second layer.

Self-integrity: a probe writes a mutant into a real source file, so the harness
snapshots every touched file first and asserts byte-identity at exit.  (An
earlier revision of this script supported a two-file "layered" mutation and
allowed ``target is target2``; it then captured the *already mutated* bytes as
its restore point and silently left a mutant in the tree.  That machinery is
gone, and the snapshot check makes any future leak a loud failure instead of a
quiet one.)
Exit code 0 only when every mutant was killed and every restore was green.
"""

from __future__ import annotations

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

QUEUE = REPO / "src/nexus_ai_agent/adapters/in_process_job_queue.py"
RENDER = REPO / "src/nexus_ai_agent/creative/render_jobs.py"

LIFECYCLE_T = "tests/integration/test_queue_ownership_lifecycle.py::"
PROCESSES_T = "tests/integration/test_queue_ownership_processes.py::"
PUB_T = "tests/integration/test_artifact_publication_safety.py::"


@dataclass(frozen=True)
class Probe:
    name: str
    target: Path
    anchor: str
    mutant: str
    tests: tuple[str, ...]


PROBES: tuple[Probe, ...] = (
    Probe(
        name="M1 acquisition never takes the OS lock",
        target=QUEUE,
        anchor="""        try:
            _lock_descriptor(fd, blocking=False)
        except OSError:""",
        mutant="""        try:
            pass  # MUTANT: ownership is claimed without any OS lock
        except OSError:""",
        tests=(
            LIFECYCLE_T + "test_second_live_instance_cannot_acquire",
            PROCESSES_T + "test_second_live_process_is_refused",
        ),
    ),
    Probe(
        name="M2 release never closes the descriptor",
        target=QUEUE,
        anchor="""        _unlock_descriptor(fd)
        try:
            os.close(fd)
        except OSError:  # pragma: no cover - already closed
            pass""",
        mutant="""        _unlock_descriptor(fd)
        # MUTANT: the descriptor is deliberately leaked""",
        tests=(LIFECYCLE_T + "test_lock_descriptor_is_closed_on_release",),
    ),
    Probe(
        name="M3 release unlinks the lock anchor",
        target=QUEUE,
        anchor="""        _unlock_descriptor(fd)
        try:
            os.close(fd)""",
        mutant="""        _unlock_descriptor(fd)
        try:
            os.unlink(lock_path)  # MUTANT: unlink the anchor after releasing
            os.close(fd)""",
        tests=(LIFECYCLE_T + "test_lock_file_is_never_deleted_on_release",),
    ),
    Probe(
        name="M4 unreadable sidecar is treated as ownership",
        target=RENDER,
        anchor="""    document = _read_sidecar(sidecar_path)
    if document is None:
        return  # missing/unreadable/malformed sidecar -> fail closed""",
        mutant="""    document = _read_sidecar(sidecar_path)
    if document is None:  # MUTANT: absence of proof is read as permission
        document = {"job_id": staging.job_id, "attempt": str(_qatt)}""",
        tests=(
            PUB_T + "test_retract_fails_closed_when_sidecar_is_missing",
            PUB_T + "test_retract_fails_closed_on_malformed_sidecar",
            PUB_T + "test_retract_fails_closed_when_sidecar_is_unreadable",
        ),
    ),
    Probe(
        name="M5 attempt identity is not compared",
        target=RENDER,
        anchor="""    if str(recorded_attempt) != str(caller_attempt):
        return  # a newer (or older) attempt owns it -> never retract it""",
        mutant="""    if False:  # MUTANT: any attempt may retract the destination
        return""",
        tests=(PUB_T + "test_older_attempt_cannot_retract_a_newer_artifact",),
    ),
    Probe(
        name="M6 publication writes no ownership evidence",
        target=RENDER,
        anchor="    _write_sidecar(sidecar_path, job_id=staging.job_id, attempt=attempt_component)",
        mutant="    pass  # MUTANT: publish without recording who owns it",
        tests=(
            PUB_T + "test_sidecar_records_complete_identity",
            PUB_T + "test_crash_after_backup_before_publish_is_recoverable",
        ),
    ),
    Probe(
        name="M7 published namespace is flat, not per job",
        target=RENDER,
        anchor="""    job_id = staging.job_id
    job_dir = Path(final_root) / job_id""",
        mutant="""    job_id = staging.job_id
    job_dir = Path(final_root)  # MUTANT: one flat namespace for every job""",
        tests=(
            PUB_T + "test_two_jobs_with_identical_filenames_do_not_collide",
            PUB_T + "test_one_jobs_publication_cannot_clobber_anothers_backup",
        ),
    ),
    Probe(
        name="M8 containment uses a string prefix",
        target=RENDER,
        anchor="""    try:
        return child.resolve(strict=False).is_relative_to(parent.resolve(strict=False))
    except (OSError, RuntimeError):  # pragma: no cover - unresolvable path
        return False""",
        mutant="""    # MUTANT: the classic substring/prefix containment bug
    return str(child).startswith(str(parent))""",
        tests=(
            PUB_T + "test_resolved_containment_rejects_sibling_prefix",
            PUB_T + "test_prefix_sibling_is_treated_as_published_output",
        ),
    ),
)


def _run(tests: tuple[str, ...]) -> int:
    return subprocess.run(
        [*PYTEST, *tests],
        cwd=str(REPO),
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    ).returncode


def main() -> int:
    failures: list[str] = []
    targets = sorted({probe.target for probe in PROBES})
    pristine = {path: path.read_bytes() for path in targets}

    for probe in PROBES:
        original = probe.target.read_bytes()
        source = original.decode("utf-8")
        if probe.anchor not in source:
            failures.append(f"{probe.name}: anchor not found (source drifted)")
            print(f"SKIP  {probe.name}: anchor not found")
            continue
        print(f"[{probe.name}] baseline ...", flush=True)
        if _run(probe.tests) != 0:
            failures.append(f"{probe.name}: baseline is not green")
            print(f"FAIL  {probe.name}: baseline is not green")
            continue
        try:
            probe.target.write_bytes(source.replace(probe.anchor, probe.mutant, 1).encode("utf-8"))
            mutated = _run(probe.tests)
        finally:
            probe.target.write_bytes(original)
        if mutated == 0:
            failures.append(f"{probe.name}: mutant SURVIVED (tests stayed green)")
            print(f"SURVIVED  {probe.name}")
            continue
        restored = _run(probe.tests)
        if restored != 0:
            failures.append(f"{probe.name}: tree not restored to green")
            print(f"BROKEN  {probe.name}: restore is not green")
            continue
        print(f"KILLED  {probe.name}")

    # Self-integrity: the tree must be byte-identical to how we found it.
    for path in targets:
        if path.read_bytes() != pristine[path]:
            failures.append(f"harness left a mutant in {path.relative_to(REPO)}")

    if failures:
        print("\nUNPROVEN INVARIANTS:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"\nAll {len(PROBES)} mutants killed; tree restored and green.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
