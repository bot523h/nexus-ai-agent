"""Read, write, and verify the committed Continuum project-state snapshot.

A snapshot is evidence only when its shape is known, its bytes were published
atomically, and each claimed relationship can be checked in the current
checkout.  This module therefore fails closed: unreadable state, unavailable
git history, unavailable test discovery, and environment drift are all
observable findings rather than a successful verification with caveats.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

SNAPSHOT_SCHEMA_VERSION = 2
_REPO_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT_PATH = _REPO_ROOT / ".nexus" / "continuum.json"
# Snapshot publication changes only .nexus/continuum.json.  A later change in
# any of these executable, test, or dependency-declaration roots requires a
# fresh snapshot rather than borrowing the older commit's evidence.
_SNAPSHOT_SOURCE_PATHS = (
    "src",
    "tests",
    "scripts",
    "alembic",
    "pyproject.toml",
    "alembic.ini",
    "VERSION",
)
_SNAPSHOT_KEYS = frozenset(
    {
        "schema_version",
        "plan",
        "step",
        "next",
        "ledger",
        "test_count_expected",
        "env_fingerprint",
    }
)
_ENVIRONMENT_KEYS = frozenset({"python", "alembic", "sqlalchemy"})
_COLLECTION_RUNNER = textwrap.dedent(
    """
    import json
    from pathlib import Path
    import sys

    import pytest


    class Collector:
        count = None

        def pytest_collection_finish(self, session):
            self.count = len(session.items)


    collector = Collector()
    exit_code = int(
        pytest.main(
            [
                "--collect-only",
                "-q",
                "-p",
                "no:cacheprovider",
                "-p",
                "pytest_asyncio.plugin",
            ],
            plugins=[collector],
        )
    )
    Path(sys.argv[1]).write_text(
        json.dumps({"exit_code": exit_code, "count": collector.count}, sort_keys=True),
        encoding="utf-8",
    )
    """
)


@dataclass(frozen=True)
class EnvFingerprint:
    python: str
    alembic: str
    sqlalchemy: str


@dataclass(frozen=True)
class ContinuumSnapshot:
    schema_version: int
    plan: str
    step: str
    next: str
    ledger: list[dict[str, Any]]
    test_count_expected: int
    env_fingerprint: EnvFingerprint

    @classmethod
    def from_dict(cls, data: object) -> ContinuumSnapshot:
        """Validate the exact schema before building an immutable snapshot value."""

        if not isinstance(data, dict):
            raise ValueError("snapshot root must be a JSON object")
        _require_exact_keys(data, _SNAPSHOT_KEYS, "snapshot")
        schema_version = data["schema_version"]
        if type(schema_version) is not int:
            raise ValueError("snapshot.schema_version must be an integer")
        if schema_version != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(f"unsupported snapshot schema_version {schema_version!r}")

        values: dict[str, str] = {}
        for key in ("plan", "step", "next"):
            value = data[key]
            if not isinstance(value, str):
                raise ValueError(f"snapshot.{key} must be a string")
            values[key] = value
        if not values["step"].strip():
            raise ValueError("snapshot.step must be a non-empty Git revision")

        ledger = data["ledger"]
        if not isinstance(ledger, list) or any(not isinstance(entry, dict) for entry in ledger):
            raise ValueError("snapshot.ledger must be a list of JSON objects")
        expected_count = data["test_count_expected"]
        if type(expected_count) is not int or expected_count < 0:
            raise ValueError("snapshot.test_count_expected must be a non-negative integer")

        environment = data["env_fingerprint"]
        if not isinstance(environment, dict):
            raise ValueError("snapshot.env_fingerprint must be a JSON object")
        _require_exact_keys(environment, _ENVIRONMENT_KEYS, "snapshot.env_fingerprint")
        for key in sorted(_ENVIRONMENT_KEYS):
            value = environment[key]
            if not isinstance(value, str) or not value:
                raise ValueError(f"snapshot.env_fingerprint.{key} must be a non-empty string")

        # JSON round-tripping rejects non-JSON direct callers and makes the
        # snapshot independent of later mutation of the source dictionary.
        canonical_ledger = json.loads(json.dumps(ledger, ensure_ascii=False, sort_keys=True))
        return cls(
            schema_version=schema_version,
            plan=values["plan"],
            step=values["step"],
            next=values["next"],
            ledger=canonical_ledger,
            test_count_expected=expected_count,
            env_fingerprint=EnvFingerprint(
                python=environment["python"],
                alembic=environment["alembic"],
                sqlalchemy=environment["sqlalchemy"],
            ),
        )

    def to_json(self, *, indent: int = 2) -> str:
        """Return canonical snapshot bytes (stable order, formatting, newline)."""

        payload = asdict(self)
        # Reuse the reader's schema validation so writers cannot knowingly emit
        # a snapshot that the reader will reject.
        type(self).from_dict(payload)
        return json.dumps(payload, ensure_ascii=False, indent=indent, sort_keys=True) + "\n"


def _require_exact_keys(data: dict[str, object], expected: frozenset[str], name: str) -> None:
    actual = frozenset(data)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing keys: {', '.join(missing)}")
        if extra:
            details.append(f"unexpected keys: {', '.join(extra)}")
        raise ValueError(f"{name} has invalid keys ({'; '.join(details)})")


def _git_output(arguments: list[str], description: str) -> str:
    try:
        outcome = subprocess.run(
            ["git", *arguments],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"{description}: cannot execute git ({exc})") from exc
    if outcome.returncode != 0:
        detail = (outcome.stderr or outcome.stdout).strip().replace("\n", " ")
        raise RuntimeError(f"{description}: git exited {outcome.returncode} ({detail})")
    return outcome.stdout.strip()


def current_commit() -> str:
    """Resolve HEAD without assuming a branch name (detached HEAD is valid)."""

    head = _git_output(["rev-parse", "HEAD"], "cannot resolve current git commit")
    if not head:
        raise RuntimeError("cannot resolve current git commit: git returned an empty commit")
    return head


def read_snapshot() -> ContinuumSnapshot:
    """Read a fully schema-valid snapshot from the canonical path."""

    try:
        data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"snapshot contains invalid JSON: {exc.msg}") from exc
    return ContinuumSnapshot.from_dict(data)


def _fsync_directory(directory: Path) -> None:
    """Persist a rename's directory entry where the platform supports it."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_snapshot(snapshot: ContinuumSnapshot) -> Path:
    """Atomically replace the snapshot after validating canonical JSON bytes.

    The temporary file is created in the destination directory, flushed and
    fsynced before :func:`os.replace`.  Therefore a process interruption before
    replacement retains the prior snapshot rather than leaving a partial JSON
    document at the contract path.
    """

    if not isinstance(snapshot, ContinuumSnapshot):
        raise TypeError("write_snapshot requires a ContinuumSnapshot")
    payload = snapshot.to_json().encode("utf-8")
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{SNAPSHOT_PATH.name}.", suffix=".tmp", dir=SNAPSHOT_PATH.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, SNAPSHOT_PATH)
        _fsync_directory(SNAPSHOT_PATH.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return SNAPSHOT_PATH


def _is_shallow_repository() -> bool:
    return (
        _git_output(["rev-parse", "--is-shallow-repository"], "cannot inspect git history")
        == "true"
    )


def _is_ancestor(ancestor: str, descendant: str) -> bool:
    """Return ancestry or raise when git cannot establish that relationship."""

    try:
        outcome = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"cannot verify snapshot ancestry: cannot execute git ({exc})") from exc
    if outcome.returncode == 0:
        return True
    if outcome.returncode == 1:
        if _is_shallow_repository():
            raise RuntimeError("cannot verify snapshot ancestry in a shallow repository")
        return False
    detail = (outcome.stderr or outcome.stdout).strip().replace("\n", " ")
    raise RuntimeError(
        f"cannot verify snapshot ancestry: git exited {outcome.returncode} ({detail})"
    )


