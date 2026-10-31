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
M9     shutdown unlocks without draining publishes     lock released under a live rename
M10    a closed queue still publishes                  publication after ownership
=====  ==============================================  ==============================

Layered defense (same convention as ``scripts/execution_core_mutations.py``):
cross-job retraction is refused by *two* independent layers — the per-job
published namespace (M7) and the sidecar's ``job_id`` comparison.  Removing
only one is survivable **by design**, so neither is listed as its own probe;
M7 proves the property is load-bearing and the job-identity comparison is kept
as defence in depth.  M5 covers the *attempt* fence, which has no second layer.
The finding-3 fix (a sidecar attempt must be a *canonical* positive integer) is
likewise two layers — :func:`_parse_attempt` validation inside
``_read_sidecar`` and the canonical comparison in the retract callback — so
removing either alone is refused by the other; the direct before/after proof for
that pair is recorded in ``docs/architecture/JOB_LIFECYCLE.md`` rather than
faked here as a surviving probe.

Self-integrity: a probe writes a mutant into a real source file, so the harness
snapshots every touched file first and asserts byte-identity at exit.  (An
earlier revision of this script supported a two-file "layered" mutation and
allowed ``target is target2``; it then captured the *already mutated* bytes as
its restore point and silently left a mutant in the tree.  That machinery is
gone, and the snapshot check makes any future leak a loud failure instead of a
quiet one.)
Exit code 0 only when every mutant was killed and every restore was green.

Invocation boundary (security review, ``dangerous-subprocess-use-audit``): the
harness spawns pytest with an **argv list** and the default ``shell=False``, so
no shell ever parses a string here.  The only variable inputs are test node ids,
which come from the frozen :data:`PROBES` table and are additionally validated
against :data:`_NODE_ID` before every spawn — anything that is not
``tests/<path>.py::<name>`` is refused instead of being passed to pytest as a
flag.  ``tests/unit/test_queue_ownership_mutations_harness.py`` pins both the
data path and the rejection behaviour.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
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
BARRIER_T = "tests/integration/test_queue_ownership_publication_barrier.py::"


#: A test node id this harness is willing to spawn pytest for.  Deliberately
#: narrow: a repository-relative ``tests/*.py`` path plus an optional node name.
#: It cannot express an option (``-`` prefix), a shell metacharacter, an
#: absolute path or a traversal segment.
_NODE_ID = re.compile(r"tests/[A-Za-z0-9_/]+\.py(::[A-Za-z0-9_\[\]\.\-]+)?")


def _validate_node_ids(tests: tuple[str, ...]) -> tuple[str, ...]:
    """Refuse anything that is not a plain ``tests/...::name`` node id.

    The probes are written by hand, so this is not an untrusted-input defence
    today; it is the invariant that keeps it one if a future probe is generated.
    """
    if not tests:
        raise ValueError("refusing to run pytest with no test selection")
    for node in tests:
        if not _NODE_ID.fullmatch(node):
            raise ValueError(f"refusing to run an unvalidated test id: {node!r}")
    return tests


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
        anchor="""    recorded_attempt = _parse_attempt(document.get("attempt"))
    if recorded_attempt is None or recorded_attempt != caller_attempt:
        return  # a newer (or older) attempt owns it -> never retract it""",
        mutant="""    recorded_attempt = _parse_attempt(document.get("attempt"))
    if False:  # MUTANT: any attempt may retract the destination
        return""",
        tests=(
            PUB_T + "test_older_attempt_cannot_retract_a_newer_artifact",
            PUB_T + "test_retract_refuses_a_different_attempt_of_the_same_job",
        ),
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
    Probe(
        name="M9 shutdown unlocks without draining publications",
        target=QUEUE,
        anchor="""        await self._drain_publications()""",
        mutant="""        pass  # MUTANT: release the lock with a publisher thread still running""",
        tests=(BARRIER_T + "test_shutdown_keeps_ownership_until_the_publication_thread_ends",),
    ),
    Probe(
        name="M10 a closed queue still publishes",
        target=QUEUE,
        anchor="""        if self._publishing_closed:""",
        mutant="""        if False:  # MUTANT: publish even though shutdown already started""",
        tests=(BARRIER_T + "test_publication_is_refused_once_shutdown_has_run",),
    ),
)


