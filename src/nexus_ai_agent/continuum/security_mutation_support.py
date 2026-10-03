"""Canonical fail-closed mutation runner and classifier for security/foundation gates.

Enforces the global mutation framework contract:
1. Expected command execution with explicit timeout handling.
2. Expected exit behavior (`returncode == 1` for pytest test failures).
3. Exact expected test node identification (`FAILED <pytest-nodeid>`).
4. Explicit distinction between:
   - KILLED (expected failure)
   - SURVIVED (tests passed)
   - UNEXPECTED_FAILURE (unrelated test failed or expected nodeid did not fail)
   - COLLECTION_ERROR (import/syntax/collection/usage error, exit 2/4/5)
   - INFRASTRUCTURE_ERROR (pytest internal error exit 3, signal, OS failure)
   - TIMEOUT (execution timed out — never counted as killed)
   - PRE_EXISTING_FAILURE (baseline was already failing before mutation)
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

_FAILED_LINE_RE = re.compile(r"^FAILED\s+(\S+?)(?:\s+-\s+.*)?$", re.MULTILINE)
_ERROR_LINE_RE = re.compile(r"^ERROR\s+(\S+?)(?:\s+-\s+.*)?$", re.MULTILINE)
_COLLECTION_ERROR_MARKERS = (
    "ERROR collecting ",
    "ImportError while importing test module",
    "ModuleNotFoundError:",
    "SyntaxError:",
    "IndentationError:",
    "collected 0 items / ",
)


class MutationStatus(str, Enum):
    KILLED = "KILLED"
    SURVIVED = "SURVIVED"
    UNEXPECTED_FAILURE = "UNEXPECTED_FAILURE"
    COLLECTION_ERROR = "COLLECTION_ERROR"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"
    TIMEOUT = "TIMEOUT"
    PRE_EXISTING_FAILURE = "PRE_EXISTING_FAILURE"


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


@dataclass(frozen=True)
class MutationOutcome:
    status: MutationStatus
    killed: bool
    returncode: int | None
    failed_nodeids: tuple[str, ...]
    error_nodeids: tuple[str, ...]
    expected_nodeids: tuple[str, ...]
    detail: str


def extract_failed_nodeids(output: str) -> tuple[str, ...]:
    """Extract exact pytest node IDs from ``FAILED <nodeid>`` summary lines."""
    seen: list[str] = []
    for match in _FAILED_LINE_RE.finditer(output):
        nodeid = match.group(1).strip()
        if nodeid and nodeid not in seen:
            seen.append(nodeid)
    return tuple(seen)


def extract_error_nodeids(output: str) -> tuple[str, ...]:
    """Extract exact pytest node IDs from ``ERROR <nodeid>`` summary lines."""
    seen: list[str] = []
    for match in _ERROR_LINE_RE.finditer(output):
        nodeid = match.group(1).strip()
        if nodeid and nodeid not in seen:
            seen.append(nodeid)
    return tuple(seen)


def _nodeid_matches(expected: str, actual_failed: tuple[str, ...]) -> bool:
    for nodeid in actual_failed:
        if nodeid == expected or nodeid.startswith(f"{expected}["):
            return True
    return False


def _all_actual_accounted_for(
    expected_nodeids: tuple[str, ...],
    actual_failed: tuple[str, ...],
) -> bool:
    for actual in actual_failed:
        if not any(actual == exp or actual.startswith(f"{exp}[") for exp in expected_nodeids):
            return False
    return True


def classify_mutation_outcome(
    *,
    returncode: int | None,
    output: str,
    expected_failures: tuple[str, ...],
    timed_out: bool = False,
    baseline_failed: bool = False,
    infrastructure_error: str | None = None,
) -> MutationOutcome:
    """Classify a single mutation test run under the strict global rule."""
    if not expected_failures or any("::" not in nodeid for nodeid in expected_failures):
        raise ValueError("expected_failures must be non-empty full pytest node IDs containing '::'")

    if baseline_failed:
        return MutationOutcome(
            status=MutationStatus.PRE_EXISTING_FAILURE,
            killed=False,
            returncode=returncode,
            failed_nodeids=(),
            error_nodeids=(),
            expected_nodeids=expected_failures,
            detail="baseline failed prior to mutation",
        )

    if timed_out:
        return MutationOutcome(
            status=MutationStatus.TIMEOUT,
            killed=False,
            returncode=returncode,
            failed_nodeids=(),
            error_nodeids=(),
            expected_nodeids=expected_failures,
            detail="mutation test execution timed out (never counted as killed)",
        )

    if infrastructure_error is not None or returncode is None:
        return MutationOutcome(
            status=MutationStatus.INFRASTRUCTURE_ERROR,
            killed=False,
            returncode=returncode,
            failed_nodeids=(),
            error_nodeids=(),
            expected_nodeids=expected_failures,
            detail=infrastructure_error or "missing process returncode",
        )

    failed_nodeids = extract_failed_nodeids(output)
    error_nodeids = extract_error_nodeids(output)

    if returncode == 0:
        return MutationOutcome(
            status=MutationStatus.SURVIVED,
            killed=False,
            returncode=0,
            failed_nodeids=failed_nodeids,
            error_nodeids=error_nodeids,
            expected_nodeids=expected_failures,
            detail="all executed tests passed; mutant survived",
        )

    if returncode < 0 or returncode == 3 or "INTERNALERROR>" in output:
        return MutationOutcome(
            status=MutationStatus.INFRASTRUCTURE_ERROR,
            killed=False,
            returncode=returncode,
            failed_nodeids=failed_nodeids,
            error_nodeids=error_nodeids,
            expected_nodeids=expected_failures,
            detail=f"pytest infrastructure/internal error (returncode={returncode})",
        )

    if (
        returncode in (2, 4, 5)
        or bool(error_nodeids)
        or any(marker in output for marker in _COLLECTION_ERROR_MARKERS)
    ):
        return MutationOutcome(
            status=MutationStatus.COLLECTION_ERROR,
            killed=False,
            returncode=returncode,
            failed_nodeids=failed_nodeids,
            error_nodeids=error_nodeids,
            expected_nodeids=expected_failures,
            detail=f"collection/import/usage error (returncode={returncode})",
        )

    if returncode != 1:
        return MutationOutcome(
            status=MutationStatus.INFRASTRUCTURE_ERROR,
            killed=False,
            returncode=returncode,
            failed_nodeids=failed_nodeids,
            error_nodeids=error_nodeids,
            expected_nodeids=expected_failures,
            detail=f"unexpected non-1 returncode={returncode}",
        )

    if not failed_nodeids:
        return MutationOutcome(
            status=MutationStatus.COLLECTION_ERROR,
            killed=False,
            returncode=returncode,
            failed_nodeids=(),
            error_nodeids=error_nodeids,
            expected_nodeids=expected_failures,
            detail="returncode=1 but no 'FAILED <nodeid>' lines were emitted",
        )

    all_expected_failed = all(_nodeid_matches(exp, failed_nodeids) for exp in expected_failures)
    only_expected_failed = _all_actual_accounted_for(expected_failures, failed_nodeids)

    if all_expected_failed and only_expected_failed:
        return MutationOutcome(
            status=MutationStatus.KILLED,
            killed=True,
            returncode=1,
            failed_nodeids=failed_nodeids,
            error_nodeids=(),
            expected_nodeids=expected_failures,
            detail=f"exact expected failure(s): {', '.join(failed_nodeids)}",
        )

    return MutationOutcome(
        status=MutationStatus.UNEXPECTED_FAILURE,
        killed=False,
        returncode=1,
        failed_nodeids=failed_nodeids,
        error_nodeids=error_nodeids,
        expected_nodeids=expected_failures,
        detail=(
            f"failed nodeids {list(failed_nodeids)} did not match "
            f"expected {list(expected_failures)}"
        ),
    )


def legacy_weak_substring_killed(
    returncode: int | None,
    output: str,
    expected_tokens: tuple[str, ...],
) -> bool:
    """Historical weak classifier retained ONLY for negative regression tests.

    Demonstrates why ``returncode and all(token in output ...)`` is forbidden
    in production mutation gates: it false-kills on collection/import errors,
    unrelated failures that mention the token, and non-1 exit codes.
    """
    return bool(returncode and all(token in output for token in expected_tokens))


def _run_tests(
    tests: tuple[str, ...],
    *,
    timeout_seconds: float = 60.0,
) -> tuple[int | None, str, bool, str | None]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(ROOT / "src"), env.get("PYTHONPATH", "")) if part
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-rA", "--tb=short", *tests],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
        return (
            completed.returncode,
            completed.stdout + completed.stderr,
            False,
            None,
        )
    except subprocess.TimeoutExpired as exc:
        stdout_str = (
            exc.stdout.decode("utf-8", errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr_str = (
            exc.stderr.decode("utf-8", errors="replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        return None, stdout_str + stderr_str, True, "subprocess.TimeoutExpired"
    except OSError as exc:
        return None, "", False, f"OSError: {exc}"


def _invalidate_bytecode(paths: set[Path]) -> None:
    for path in paths:
        try:
            Path(importlib.util.cache_from_source(str(path))).unlink(missing_ok=True)
        except (NotImplementedError, OSError, ValueError):
            continue


def _tail(output: str) -> str:
    stripped = output.strip()
    return stripped[-4000:] if stripped else "(pytest produced no output)"


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
    *,
    timeout_seconds: float = 60.0,
) -> int:
    print(f"{seam}: baseline ...", end=" ", flush=True)
    base_rc, base_out, base_timeout, base_err = _run_tests(
        baseline_tests, timeout_seconds=timeout_seconds
    )
    if base_timeout or base_err is not None or base_rc != 0:
        status_label = MutationStatus.PRE_EXISTING_FAILURE.value
        print(f"RED ({status_label}) — refusing to mutate a failing baseline")
        print(_tail(base_out))
        return 2
    print("GREEN")

    killed = 0
    non_killed: list[str] = []
    restoration_failed = False
    for mutation in mutations:
        originals = _apply(mutation)
        if originals is None:
            non_killed.append(f"{mutation.name}(DOES_NOT_APPLY)")
            continue

        try:
            rc, out, timed_out, infra_err = _run_tests(
                mutation.tests, timeout_seconds=timeout_seconds
            )
        finally:
            for path, text in originals.items():
                path.write_text(text, encoding="utf-8")
            _invalidate_bytecode(set(originals))

        if any(path.read_text(encoding="utf-8") != text for path, text in originals.items()):
            restoration_failed = True
            print(f"{mutation.name}: RESTORE FAILED")
            non_killed.append(f"{mutation.name}(RESTORE_FAILED)")
            continue

        outcome = classify_mutation_outcome(
            returncode=rc,
            output=out,
            expected_failures=mutation.expected_failures,
            timed_out=timed_out,
            baseline_failed=False,
            infrastructure_error=infra_err,
        )
        if outcome.killed and outcome.status is MutationStatus.KILLED:
            killed += 1
            print(f"{mutation.name}: KILLED — {mutation.rationale} [{outcome.detail}]")
        else:
            non_killed.append(f"{mutation.name}({outcome.status.value})")
            print(
                f"{mutation.name}: {outcome.status.value} (not counted as killed) — "
                f"{outcome.detail}"
            )
            if outcome.status is not MutationStatus.SURVIVED:
                print(_tail(out))

    print(f"{seam}: restored baseline ...", end=" ", flush=True)
    rest_rc, rest_out, rest_timeout, rest_err = _run_tests(
        baseline_tests, timeout_seconds=timeout_seconds
    )
    if rest_timeout or rest_err is not None or rest_rc != 0 or restoration_failed:
        print("RED — restore verification failed")
        print(_tail(rest_out))
        return 3
    print("GREEN")
    print(f"{seam}: {killed}/{len(mutations)} mutants killed")
    if non_killed:
        print("non_killed: " + ", ".join(non_killed))
    return 0 if killed == len(mutations) else 1