def _working_tree_clean() -> bool:
    """Return whether the checkout still matches the committed snapshot state.

    A committed snapshot is part of a Git tree.  Verifying only ``HEAD`` lets
    uncommitted source changes borrow a previous commit's green result, so
    every tracked *and* untracked change is evidence drift.  The porcelain
    format is stable, machine-readable, and deliberately includes untracked
    files rather than silently accepting generated or newly added source.
    """

    return not _git_output(
        ["status", "--porcelain=v1", "--untracked-files=all"],
        "cannot inspect working tree",
    )


def _source_tree_matches_snapshot(step: str, head: str) -> bool:
    """Check that executable/test/dependency roots did not drift after ``step``.

    ``step`` records the commit from which the snapshot's claims were made;
    publishing that snapshot creates a later commit containing only the
    snapshot.  Requiring byte-identical ``step == HEAD`` would therefore reject
    every honestly committed snapshot.  Instead, diff the roots that can alter
    runtime behaviour, collection, coverage targets, pack membership, or
    dependency resolution.  A clean working tree alone cannot detect this
    committed stale-evidence case.
    """

    try:
        outcome = subprocess.run(
            ["git", "diff", "--quiet", step, head, "--", *_SNAPSHOT_SOURCE_PATHS],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(
            f"cannot verify snapshot source provenance: cannot execute git ({exc})"
        ) from exc
    if outcome.returncode == 0:
        return True
    if outcome.returncode == 1:
        return False
    detail = (outcome.stderr or outcome.stdout).strip().replace("\n", " ")
    raise RuntimeError(
        f"cannot verify snapshot source provenance: git exited {outcome.returncode} ({detail})"
    )


def _environment() -> EnvFingerprint:
    return EnvFingerprint(
        python=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        alembic=version("alembic"),
        sqlalchemy=version("sqlalchemy"),
    )


def _test_case_count() -> int:
    """Count actual pytest collection items, including parameters and generated tests."""

    environment = os.environ.copy()
    source = str(_REPO_ROOT / "src")
    old_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source if not old_pythonpath else source + os.pathsep + old_pythonpath
    )
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    with tempfile.TemporaryDirectory(prefix="nexus-continuum-collect-") as directory:
        output = Path(directory) / "collection.json"
        try:
            outcome = subprocess.run(
                [sys.executable, "-c", _COLLECTION_RUNNER, str(output)],
                cwd=_REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"cannot collect tests: {exc}") from exc
        if outcome.returncode != 0:
            detail = (outcome.stderr or outcome.stdout).strip().replace("\n", " ")
            raise RuntimeError(
                f"pytest collection subprocess exited {outcome.returncode}: {detail}"
            )
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(
                f"pytest collection produced no valid evidence artifact: {exc}"
            ) from exc
    if payload.get("exit_code") != 0:
        output_text = outcome.stderr or outcome.stdout
        missing_dependencies = sorted(set(re.findall(r"No module named '([^']+)'", output_text)))
        if missing_dependencies:
            detail = "missing import dependencies: " + ", ".join(missing_dependencies)
        else:
            detail = output_text.strip().replace("\n", " ")[-500:]
        raise RuntimeError(
            f"pytest collection failed with exit code {payload.get('exit_code')!r}: {detail}"
        )
    count = payload.get("count")
    if type(count) is not int or count < 0:
        raise RuntimeError("pytest collection produced an invalid test count")
    return count


