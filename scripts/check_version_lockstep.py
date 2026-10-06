#!/usr/bin/env python3
"""Dependency-free release-version lock-step check (task-111 residue R1)."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

VERSION_FILENAME = "VERSION"
PYPROJECT_FILENAME = "pyproject.toml"
CHANGELOG_FILENAME = "CHANGELOG.md"
README_FILENAME = "README.md"

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_RELEASED_HEADING = re.compile(r"^##[ \t]*\[(\d+\.\d+\.\d+)\]", re.MULTILINE)
_README_VERSION = re.compile(r"^[> \t]*\*\*Version:[ \t]*v?(\d+\.\d+\.\d+)\*\*", re.MULTILINE)
_PROJECT_TABLE = re.compile(r"^\[project\][ \t]*$", re.MULTILINE)
_ANY_TABLE = re.compile(r"^\[[^\[\]]+\][ \t]*$", re.MULTILINE)
_PROJECT_VERSION = re.compile(r'^version[ \t]*=[ \t]*"([^"]+)"[ \t]*$', re.MULTILINE)


class MissingDeclaration(RuntimeError):
    pass


def read_version_file(root: Path) -> str:
    path = root / VERSION_FILENAME
    if not path.is_file():
        raise MissingDeclaration(f"{VERSION_FILENAME} is missing")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise MissingDeclaration(f"{VERSION_FILENAME} is empty")
    return text


def read_pyproject_version(root: Path) -> str:
    path = root / PYPROJECT_FILENAME
    if not path.is_file():
        raise MissingDeclaration(f"{PYPROJECT_FILENAME} is missing")
    text = path.read_text(encoding="utf-8")
    project = _PROJECT_TABLE.search(text)
    if project is None:
        raise MissingDeclaration(f"{PYPROJECT_FILENAME} has no [project] table")
    start = project.end()
    nxt = _ANY_TABLE.search(text, start)
    region = text[start : nxt.start() if nxt else None]
    match = _PROJECT_VERSION.search(region)
    if match is None:
        raise MissingDeclaration(f"{PYPROJECT_FILENAME} [project] has no version")
    return match.group(1)


def read_changelog_version(root: Path) -> str:
    path = root / CHANGELOG_FILENAME
    if not path.is_file():
        raise MissingDeclaration(f"{CHANGELOG_FILENAME} is missing")
    match = _RELEASED_HEADING.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise MissingDeclaration(f"{CHANGELOG_FILENAME} has no released heading")
    return match.group(1)


def read_readme_version(root: Path) -> str:
    path = root / README_FILENAME
    if not path.is_file():
        raise MissingDeclaration(f"{README_FILENAME} is missing")
    match = _README_VERSION.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise MissingDeclaration(f"{README_FILENAME} has no canonical Version label")
    return match.group(1)


def lockstep_violations(
    version_file: str,
    pyproject_version: str,
    changelog_version: str,
    readme_version: str | None = None,
) -> list[str]:
    problems: list[str] = []
    declarations = [
        (VERSION_FILENAME, version_file),
        (f"{PYPROJECT_FILENAME} [project].version", pyproject_version),
        (f"{CHANGELOG_FILENAME} newest released heading", changelog_version),
    ]
    if readme_version is not None:
        declarations.append((f"{README_FILENAME} Version label", readme_version))
    for label, value in declarations:
        if not _SEMVER.match(value):
            problems.append(f"{label} is not a semantic version: {value!r}")
    if problems:
        return problems
    canonical = version_file
    comparisons = [
        (f"{PYPROJECT_FILENAME} [project].version", pyproject_version),
        (f"{CHANGELOG_FILENAME} newest released heading", changelog_version),
    ]
    if readme_version is not None:
        comparisons.append((f"{README_FILENAME} Version label", readme_version))
    for label, value in comparisons:
        if value != canonical:
            problems.append(f"{label} ({value}) != {VERSION_FILENAME} ({canonical})")
    return problems


def toml_cross_check(pyproject_text: str, pyproject_version: str) -> list[str]:
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        version_file = read_version_file(root)
        pyproject_version = read_pyproject_version(root)
        changelog_version = read_changelog_version(root)
        readme_version = read_readme_version(root)
        pyproject_text = (root / PYPROJECT_FILENAME).read_text(encoding="utf-8")
    except MissingDeclaration as exc:
        print(f"cannot check the release version: {exc}", file=sys.stderr)
        return 2
    problems = lockstep_violations(
        version_file, pyproject_version, changelog_version, readme_version
    )
    problems += toml_cross_check(pyproject_text, pyproject_version)
    if problems:
        print("release version drift detected:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "  fix: align VERSION, pyproject.toml [project].version, README Version "
            "label and the newest released CHANGELOG heading",
            file=sys.stderr,
        )
        return 1
    print(
        f"release versions in lock-step: {version_file} "
        f"(VERSION, pyproject, CHANGELOG, README)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
