#!/usr/bin/env python3
"""Adversarial mutation harness for the durable project/revision lifecycle (task-282).

Evidence rule: an invariant that has never been attacked is not evidence.  Each
mutation below rewrites one shipped correctness/safety guard, re-runs the
lifecycle suites, and requires them to turn RED.  Every source file is restored
afterwards -- including on failure -- and the final run must be GREEN again.

Usage::

    python scripts/durable_project_state_mutations.py          # run every mutation
    python scripts/durable_project_state_mutations.py --list

Exit code 0 means "N/N mutants killed".
"""

# ruff: noqa: E501 - the MUTATIONS table holds verbatim source snippets; they must
# match the shipped files byte-for-byte, so they cannot be re-wrapped.
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "src" / "nexus_ai_agent" / "creative" / "project_state" / "store.py"
PERSISTENCE = ROOT / "src" / "nexus_ai_agent" / "creative" / "studio" / "persistence.py"
TESTS = (
    "tests/unit/test_durable_project_state.py",
    "tests/integration/test_durable_project_concurrency.py",
    "tests/integration/test_durable_project_restart.py",
)


@dataclass(frozen=True)
class Mutation:
    name: str
    path: Path
    old: str
    new: str
    why: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "cas_predicate_removed",
        STORE,
        "                orm.ProjectRevisionHead.revision_id == expected_parent_revision_id,  # type: ignore[arg-type]",
        "                orm.ProjectRevisionHead.project_id == project_id,  # mutated: CAS predicate dropped",
        "the head advance must be a compare-and-swap on the expected parent; "
        "dropping it lets a stale writer silently overwrite a newer revision",
    ),
    Mutation(
        "cross_project_read_check_removed",
        STORE,
        "        if row.project_id != project_id:",
        "        if False:",
        "a revision must only be readable through the project that owns it; "
        "removing the check leaks another project's state",
    ),
    Mutation(
        "genesis_arbiter_defanged",
        STORE,
        """            try:
                await session.flush()
            except IntegrityError as exc:
                raise RevisionConflict("project already has a head; genesis refused") from exc
            return""",
        """            await session.flush()
            return""",
        "a second genesis must be refused as a typed RevisionConflict; defanging "
        "the primary-key arbiter lets a duplicate head overwrite the project",
    ),
    Mutation(
        "unset_cas_token_accepted",
        STORE,
        "        if expected_parent_revision_id is _UNSET:",
        "        if False:",
        "an unguarded commit (no CAS token) must be refused; accepting it turns "
        "every write into last-writer-wins",
    ),
    Mutation(
        "content_address_not_digested",
        PERSISTENCE,
        '    return "sha256:" + hashlib.sha256(state_bytes).hexdigest()',
        '    return "sha256:" + state_bytes.decode("utf-8")',
        "the revision id must be the content hash of the state bytes; returning "
        "the raw bytes breaks content addressing and immutability",
    ),
)

TEST_TIMEOUT_SECONDS = 600


def run_tests() -> bool:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-x", *TESTS],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(f"(test run exceeded {TEST_TIMEOUT_SECONDS}s) ", end="", flush=True)
        return False
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for mutation in MUTATIONS:
            print(f"{mutation.name}: {mutation.why}")
        return 0

    print("baseline ... ", end="", flush=True)
    if not run_tests():
        print("RED — refusing to mutate a suite that is already failing")
        return 2
    print("GREEN")

    killed = 0
    survivors: list[str] = []
    for mutation in MUTATIONS:
        original = mutation.path.read_text(encoding="utf-8")
        if mutation.old not in original:
            print(f"{mutation.name}: MUTATION DOES NOT APPLY (source drifted)")
            survivors.append(mutation.name)
            continue
        try:
            mutation.path.write_text(
                original.replace(mutation.old, mutation.new, 1), encoding="utf-8"
            )
            green = run_tests()
        finally:
            mutation.path.write_text(original, encoding="utf-8")
        if green:
            print(f"{mutation.name}: SURVIVED — {mutation.why}")
            survivors.append(mutation.name)
        else:
            killed += 1
            print(f"{mutation.name}: killed")

    print("restored baseline ... ", end="", flush=True)
    if not run_tests():
        print("RED — restore failed")
        return 3
    print("GREEN")
    print(f"{killed}/{len(MUTATIONS)} killed")
    if survivors:
        print("survivors: " + ", ".join(survivors))
    return 0 if killed == len(MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
