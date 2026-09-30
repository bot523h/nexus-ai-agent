"""Adversarial mutation tests for the creative spine (D-0024).

A guard that has never been attacked is not evidence. Each mutation below
weakens one spine invariant in place, runs the spine suite, and requires it to
turn RED. Mutated files are restored from memory in a ``finally`` block and
their sha256 is checked, so a crash can never leave the tree altered.

The mutations are applied to files under ``src/nexus_ai_agent/creative/spine/``;
the suite is run with ``--noconftest`` so it needs no optional dependencies.
"""

# ruff: noqa: E501 - the MUTATIONS table holds verbatim source snippets; they must
# match the shipped files byte-for-byte, so they cannot be re-wrapped.
from __future__ import annotations

import hashlib
import os
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
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_spine_tests() -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "--noconftest", *TESTS],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**_env(), "PYTHONPATH": str(REPO_ROOT / "src")},
    )
    return result.returncode == 0


def _env() -> dict[str, str]:
    return dict(os.environ)


def test_spine_mutations_are_all_killed() -> None:
    originals = {m.target: m.target.read_text(encoding="utf-8") for m in MUTATIONS}
    before = {path: _sha256(path) for path in originals}

    assert _run_spine_tests(), "baseline spine suite must be green before mutating"

    survivors: list[str] = []
    try:
        for mutation in MUTATIONS:
            original = originals[mutation.target]
            if mutation.old not in original:
                survivors.append(f"{mutation.name} (MUTATION DOES NOT APPLY — source drifted)")
                continue
            mutation.target.write_text(
                original.replace(mutation.old, mutation.new, 1), encoding="utf-8"
            )
            try:
                green = _run_spine_tests()
            finally:
                mutation.target.write_text(original, encoding="utf-8")
            if green:
                survivors.append(f"{mutation.name}: SURVIVED — {mutation.why}")
    finally:
        for path, text in originals.items():
            path.write_text(text, encoding="utf-8")

    for path in originals:
        assert _sha256(path) == before[path], f"restore changed {path}"
    assert not survivors, "spine mutants survived (a guard is fake):\n" + "\n".join(survivors)
