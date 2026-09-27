"""Read, publish, and verify the committed Continuum project-state snapshot.

A snapshot is evidence only when its shape is known, its bytes are canonical,
it was published atomically, and each claimed relationship can be checked in
the current checkout.  This module therefore fails closed: unreadable state,
unavailable or shallow git history, unavailable test discovery, and
environment drift are all observable findings rather than a successful
verification with caveats.

Threat model (each row is pinned by a regression test and, where it is a code
decision, by a replayable mutation in :mod:`nexus_ai_agent.continuum.mutations`):

========================================  =====================================
attack                                    verdict
========================================  =====================================
stale snapshot / unreachable ``step``     ``state loss detected``
``step`` that is not a full commit id     unreadable (``HEAD``/branch names
                                          would always look fresh)
later committed source drift              ``source state drift detected``
dirty, untracked or ignored-importable    ``working tree drift detected``
source
shallow clone / missing history / no git  ``git verification unavailable``
test-count drift                          ``test count mismatch``
environment drift                         ``environment fingerprint mismatch``
malformed / non-canonical / duplicate-    ``snapshot unreadable``
key / extra-key / missing-key / wrong-
type document
partial publication                       prior file retained; a leftover temp
                                          file is working-tree drift
========================================  =====================================

The committed ``.nexus/continuum.json`` is machine-bound (its environment
fingerprint pins the interpreter micro version) and is refreshed only at a
release cut (DECISION_LOG D-0006/D-0023).  CI therefore does not gate on the
committed file's freshness; it gates on the *verifier* by publishing a fresh
snapshot for the exact SHA and replaying every attack above
(:mod:`nexus_ai_agent.continuum.gate`).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from nexus_ai_agent.continuum import provenance

SNAPSHOT_SCHEMA_VERSION = 2
_REPO_ROOT = provenance.REPO_ROOT
SNAPSHOT_PATH = _REPO_ROOT / ".nexus" / "continuum.json"
# Snapshot publication changes only .nexus/continuum.json.  A later change in
# any of these executable, test, migration, asset or dependency-declaration
# roots requires a fresh snapshot rather than borrowing the older commit's
# evidence.  Single source of truth: provenance.EVIDENCE_SOURCE_PATHS.
_SNAPSHOT_SOURCE_PATHS = provenance.EVIDENCE_SOURCE_PATHS
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
_COLLECTION_KEYS = frozenset({"exit_code", "count", "deselected"})
_COLLECTION_RUNNER = textwrap.dedent(
    """
    import json
    import os
    from pathlib import Path
    import sys

    import pytest


    class Collector:
        count = None
        deselected = 0

        def pytest_deselected(self, items):
            self.deselected += len(items)

        def pytest_collection_finish(self, session):
            self.count = len(session.items)


    output_path = Path(sys.argv[1])
    root = sys.argv[2]
    collector = Collector()
    exit_code = int(
        pytest.main(
            [
                "--collect-only",
                "-q",
                "-c",
                os.path.join(root, "pyproject.toml"),
                "--rootdir",
                root,
                "-p",
                "no:cacheprovider",
                "-p",
                "pytest_asyncio.plugin",
            ],
            plugins=[collector],
        )
    )
    output_path.write_text(
        json.dumps(
            {
                "exit_code": exit_code,
                "count": collector.count,
                "deselected": collector.deselected,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    """
)


@dataclass(frozen=True)
class EnvFingerprint:
    python: str
    alembic: str
    sqlalchemy: str


def _reject_non_finite(token: str) -> object:
    raise ValueError(f"snapshot contains a non-finite number ({token})")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"snapshot contains a duplicate key {key!r}")
        result[key] = value
    return result


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
        if not provenance.COMMIT_ID_PATTERN.fullmatch(values["step"]):
            # A symbolic revision ("HEAD", a branch, an abbreviation) re-resolves
            # at verification time, so it would vouch for whatever is checked out.
            raise ValueError("snapshot.step must be a full lowercase hexadecimal commit id")

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

        # JSON round-tripping rejects non-JSON direct callers (and NaN/Infinity)
        # and makes the snapshot independent of later mutation of the source.
        canonical_ledger = json.loads(
            json.dumps(ledger, ensure_ascii=False, sort_keys=True, allow_nan=False)
        )
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
        return (
            json.dumps(payload, ensure_ascii=False, indent=indent, sort_keys=True, allow_nan=False)
            + "\n"
        )


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


def current_commit() -> str:
    """Resolve HEAD without assuming a branch name (detached HEAD is valid)."""

    try:
        return provenance.current_commit(_REPO_ROOT)
    except RuntimeError as exc:
        raise RuntimeError(f"cannot resolve current git commit: {exc}") from exc


def parse_snapshot_bytes(raw: bytes) -> ContinuumSnapshot:
    """Parse *raw* snapshot bytes, accepting only the canonical serialisation.

    Canonical bytes make the committed file a single, reviewable fact: two
    different byte strings can never claim to be the same snapshot, duplicate
    keys cannot hide a second value, and NaN/Infinity cannot enter the ledger.
    """

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"snapshot is not UTF-8: {exc.reason}") from exc
    try:
        data = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"snapshot contains invalid JSON: {exc.msg}") from exc
    snapshot = ContinuumSnapshot.from_dict(data)
    if snapshot.to_json().encode("utf-8") != raw:
        raise ValueError(
            "snapshot bytes are not canonical (sorted keys, two-space indent, UTF-8, one "
            "trailing newline); publish it with `nexus continuum publish`"
        )
    return snapshot


def read_snapshot() -> ContinuumSnapshot:
    """Read a fully schema-valid, canonical snapshot from the contract path."""

    return parse_snapshot_bytes(SNAPSHOT_PATH.read_bytes())


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
    return provenance.atomic_write_bytes(SNAPSHOT_PATH, payload)


def _is_ancestor(ancestor: str, descendant: str) -> bool:
    """Return ancestry or raise when git cannot establish that relationship.

    A recorded commit that does not exist is unreachable (state loss) in a
    complete clone, but merely *unknown* in a shallow one.
    """

    try:
        if not provenance.commit_exists(_REPO_ROOT, ancestor):
            if provenance.is_shallow_repository(_REPO_ROOT):
                raise RuntimeError("cannot verify snapshot ancestry in a shallow repository")
            return False
        return provenance.is_ancestor(_REPO_ROOT, ancestor, descendant)
    except RuntimeError as exc:
        message = str(exc)
        if not message.startswith("cannot verify snapshot ancestry"):
            message = f"cannot verify snapshot ancestry: {message}"
        raise RuntimeError(message) from exc


def _working_tree_clean() -> bool:
    """Return whether the checkout still matches the committed snapshot state.

    A committed snapshot is part of a Git tree.  Verifying only ``HEAD`` lets
    uncommitted source changes borrow a previous commit's green result, so
    every tracked and untracked change anywhere in the checkout — plus every
    ignored-but-importable file under the evidence source roots — is drift.
    """

    return not provenance.working_tree_drift(_REPO_ROOT)


def _source_tree_matches_snapshot(step: str, head: str) -> bool:
    """Check that executable/test/dependency roots did not drift after ``step``.

    ``step`` records the commit from which the snapshot's claims were made;
    publishing that snapshot creates a later commit containing only the
    snapshot.  Requiring byte-identical ``step == HEAD`` would therefore reject
    every honestly committed snapshot.  Instead, diff the roots that can alter
    runtime behaviour, collection, coverage targets, pack membership,
    migrations, or dependency resolution.  A clean working tree alone cannot
    detect this committed stale-evidence case.
    """

    try:
        return provenance.paths_match(_REPO_ROOT, step, head, _SNAPSHOT_SOURCE_PATHS)
    except RuntimeError as exc:
        raise RuntimeError(f"cannot verify snapshot source provenance: {exc}") from exc


def _environment() -> EnvFingerprint:
    return EnvFingerprint(
        python=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        alembic=version("alembic"),
        sqlalchemy=version("sqlalchemy"),
    )


def _test_case_count() -> int:
    """Count actual pytest collection items, including parameters and generated tests."""

    root = _REPO_ROOT
    with tempfile.TemporaryDirectory(prefix="nexus-continuum-collect-") as directory:
        output = Path(directory) / "collection.json"
        try:
            outcome = subprocess.run(
                [sys.executable, "-c", _COLLECTION_RUNNER, str(output), str(root)],
                cwd=root,
                env=provenance.isolated_python_environment(root),
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
    if not isinstance(payload, dict) or set(payload) != _COLLECTION_KEYS:
        raise RuntimeError("pytest collection produced a malformed evidence artifact")
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
    deselected = payload.get("deselected")
    if type(deselected) is not int or deselected != 0:
        raise RuntimeError(f"pytest collection deselected {deselected!r} item(s)")
    count = payload.get("count")
    if type(count) is not int or count < 0:
        raise RuntimeError("pytest collection produced an invalid test count")
    return count


def capture_snapshot(
    *, plan: str, next_step: str, ledger: list[dict[str, Any]]
) -> ContinuumSnapshot:
    """Build a snapshot from live checkout facts — never from caller-supplied facts.

    ``step``, ``test_count_expected`` and ``env_fingerprint`` are measured here;
    only the human narrative (plan/next/ledger) is supplied by the caller.  A
    dirty checkout is refused because its HEAD cannot vouch for the measured
    test count.
    """

    if not _working_tree_clean():
        raise RuntimeError(
            "refusing to capture a snapshot from a dirty checkout: commit or remove every "
            "tracked, untracked and ignored-importable change first"
        )
    return ContinuumSnapshot.from_dict(
        {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "plan": plan,
            "step": current_commit(),
            "next": next_step,
            "ledger": ledger,
            "test_count_expected": _test_case_count(),
            "env_fingerprint": asdict(_environment()),
        }
    )


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


__all__ = [
    "SNAPSHOT_PATH",
    "SNAPSHOT_SCHEMA_VERSION",
    "ContinuumSnapshot",
    "EnvFingerprint",
    "capture_snapshot",
    "current_commit",
    "parse_snapshot_bytes",
    "read_snapshot",
    "verify_snapshot",
    "write_snapshot",
]