def _problem(prefix: str, exc: BaseException) -> str:
    detail = str(exc).replace("\n", " ")
    return f"{prefix}: {detail or type(exc).__name__}"


def verify_snapshot() -> list[str]:
    """Return every observable reason the snapshot cannot be trusted.

    An empty list is the sole success value.  Every subsystem is checked
    independently after a readable snapshot so an operator receives useful
    diagnostics rather than a hidden exception or the first failure only.
    """

    try:
        snapshot = read_snapshot()
    except (FileNotFoundError, OSError, ValueError, TypeError) as exc:
        return [_problem("snapshot unreadable", exc)]

    problems: list[str] = []
    try:
        head = current_commit()
    except RuntimeError as exc:
        problems.append(_problem("git verification unavailable", exc))
    else:
        try:
            ancestor = _is_ancestor(snapshot.step, head)
        except RuntimeError as exc:
            problems.append(_problem("git verification unavailable", exc))
        else:
            if not ancestor:
                problems.append(
                    "state loss detected: recorded good commit "
                    f"{snapshot.step} is not reachable from HEAD {head}"
                )
            else:
                try:
                    source_matches = _source_tree_matches_snapshot(snapshot.step, head)
                except RuntimeError as exc:
                    problems.append(_problem("git verification unavailable", exc))
                else:
                    if not source_matches:
                        problems.append(
                            "source state drift detected: executable, test, or dependency roots "
                            "changed after the recorded good commit"
                        )
        try:
            clean = _working_tree_clean()
        except RuntimeError as exc:
            problems.append(_problem("git verification unavailable", exc))
        else:
            if not clean:
                problems.append(
                    "working tree drift detected: snapshot verification requires a clean checkout"
                )

    try:
        actual_tests = _test_case_count()
    except RuntimeError as exc:
        problems.append(_problem("test discovery unavailable", exc))
    else:
        if snapshot.test_count_expected != actual_tests:
            problems.append(
                "test count mismatch: "
                f"expected {snapshot.test_count_expected}, found {actual_tests}"
            )

    try:
        expected_environment = _environment()
    except (PackageNotFoundError, OSError, RuntimeError) as exc:
        problems.append(_problem("environment fingerprint unavailable", exc))
    else:
        if asdict(snapshot.env_fingerprint) != asdict(expected_environment):
            problems.append(
                f"environment fingerprint mismatch: expected {asdict(snapshot.env_fingerprint)}, "
                f"found {asdict(expected_environment)}"
            )
    return problems
