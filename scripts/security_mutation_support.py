"""Common fail-closed runner for the task-196 security mutation gates.

Each seam script applies controlled source weakenings, verifies that the
corresponding behavioral/architecture assertions go red, restores every file
byte-for-byte, and reruns its baseline tests green. The runner preserves any
pre-existing local edits because restoration uses the source text captured at
invocation time rather than a Git checkout.
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import re
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
    mutation_id: str = ""


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


# The API-gate campaign uses a stricter, report-producing runner than the
# legacy print-only campaigns above.  In particular, a non-zero pytest exit is
# not itself a kill: the targeted test node and its expected assertion oracle
# must both be present, and any unrelated failure makes the result invalid.
_PYTEST_SUMMARY_NODE = re.compile(r"^(FAILED|ERROR)\s+(\S+?)(?:\s+-\s+.*)?$")
_PYTEST_PASSED = re.compile(r"\b(\d+)\s+passed\b")


def _summary_nodes(output: str, kind: str) -> list[str]:
    nodes: set[str] = set()
    for line in output.splitlines():
        match = _PYTEST_SUMMARY_NODE.match(line.strip())
        if match and match.group(1) == kind:
            nodes.add(match.group(2))
    return sorted(nodes)


def _infrastructure_failure_kind(output: str, error_nodes: list[str]) -> str | None:
    """Name common non-security failures so they can never be reported as kills."""
    lowered = output.lower()
    if "modulenotfounderror" in lowered or "importerror" in lowered:
        return "import_error"
    if any(
        marker in lowered
        for marker in (
            "operationalerror",
            "databaseerror",
            "database is locked",
            "sqlite3.error",
            "psycopg.errors",
        )
    ):
        return "database_error"
    if "fixture" in lowered and (error_nodes or "setup error" in lowered):
        return "fixture_error"
    if error_nodes:
        return "pytest_error"
    return None


def _node_matches(expected: str, observed: str) -> bool:
    """Match a pinned pytest node id, including an optional parameter suffix."""
    return observed == expected or observed.startswith(expected + "[")


def _passed_count(output: str) -> int:
    matches = _PYTEST_PASSED.findall(output)
    return int(matches[-1]) if matches else 0


def _baseline_record(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    output = result.stdout + result.stderr
    failed = _summary_nodes(output, "FAILED")
    errors = _summary_nodes(output, "ERROR")
    infrastructure_kind = _infrastructure_failure_kind(output, errors)
    passed = _passed_count(output)
    if result.returncode == 0 and passed > 0 and not errors:
        status = "passed"
        failure_kind = None
    elif infrastructure_kind:
        status = f"invalid_{infrastructure_kind}"
        failure_kind = infrastructure_kind
    elif failed:
        status = "failed"
        failure_kind = "baseline_test_failure"
    elif result.returncode == 0:
        status = "invalid_no_tests"
        failure_kind = "no_tests_collected"
    else:
        status = "invalid_pytest_exit"
        failure_kind = "pytest_execution_error"
    return {
        "status": status,
        "returncode": result.returncode,
        "passed_tests": passed,
        "failed_tests": failed,
        "errors": errors,
        "failure_kind": failure_kind,
    }


def _run_audited_tests(
    tests: tuple[str, ...], timeout_seconds: int
) -> subprocess.CompletedProcess[str]:
    """Run only repo-local tests, with caches disabled and a hard timeout."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            *tests,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )


def _prepare_exact_edits(
    mutation: Mutation,
) -> tuple[dict[Path, bytes], dict[Path, bytes], str | None]:
    """Validate every edit before writing anything; anchors must occur once."""
    if not mutation.edits:
        return {}, {}, "mutation has no source edits"

    root = ROOT.resolve()
    originals: dict[Path, bytes] = {}
    updated: dict[Path, bytes] = {}
    for edit in mutation.edits:
        relative = Path(edit.path)
        if relative.is_absolute():
            return originals, updated, f"absolute source path is not allowed: {edit.path}"
        try:
            target = (ROOT / relative).resolve()
        except (OSError, RuntimeError) as exc:
            return originals, updated, f"cannot resolve {edit.path}: {type(exc).__name__}"
        if not target.is_relative_to(root):
            return originals, updated, f"source path escapes repository root: {edit.path}"
        if edit.old == edit.new:
            return originals, updated, f"no-op edit rejected: {edit.path}"
        try:
            if target not in originals:
                originals[target] = target.read_bytes()
                updated[target] = originals[target]
        except OSError as exc:
            return originals, updated, f"cannot read {edit.path}: {type(exc).__name__}"

        old = edit.old.encode("utf-8")
        new = edit.new.encode("utf-8")
        occurrences = updated[target].count(old)
        if occurrences != 1:
            return (
                originals,
                updated,
                f"anchor in {edit.path} occurs {occurrences} times (exactly one required)",
            )
        updated[target] = updated[target].replace(old, new, 1)
    return originals, updated, None