def _run(tests: tuple[str, ...]) -> int:
    """Run one probe's tests; returns pytest's exit code (0 = green)."""
    argv = [*PYTEST, *_validate_node_ids(tests)]
    return subprocess.run(  # noqa: S603 - argv list, shell=False, ids validated
        argv,
        cwd=str(REPO),
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        shell=False,  # explicit: no shell ever parses these arguments
        check=False,
    ).returncode


def _git(*args: str) -> str:
    """Best-effort git query; the harness must not fail because git is absent."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, shell=False
            ["git", *args],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
    except OSError:  # pragma: no cover - git missing
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mutation-prove the queue-ownership/publication guards."
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="write a JSON report (summary + per-probe status) to this path",
    )
    args = parser.parse_args(argv)

    failures: list[str] = []
    results: list[dict[str, object]] = []
    targets = sorted({probe.target for probe in PROBES})
    pristine = {path: path.read_bytes() for path in targets}

    def record(name: str, outcome: str, detail: str = "") -> None:
        results.append({"probe": name, "status": outcome, "detail": detail})

    try:
        for probe in PROBES:
            original = probe.target.read_bytes()
            source = original.decode("utf-8")
            if probe.anchor not in source:
                failures.append(f"{probe.name}: anchor not found (source drifted)")
                record(probe.name, "anchor_missing")
                print(f"SKIP  {probe.name}: anchor not found")
                continue
            print(f"[{probe.name}] baseline ...", flush=True)
            baseline = _run(probe.tests)
            if baseline != 0:
                failures.append(f"{probe.name}: baseline is not green")
                record(probe.name, "baseline_red", f"pytest exit {baseline}")
                print(f"FAIL  {probe.name}: baseline is not green")
                continue
            try:
                probe.target.write_bytes(
                    source.replace(probe.anchor, probe.mutant, 1).encode("utf-8")
                )
                mutated = _run(probe.tests)
            finally:
                probe.target.write_bytes(original)
            if mutated == 0:
                failures.append(f"{probe.name}: mutant SURVIVED (tests stayed green)")
                record(probe.name, "survived")
                print(f"SURVIVED  {probe.name}")
                continue
            restored = _run(probe.tests)
            if restored != 0:
                failures.append(f"{probe.name}: tree not restored to green")
                record(probe.name, "restore_broken", f"pytest exit {restored}")
                print(f"BROKEN  {probe.name}: restore is not green")
                continue
            record(probe.name, "killed", f"mutant pytest exit {mutated}")
            print(f"KILLED  {probe.name}")
    finally:
        # Self-integrity: the tree must be byte-identical to how we found it,
        # even if a probe raised.  This runs before any report is written.
        restored_bytes = {path: path.read_bytes() for path in targets}
        for path in targets:
            if restored_bytes[path] != pristine[path]:
                failures.append(f"harness left a mutant in {path.relative_to(REPO)}")
        summary = {
            "planned": len(PROBES),
            "executed": sum(1 for r in results if r["status"] != "anchor_missing"),
            "killed": sum(1 for r in results if r["status"] == "killed"),
            "survived": sum(1 for r in results if r["status"] == "survived"),
            "invalid": sum(
                1
                for r in results
                if r["status"] in {"anchor_missing", "baseline_red", "restore_broken"}
            ),
            "not_run": len(PROBES) - len(results),
        }
        report = {
            "accepted": not failures and summary["killed"] == len(PROBES),
            "summary": summary,
            "failures": failures,
            "probes": results,
            "provenance": {
                "git_commit": _git("rev-parse", "HEAD"),
                "worktree_clean": _git("status", "--porcelain") == "",
                "interpreter": sys.version.split()[0],
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "targets": [str(path.relative_to(REPO)) for path in targets],
                "target_hashes": {
                    str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in targets
                },
            },
        }
        if args.report is not None:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(f"report: {args.report}")

    if failures:
        print("\nUNPROVEN INVARIANTS:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"\nAll {len(PROBES)} mutants killed; tree restored and green.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
