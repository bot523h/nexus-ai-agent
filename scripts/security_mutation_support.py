"""Common fail-closed runner for the task-196 security mutation gates.

Each seam script applies controlled source weakenings, verifies that the
corresponding behavioral/architecture assertions go red, restores every file
byte-for-byte, and reruns its baseline tests green. The runner preserves any
pre-existing local edits because restoration uses the source text captured at
invocation time rather than a Git checkout.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class SourceEdit:
    path: str
    old: str
    new: str


@dataclass(frozen=True)
class Mutation:
    name: str
    edits: tuple[SourceEdit, ...]
    tests: tuple[str, ...]
    expected_failures: tuple[str, ...]
    rationale: str


def _run_tests(tests: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(ROOT / "src"), env.get("PYTHONPATH", "")) if part
    )
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *tests],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _invalidate_bytecode(paths: set[Path]) -> None:
    for path in paths:
        try:
            Path(importlib.util.cache_from_source(str(path))).unlink(missing_ok=True)
        except (NotImplementedError, OSError, ValueError):
            continue


def _tail(result: subprocess.CompletedProcess[str]) -> str:
    output = (result.stdout + result.stderr).strip()
    return output[-4000:] if output else "(pytest produced no output)"


def _apply(mutation: Mutation) -> dict[Path, str] | None:
    originals: dict[Path, str] = {}
    updated: dict[Path, str] = {}
    for edit in mutation.edits:
        path = ROOT / edit.path
        if path not in originals:
            originals[path] = path.read_text(encoding="utf-8")
            updated[path] = originals[path]
        if edit.old not in updated[path]:
            print(f"{mutation.name}: MUTATION DOES NOT APPLY in {edit.path}")
            return None
        updated[path] = updated[path].replace(edit.old, edit.new, 1)
    try:
        for path, text in updated.items():
            path.write_text(text, encoding="utf-8")
    except BaseException:
        for path, text in originals.items():
            path.write_text(text, encoding="utf-8")
        _invalidate_bytecode(set(originals))
        raise
    _invalidate_bytecode(set(updated))
    return originals


def run_mutation_suite(
    seam: str,
    baseline_tests: tuple[str, ...],
    mutations: tuple[Mutation, ...],
) -> int:
    print(f"{seam}: baseline ...", end=" ", flush=True)
    baseline = _run_tests(baseline_tests)
    if baseline.returncode:
        print("RED — refusing to mutate a failing baseline")
        print(_tail(baseline))
        return 2
    print("GREEN")

    killed = 0
    survivors: list[str] = []
    restoration_failed = False
    for mutation in mutations:
        originals = _apply(mutation)
        if originals is None:
            survivors.append(mutation.name)
            continue

        try:
            result = _run_tests(mutation.tests)
        finally:
            for path, text in originals.items():
                path.write_text(text, encoding="utf-8")
            _invalidate_bytecode(set(originals))

        if any(path.read_text(encoding="utf-8") != text for path, text in originals.items()):
            restoration_failed = True
            print(f"{mutation.name}: RESTORE FAILED")
            survivors.append(mutation.name)
            continue

        output = result.stdout + result.stderr
        if result.returncode and all(token in output for token in mutation.expected_failures):
            killed += 1
            print(f"{mutation.name}: killed — {mutation.rationale}")
        else:
            survivors.append(mutation.name)
            if result.returncode:
                print(f"{mutation.name}: unexpected failure (not counted as killed)")
                print(_tail(result))
            else:
                print(f"{mutation.name}: SURVIVED — {mutation.rationale}")

    print(f"{seam}: restored baseline ...", end=" ", flush=True)
    restored = _run_tests(baseline_tests)
    if restored.returncode or restoration_failed:
        print("RED — restore verification failed")
        print(_tail(restored))
        return 3
    print("GREEN")
    print(f"{seam}: {killed}/{len(mutations)} mutants killed")
    if survivors:
        print("survivors: " + ", ".join(survivors))
    return 0 if killed == len(mutations) else 1
