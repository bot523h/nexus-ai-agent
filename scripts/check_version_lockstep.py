#!/usr/bin/env python3
"""Dependency-free release-version lock-step check (task-111 residue R1).

The invariant is::

    VERSION == pyproject.toml:[project].version == newest released CHANGELOG heading

Two guards already cover it *inside* the test suite
(``tests/unit/test_version_command.py`` from wave-4 step 1, and the
``tests/unit/test_version_lockstep.py`` guard).  Both of them, however, can only
run after ``pip install -e ".[dev]"`` has finished, so a version drift costs a
full dependency install before it is reported — and if the install itself is
what broke, the drift is never reported at all.

This script is the CI entry point of the *fast rail*: standard library only, no
import from the package, no third-party dependency, no network, ~50 ms runtime.
``lint-fast`` (see ``.github/workflows/ci.yml``) therefore runs it as its very
first step, before anything is installed.

Exit codes
----------
0
    every declaration agrees.
1
    drift detected, or a declaration is not a semantic version.  The message
    names every offender.
2
    a required declaration is missing or unreadable.

Usage
-----
    python scripts/check_version_lockstep.py [--root PATH] [--quiet]

Persian note: این اسکریپت عمداً هیچ وابستگی ندارد تا پیش از نصب پکیج اجرا شود.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

VERSION_FILENAME = "VERSION"
PYPROJECT_FILENAME = "pyproject.toml"
CHANGELOG_FILENAME = "CHANGELOG.md"

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
#: ``## [3.13.0] — 2026-09-21`` / ``## [3.13.0] - 2026-09-21``; ``[Unreleased]`` never matches.
_RELEASED_HEADING = re.compile(r"^##[ \t]*\[(\d+\.\d+\.\d+)\]", re.MULTILINE)
_PROJECT_TABLE = re.compile(r"^\[project\][ \t]*$", re.MULTILINE)
_ANY_TABLE = re.compile(r"^\[[^\[\]]+\][ \t]*$", re.MULTILINE)
_PROJECT_VERSION = re.compile(r'^version[ \t]*=[ \t]*"([^"]+)"[ \t]*$', re.MULTILINE)


class MissingDeclaration(FileNotFoundError):
    """A required release declaration is absent or unparsable."""


def _project_table_body(text: str) -> str:
    """Return only the ``[project]`` table, so a ``version =`` of another table can never win.

    ``tomllib`` is not available on Python 3.10, which this repository still supports
    (``requires-python = ">=3.10"``), hence the text scan.  :func:`toml_cross_check`
    re-parses with the real TOML parser wherever it exists, so a text scan that picked
    the wrong table is reported instead of being trusted.
    """
    start = _PROJECT_TABLE.search(text)
    if start is None:
        raise MissingDeclaration(f"{PYPROJECT_FILENAME} has no [project] table")
    rest = text[start.end() :]
    end = _ANY_TABLE.search(rest)
    return rest if end is None else rest[: end.start()]


def read_version_file(root: Path) -> str:
    path = root / VERSION_FILENAME
    if not path.is_file():
        raise MissingDeclaration(f"{VERSION_FILENAME} is missing")
    return path.read_text(encoding="utf-8").strip()


def read_pyproject_version(root: Path) -> str:
    path = root / PYPROJECT_FILENAME
    if not path.is_file():
        raise MissingDeclaration(f"{PYPROJECT_FILENAME} is missing")
    match = _PROJECT_VERSION.search(_project_table_body(path.read_text(encoding="utf-8")))
    if match is None:
        raise MissingDeclaration(f"{PYPROJECT_FILENAME} declares no version in [project]")
    return match.group(1).strip()


def read_changelog_version(root: Path) -> str:
    path = root / CHANGELOG_FILENAME
    if not path.is_file():
        raise MissingDeclaration(f"{CHANGELOG_FILENAME} is missing")
    match = _RELEASED_HEADING.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise MissingDeclaration(
            f"{CHANGELOG_FILENAME} has no released heading such as '## [1.2.3]'"
        )
    return match.group(1)


def lockstep_violations(
    version_file: str, pyproject_version: str, changelog_version: str
) -> list[str]:
    """Every disagreement between the three declarations (empty list = in lock-step)."""
    problems: list[str] = []
    for label, value in (
        (VERSION_FILENAME, version_file),
        (f"{PYPROJECT_FILENAME} [project].version", pyproject_version),
        (f"{CHANGELOG_FILENAME} newest released heading", changelog_version),
    ):
        if not _SEMVER.match(value):
            problems.append(f"{label} is not a semantic version: {value!r}")
    if problems:
        return problems

    canonical = version_file
    for label, value in (
        (f"{PYPROJECT_FILENAME} [project].version", pyproject_version),
        (f"{CHANGELOG_FILENAME} newest released heading", changelog_version),
    ):
        if value != canonical:
            problems.append(f"{label} ({value}) != {VERSION_FILENAME} ({canonical})")
    return problems


def toml_cross_check(pyproject_text: str, scanned_version: str) -> list[str]:
    """Re-parse with ``tomllib`` when available (Python 3.11+); 3.10 trusts the scan."""
    try:  # pragma: no cover - the 3.10 branch is exercised by the version matrix, not here
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10 only
        return []
    parsed = str(tomllib.loads(pyproject_text).get("project", {}).get("version", ""))
    if parsed != scanned_version:
        return [
            f"{PYPROJECT_FILENAME}: the TOML parser reads {parsed!r} where the text scan "
            f"reads {scanned_version!r}"
        ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail fast when the release version declarations drift apart."
    )
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parents[1]),
        help="repository root to check (defaults to the repository of this script)",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="print nothing when the declarations agree"
    )
    args = parser.parse_args(argv)
    root = Path(args.root)

    try:
        version_file = read_version_file(root)
        pyproject_version = read_pyproject_version(root)
        changelog_version = read_changelog_version(root)
        pyproject_text = (root / PYPROJECT_FILENAME).read_text(encoding="utf-8")
    except MissingDeclaration as exc:
        print(f"cannot check the release version: {exc}", file=sys.stderr)
        return 2

    problems = lockstep_violations(version_file, pyproject_version, changelog_version)
    problems += toml_cross_check(pyproject_text, pyproject_version)
    if problems:
        print("release version drift detected:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "  fix: align VERSION, pyproject.toml [project].version and the newest "
            "released CHANGELOG heading",
            file=sys.stderr,
        )
        return 1

    if not args.quiet:
        print(f"version lock-step ok: VERSION == pyproject == CHANGELOG == {version_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
