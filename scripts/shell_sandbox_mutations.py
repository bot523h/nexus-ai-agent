#!/usr/bin/env python3
"""Adversarial mutation harness for the restricted-shell sandbox (ADR 0007).

Evidence rule, same as ``scripts/pack_trust_mutations.py``: a guard that has
never been attacked is not evidence.  Each mutation below removes or weakens
*exactly one* load-bearing part of the shell sandbox, re-runs the sandbox suite,
and requires the suite to turn RED.  Every touched source file is restored
afterwards — including on failure — and the final run must be GREEN again.

A surviving mutant means the guard is not actually enforced by any test, i.e.
the invariant is documentation rather than code.  Survivors are reported by
name; the run exits non-zero.

Usage::

    python scripts/shell_sandbox_mutations.py            # run every mutation
    python scripts/shell_sandbox_mutations.py --list

Exit code 0 means "N/N mutants killed"; anything else means a guard is fake.
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
TOOLS = ROOT / "src" / "nexus_ai_agent" / "tools"
#: Every suite that owns part of the sandbox contract: the command surface, and
#: the physical boundary the command surface delegates to.
TESTS = (
    "tests/unit/test_shell_sandbox.py",
    "tests/unit/test_filesystem_boundary.py",
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
        "undeclared_flag_accepted",
        TOOLS / "system_shell.py",
        "        kind = self.FLAGS[command].get(flag)\n        if kind is None:\n            raise self._refusal(command, flag)",
        "        kind = self.FLAGS[command].get(flag)\n        if kind is None:\n            return (FlagKind.BOOL.value,)",
        "the whole point of the grammar: an unknown flag must be refused",
    ),
    Mutation(
        "path_flag_value_not_validated",
        TOOLS / "system_shell.py",
        '        if kind == "path":\n            # M1: the table says this token is a file the command will open.\n            self._validate_path(value, what=f"value of {flag}")',
        '        if kind == "path":\n            pass',
        "a flag declared PATH must have its value checked (grep -f, find -newer)",
    ),
    Mutation(
        "path_shaped_net_disabled",
        TOOLS / "system_shell.py",
        '        candidate = token\n        if token.startswith("--") and "=" in token:\n            candidate = token.partition("=")[2]\n        if self._looks_like_a_path(candidate):\n            self._validate_path(candidate, what=what)',
        "        return",
        "the independent net must contain absolute/traversing tokens anywhere",
    ),
    Mutation(
        "positional_paths_not_validated",
        TOOLS / "system_shell.py",
        '        policy = _POSITIONAL_POLICY.get(command)\n        if policy == "none":',
        '        policy = _POSITIONAL_POLICY.get(command)\n        if policy == "none" or True:\n            return\n        if policy == "none":',
        "positional file arguments must stay inside the workspace",
    ),
    Mutation(
        "attached_short_value_dropped",
        TOOLS / "system_shell.py",
        "            remainder = cluster[position + 1 :] or None",
        "            remainder = None",
        "the attached spelling (-fFILE) must be validated, not silently passed on",
    ),
    Mutation(
        "grep_positional_policy_ignores_e_and_f",
        TOOLS / "system_shell.py",
        "            targets = positionals if pattern_supplied else positionals[1:]",
        "            targets = positionals[1:]",
        "with -e/-f present, every grep positional is a file and must be checked",
    ),
    Mutation(
        "bool_flag_consumes_next_token",
        TOOLS / "system_shell.py",
        '            if kind == "bool":\n                # Consumes nothing.  Reached for whole-word flags (``find -o``,\n                # ``find -print0``); a ``bool`` never swallows the next token.\n                continue\n',
        "",
        "a boolean flag must never swallow the following token",
    ),
    Mutation(
        "find_unknown_expression_token_accepted",
        TOOLS / "system_shell.py",
        'raise ShellCommandError(f"Unexpected token in a find expression: {token!r}")',
        "pass",
        "nothing the grammar cannot account for may reach the subprocess",
    ),
    # The two table mutations below are anchored on the table *definition*
    # rather than on a list item.  Rewriting those lists into one-item-per-line
    # is exactly what a formatter does, so an anchor made of list items can be
    # moved out from under the harness by a ``ruff format`` run — and a mutation
    # that quietly stops applying is worse than no mutation, because the harness
    # still reports a clean "N/N killed".  Definition lines are formatter-stable.
    Mutation(
        "find_follow_links_declared_safe",
        TOOLS / "system_shell.py",
        "_FIND_FLAGS: dict[str, FlagKind] = {",
        '_FIND_FLAGS: dict[str, FlagKind] = { "-L": FlagKind.BOOL, "-H": FlagKind.BOOL, "-follow": FlagKind.BOOL,',
        "find -L/-H/-follow must never be declared safe: they leave the workspace",
    ),
    Mutation(
        "grep_dereference_recursive_declared_safe",
        TOOLS / "system_shell.py",
        "_GREP_FLAGS: dict[str, FlagKind] = {",
        '_GREP_FLAGS: dict[str, FlagKind] = { "-R": FlagKind.BOOL, "--dereference-recursive": FlagKind.BOOL,',
        "grep -R follows in-tree symlinks and measured a real escape",
    ),
    Mutation(
        "symlink_component_check_removed",
        TOOLS / "filesystem_policy.py",
        "        self._reject_existing_symlink_components(parts)",
        "        pass",
        "the physical boundary must reject paths with a symlink component",
    ),
)


def run_tests() -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *TESTS],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
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
        mutation.path.write_text(original.replace(mutation.old, mutation.new, 1), encoding="utf-8")
        try:
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
    return 0 if killed == len(MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
