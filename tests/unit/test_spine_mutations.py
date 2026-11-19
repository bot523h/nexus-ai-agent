"""Adversarial mutation tests for the creative spine (D-0025).

A guard that has never been attacked is not evidence. Each mutation below
weakens one spine invariant in place, runs the spine suite, and requires it to
turn RED. Mutated files are tested inside an isolated temporary copy of ``src/``
so the working tree is never altered, guaranteeing zero cross-test pollution.

The mutations are applied to files under ``src/nexus_ai_agent/creative/spine/``;
the suite is run with ``--noconftest`` so it needs no optional dependencies.
"""

# ruff: noqa: E501 - the MUTATIONS table holds verbatim source snippets; they must
# match the shipped files byte-for-byte, so they cannot be re-wrapped.
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SPINE = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "spine"
TESTS = ("tests/unit/test_creative_spine.py",)


@dataclass(frozen=True)
class Mutation:
    name: str
    target: Path
    old: str
    new: str
    why: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "no_operations_becomes_a_silent_success",
        SPINE / "execution.py",
        '        if not planned:\n            raise ValueError(\n                "intent compiled to no operations; the rules compiler does not understand it"\n            )',
        "        if not planned:\n            return None  # mutation: pretend an empty plan succeeded",
        "an intent with no compiled operations must be refused, not silently 'succeed'",
    ),
    Mutation(
        "evidence_is_not_recorded",
        SPINE / "execution.py",
        '        self._graph.add_node(\n            "evidence",',
        '        if False:\n            self._graph.add_node(\n            "evidence",',
        "every artifact must be bound to evidence in the graph",
    ),
    Mutation(
        "capability_nodes_are_not_planned",
        SPINE / "compiler.py",
        "        for planned in compiled.plan:\n            graph.add_node(",
        "        for planned in []:\n            graph.add_node(",
        "each planned operation must be recorded as a capability node",
    ),
    Mutation(
        "compiler_skips_registry_check",
        SPINE / "compiler.py",
        "            try:\n                registry.get_spec(operation)\n            except UnknownOperationError as exc:",
        "            try:\n                pass\n            except UnknownOperationError as exc:",
        "the compiler must validate every operation against the registry",
    ),
    Mutation(
        "recipe_becomes_a_copy",
        SPINE / "reference.py",
        '        constraints=("do not copy the reference; re-apply the strategy to own assets",),',
        "        constraints=(),",
        "a recipe-derived intent must carry the no-copy constraint",
    ),
    Mutation(
        "failed_plan_keeps_the_steps_it_already_committed",
        SPINE / "execution.py",
        "            self._rollback(intent, committed_transactions, artifact_nodes)\n            raise",
        "            raise  # mutation: no rollback, the half-applied plan stands",
        "a refused later step must roll back the steps already committed",
    ),
    Mutation(
        "rolled_back_run_keeps_its_artifact_nodes",
        SPINE / "execution.py",
        "        for node_id in reversed(artifact_nodes):\n            self._graph.remove_node(node_id)",
        "        for node_id in []:\n            self._graph.remove_node(node_id)",
        "a rolled-back run must not leave artifact nodes in the graph",
    ),
    Mutation(
        "rollback_ignores_transaction_identity",
        SPINE / "execution.py",
        '"input": {"transaction_id": transaction_id},',
        '"input": {},  # mutation: the rollback drops transaction identity',
        "a rollback must name the transaction it undoes, not rewind the newest",
    ),
    Mutation(
        "duplicate_delivery_reapplies_the_plan",
        SPINE / "execution.py",
        "        cached = self._completed.get(intent.intent_id)\n        if cached is not None:",
        "        cached = None  # mutation: no exactly-once replay\n        if cached is not None:",
        "a duplicate delivery of an intent must not mutate the project twice",
    ),
)


def _run_spine_tests(src_path: Path) -> bool:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src_path)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "--noconftest", *TESTS],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode == 0


def test_spine_mutations_are_all_killed(tmp_path: Path) -> None:
    # Copy src into an isolated temporary directory so workspace files are never mutated in-place
    tmp_src = tmp_path / "src"
    shutil.copytree(REPO_ROOT / "src", tmp_src)

    assert _run_spine_tests(tmp_src), "baseline spine suite must be green before mutating"

    survivors: list[str] = []
    for mutation in MUTATIONS:
        rel_target = mutation.target.relative_to(REPO_ROOT / "src")
        tmp_target = tmp_src / rel_target

        original = tmp_target.read_text(encoding="utf-8")
        if mutation.old not in original:
            survivors.append(f"{mutation.name} (MUTATION DOES NOT APPLY — source drifted)")
            continue

        tmp_target.write_text(original.replace(mutation.old, mutation.new, 1), encoding="utf-8")
        try:
            green = _run_spine_tests(tmp_src)
        finally:
            tmp_target.write_text(original, encoding="utf-8")

        if green:
            survivors.append(f"{mutation.name}: SURVIVED — {mutation.why}")

    assert not survivors, "spine mutants survived (a guard is fake):\n" + "\n".join(survivors)