def _restore_exact_bytes(originals: dict[Path, bytes]) -> tuple[bool, list[str]]:
    """Restore and verify the exact original bytes for every touched file."""
    errors: list[str] = []
    for path, content in originals.items():
        try:
            path.write_bytes(content)
        except OSError as exc:
            errors.append(f"{path.relative_to(ROOT)}: write failed ({type(exc).__name__})")
    for path, content in originals.items():
        try:
            if path.read_bytes() != content:
                errors.append(f"{path.relative_to(ROOT)}: byte comparison failed")
        except OSError as exc:
            errors.append(f"{path.relative_to(ROOT)}: verification failed ({type(exc).__name__})")
    return not errors, sorted(set(errors))


def _apply_exact_mutation(
    mutation: Mutation,
) -> tuple[dict[Path, bytes] | None, str | None, bool]:
    """Apply a mutation only after exact-one anchor validation.

    Returns ``(original_bytes, error, restored)``.  On an invalid anchor no
    files are written.  A partial write is rolled back immediately and the
    caller is told whether byte restoration was proven.
    """
    originals, updated, error = _prepare_exact_edits(mutation)
    if error:
        return None, error, True
    for path, original in originals.items():
        try:
            if path.read_bytes() != original:
                return None, f"source changed after snapshot: {path.relative_to(ROOT)}", False
        except OSError as exc:
            return None, f"source recheck failed ({type(exc).__name__})", False
    try:
        for path, content in updated.items():
            path.write_bytes(content)
    except OSError as exc:
        restored, _ = _restore_exact_bytes(originals)
        _invalidate_bytecode(set(originals))
        return None, f"source mutation write failed ({type(exc).__name__})", restored
    _invalidate_bytecode(set(updated))
    return originals, None, True


def _set_mutation_status(
    row: dict[str, object],
    status: str,
    *,
    test_executed: bool,
    reason: str,
    failure_kind: str | None = None,
) -> None:
    row["status"] = status
    row["test_executed"] = test_executed
    row["detected"] = status == "detected"
    row["survived"] = status == "survived"
    row["invalid"] = status == "invalid"
    row["not_run"] = not test_executed
    row["reason"] = reason
    row["failure_kind"] = failure_kind


def _new_mutation_row(mutation_id: str, mutation: Mutation | None) -> dict[str, object]:
    if mutation is None:
        return {
            "id": mutation_id,
            "name": None,
            "status": "not_run",
            "test_executed": False,
            "detected": False,
            "survived": False,
            "invalid": False,
            "not_run": True,
            "returncode": None,
            "source_paths": [],
            "tests": [],
            "expected_failure_oracles": [],
            "observed_failed_tests": [],
            "observed_errors": [],
            "observed_oracles": [],
            "source_bytes_restored": None,
            "reason": "not started",
            "failure_kind": None,
            "infrastructure_failure_kind": None,
        }
    return {
        "id": mutation_id,
        "name": mutation.name,
        "status": "not_run",
        "test_executed": False,
        "detected": False,
        "survived": False,
        "invalid": False,
        "not_run": True,
        "returncode": None,
        "source_paths": sorted({edit.path for edit in mutation.edits}),
        "tests": list(mutation.tests),
        "expected_failure_oracles": list(mutation.expected_failures),
        "observed_failed_tests": [],
        "observed_errors": [],
        "observed_oracles": [],
        "source_bytes_restored": None,
        "reason": "not started",
        "failure_kind": None,
        "infrastructure_failure_kind": None,
        "rationale": mutation.rationale,
    }


def _worktree_clean() -> tuple[bool, list[str]]:
    """Refuse commit-bound success from a modified checkout.

    A generated, untracked ``ci-artifacts/`` report is the only local
    exception.  Tracked edits, staged changes, and other untracked files mean
    the campaign would be testing bytes not represented by ``HEAD``.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, [f"git status unavailable ({type(exc).__name__})"]
    if result.returncode != 0:
        return False, ["git status returned non-zero"]
    problems = []
    for line in result.stdout.splitlines():
        status, path = line[:2], line[3:]
        if status == "??" and (path == "ci-artifacts" or path.startswith("ci-artifacts/")):
            continue
        problems.append(line)
    return not problems, problems


def _commit_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    candidate = result.stdout.strip()
    return (
        candidate
        if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", candidate)
        else None
    )


def _write_report(report_path: Path, report: dict[str, object]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_name(report_path.name + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(report_path)


def run_reported_mutation_suite(
    seam: str,
    baseline_tests: tuple[str, ...],
    mutations: tuple[Mutation, ...],
    expected_ids: tuple[str, ...],
    report_path: str | Path,
    *,
    timeout_seconds: int = 120,
) -> int:
    """Run a strict, commit-bound campaign and emit an auditable JSON report.

    ``detected`` requires a non-zero pytest result, every targeted test node
    listed as failed, no unexpected failed/error nodes, and every declared
    failure-oracle marker.  A passing mutant is ``survived``; anchor drift,
    timeout, collection/import failure, unexpected assertions, report errors,
    or restoration uncertainty can never be counted as a kill.
    """
    destination = Path(report_path)
    if not destination.is_absolute():
        destination = ROOT / destination
    destination = destination.resolve(strict=False)
    if destination.is_relative_to((ROOT / "src").resolve()):
        print(f"{seam}: refusing to write evidence under product source: {destination}")
        return 4

    expected_unique = tuple(dict.fromkeys(expected_ids))
    definitions: dict[str, list[Mutation]] = {}
    unexpected_definitions: list[dict[str, str]] = []
    for mutation in mutations:
        if mutation.mutation_id in expected_unique:
            definitions.setdefault(mutation.mutation_id, []).append(mutation)
        else:
            unexpected_definitions.append({"id": mutation.mutation_id, "name": mutation.name})

    missing_ids = [item for item in expected_unique if item not in definitions]
    duplicate_ids = sorted(item for item, entries in definitions.items() if len(entries) != 1)
    catalog_valid = (
        bool(expected_ids)
        and len(expected_unique) == len(expected_ids)
        and not missing_ids
        and not duplicate_ids
        and not unexpected_definitions
    )
    worktree_clean, worktree_problems = _worktree_clean()
    report: dict[str, object] = {
        "schema_version": 1,
        "campaign": seam,
        "provenance": {
            "git_commit": _commit_sha(),
            "github_sha": os.environ.get("GITHUB_SHA") or None,
            "pr_head_sha": os.environ.get("NEXUS_EXPECTED_PR_HEAD_SHA") or None,
            "python_version": platform.python_version(),
            "worktree_clean": worktree_clean,
            "worktree_problems": worktree_problems,
        },
        "catalog": {
            "expected_ids": list(expected_ids),
            "defined_ids": [mutation.mutation_id for mutation in mutations],
            "missing_ids": missing_ids,
            "duplicate_ids": duplicate_ids,
            "unexpected_definitions": sorted(
                unexpected_definitions, key=lambda item: (item["id"], item["name"])
            ),
            "valid": catalog_valid,
        },
        "baseline": {
            "status": "not_run",
            "returncode": None,
            "passed_tests": 0,
            "failure_kind": None,
        },
        "restored_baseline": {
            "status": "not_run",
            "returncode": None,
            "passed_tests": 0,
            "failure_kind": None,
        },
        "source_bytes_restored": True,
        "mutations": [],
        "summary": {},
        "accepted": False,
    }
    provenance = report["provenance"]
    assert isinstance(provenance, dict)
    sha = provenance.get("git_commit")
    github_sha = provenance.get("github_sha")
    expected_pr_head_sha = os.environ.get("NEXUS_EXPECTED_PR_HEAD_SHA") or None
    pr_head_binding_valid = (
        not expected_pr_head_sha or provenance.get("pr_head_sha") == expected_pr_head_sha
    )
    provenance["pr_head_binding_valid"] = pr_head_binding_valid
    commit_binding_valid = (
        bool(sha)
        and (not github_sha or github_sha == sha)
        and provenance.get("worktree_clean") is True
        and pr_head_binding_valid
    )
    provenance["commit_binding_valid"] = commit_binding_valid

    rows: list[dict[str, object]] = []
    mutation_by_id: dict[str, Mutation] = {}
    for mutation_id in expected_unique:
        matches = definitions.get(mutation_id, [])
        mutation = matches[0] if len(matches) == 1 else None
        row = _new_mutation_row(mutation_id, mutation)
        if len(matches) == 0:
            _set_mutation_status(
                row,
                "invalid",
                test_executed=False,
                reason="expected mutation definition is missing",
                failure_kind="missing_definition",
            )
        elif len(matches) > 1:
            _set_mutation_status(
                row,
                "invalid",
                test_executed=False,
                reason=f"expected exactly one definition; found {len(matches)}",
                failure_kind="ambiguous_definition",
            )
        elif not mutation.tests or not mutation.expected_failures:
            _set_mutation_status(
                row,
                "invalid",
                test_executed=False,
                reason="mutation is missing test nodes or a failure oracle",
                failure_kind="missing_test_oracle",
            )
        else:
            mutation_by_id[mutation_id] = mutation
        rows.append(row)

    report["mutations"] = rows
    baseline_ok = False
    if not commit_binding_valid:
        report["baseline"] = {
            "status": "invalid_commit_binding",
            "returncode": None,
            "passed_tests": 0,
            "failure_kind": "invalid_provenance",
        }
        for row in rows:
            if row["status"] == "not_run":
                row["reason"] = (
                    "commit/worktree binding invalid (missing SHA, GITHUB_SHA/PR-head mismatch, "
                    "or non-artifact worktree changes)"
                )
                row["failure_kind"] = "invalid_provenance"
    else:
        print(f"{seam}: baseline ...", end=" ", flush=True)
        try:
            baseline = _run_audited_tests(baseline_tests, timeout_seconds)
            baseline_record = _baseline_record(baseline)
            baseline_ok = baseline_record["status"] == "passed"
            report["baseline"] = baseline_record
            print("GREEN" if baseline_ok else "RED — refusing to count mutations")
        except subprocess.TimeoutExpired:
            report["baseline"] = {
                "status": "invalid_timeout",
                "returncode": None,
                "passed_tests": 0,
                "failure_kind": "timeout",
            }
        except Exception as exc:
            report["baseline"] = {
                "status": "invalid_execution",
                "returncode": None,
                "passed_tests": 0,
                "error_type": type(exc).__name__,
                "failure_kind": "execution_error",
            }
        if not baseline_ok:
            baseline_status = report["baseline"]["status"]  # type: ignore[index]
            for row in rows:
                if row["status"] == "not_run":
                    row["reason"] = f"baseline did not pass: {baseline_status}"
                    row["failure_kind"] = "baseline_not_passed"

    if baseline_ok:
        stop_reason = ""
        for row in rows:
            if row["status"] == "invalid":
                continue
            mutation_id = str(row["id"])
            mutation = mutation_by_id[mutation_id]
            originals, apply_error, restored = _apply_exact_mutation(mutation)
            if apply_error or originals is None:
                row["source_bytes_restored"] = restored
                report["source_bytes_restored"] = bool(report["source_bytes_restored"]) and restored
                _set_mutation_status(
                    row,
                    "invalid",
                    test_executed=False,
                    reason=apply_error or "mutation application produced no source snapshot",
                    failure_kind="source_restoration_error"
                    if not restored
                    else "invalid_source_edit",
                )
                if not restored:
                    stop_reason = "source restoration failed during mutation application"
                    break
                continue

            result: subprocess.CompletedProcess[str] | None = None
            test_executed = False
            execution_error = ""
            execution_failure_kind: str | None = None
            try:
                result = _run_audited_tests(mutation.tests, timeout_seconds)
                test_executed = True
            except subprocess.TimeoutExpired:
                test_executed = True
                execution_error = "pytest timed out"
                execution_failure_kind = "timeout"
            except Exception as exc:
                execution_error = f"pytest could not execute ({type(exc).__name__})"
                execution_failure_kind = "execution_error"
            finally:
                restored_ok, restore_errors = _restore_exact_bytes(originals)
                _invalidate_bytecode(set(originals))

            row["source_bytes_restored"] = restored_ok
            report["source_bytes_restored"] = bool(report["source_bytes_restored"]) and restored_ok
            if not restored_ok:
                _set_mutation_status(
                    row,
                    "invalid",
                    test_executed=test_executed,
                    reason="source bytes failed restoration verification",
                    failure_kind="source_restoration_error",
                )
                row["restore_errors"] = restore_errors
                stop_reason = "source restoration failed; remaining mutations not run"
                break
            if execution_error:
                _set_mutation_status(
                    row,
                    "invalid",
                    test_executed=test_executed,
                    reason=execution_error,
                    failure_kind=execution_failure_kind,
                )
                if not test_executed:
                    stop_reason = "pytest process could not be started; remaining mutations not run"
                    break
                continue

            assert result is not None
            output = result.stdout + result.stderr
            failed_nodes = _summary_nodes(output, "FAILED")
            error_nodes = _summary_nodes(output, "ERROR")
            observed_oracles = [marker for marker in mutation.expected_failures if marker in output]
            infrastructure_kind = _infrastructure_failure_kind(output, error_nodes)
            expected_nodes_failed = all(
                any(_node_matches(expected, observed) for observed in failed_nodes)
                for expected in mutation.tests
            )
            no_unexpected_failures = all(
                any(_node_matches(expected, observed) for expected in mutation.tests)
                for observed in failed_nodes
            )
            oracle_complete = len(observed_oracles) == len(mutation.expected_failures)
            row["returncode"] = result.returncode
            row["infrastructure_failure_kind"] = infrastructure_kind
            row["observed_failed_tests"] = failed_nodes
            row["observed_errors"] = error_nodes
            row["observed_oracles"] = observed_oracles

            if result.returncode == 0:
                _set_mutation_status(
                    row,
                    "survived",
                    test_executed=True,
                    reason="targeted test passed while the mutation was applied",
                    failure_kind="surviving_mutant",
                )
            elif (
                expected_nodes_failed
                and no_unexpected_failures
                and not error_nodes
                and infrastructure_kind is None
                and oracle_complete
            ):
                _set_mutation_status(
                    row,
                    "detected",
                    test_executed=True,
                    reason="all targeted test nodes failed with their declared oracle",
                    failure_kind="expected_security_oracle",
                )
            else:
                failure_kind = infrastructure_kind or (
                    "unexpected_test_failure" if failed_nodes else "pytest_execution_error"
                )
                _set_mutation_status(
                    row,
                    "invalid",
                    test_executed=True,
                    reason=(f"pytest run was not a clean security detection: {failure_kind}"),
                    failure_kind=failure_kind,
                )
            print(f"{seam} {mutation_id} {mutation.name}: {row['status']}")

        if stop_reason:
            for row in rows:
                if row["status"] == "not_run":
                    row["reason"] = stop_reason
                    row["failure_kind"] = "prior_case_stopped_campaign"

        if bool(report["source_bytes_restored"]):
            print(f"{seam}: restored baseline ...", end=" ", flush=True)
            try:
                restored_baseline = _run_audited_tests(baseline_tests, timeout_seconds)
                restored_record = _baseline_record(restored_baseline)
                restored_ok = restored_record["status"] == "passed"
                report["restored_baseline"] = restored_record
                print("GREEN" if restored_ok else "RED — restored baseline failed")
            except subprocess.TimeoutExpired:
                report["restored_baseline"] = {
                    "status": "invalid_timeout",
                    "returncode": None,
                    "passed_tests": 0,
                    "failure_kind": "timeout",
                }
            except Exception as exc:
                report["restored_baseline"] = {
                    "status": "invalid_execution",
                    "returncode": None,
                    "passed_tests": 0,
                    "error_type": type(exc).__name__,
                    "failure_kind": "execution_error",
                }

    detected = sum(row["detected"] is True for row in rows)
    survived = sum(row["survived"] is True for row in rows)
    invalid = sum(row["invalid"] is True for row in rows)
    executed = sum(row["test_executed"] is True for row in rows)
    not_run = sum(row["not_run"] is True for row in rows)
    baseline_status = report["baseline"]["status"]  # type: ignore[index]
    restored_status = report["restored_baseline"]["status"]  # type: ignore[index]
    accepted = (
        catalog_valid
        and commit_binding_valid
        and baseline_status == "passed"
        and restored_status == "passed"
        and bool(report["source_bytes_restored"])
        and len(expected_ids) == 14
        and detected == 14
        and survived == 0
        and invalid == 0
        and not_run == 0
    )
    report["summary"] = {
        "planned": len(expected_ids),
        "executed": executed,
        "detected": detected,
        "survived": survived,
        "invalid": invalid,
        "not_run": not_run,
    }
    report["accepted"] = accepted
    print(
        f"{seam}: planned={len(expected_ids)} executed={executed} detected={detected} "
        f"survived={survived} invalid={invalid} not_run={not_run} accepted={accepted}"
    )

    try:
        _write_report(destination, report)
    except OSError as exc:
        print(f"{seam}: REPORT WRITE FAILED ({type(exc).__name__})", file=sys.stderr)
        return 4
    print(f"{seam}: report written to {destination}")

    if accepted:
        return 0
    if not commit_binding_valid or baseline_status != "passed":
        return 2
    if not bool(report["source_bytes_restored"]):
        return 3
    return 1
