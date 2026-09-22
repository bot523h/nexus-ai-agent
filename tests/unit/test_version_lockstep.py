"""Release version lock-step guard (board task-111).

The v3.12.0 release drifted: ``README`` described v3.13.0 behaviour while
``VERSION`` and ``pyproject.toml`` still said 3.12.0, and the hygiene pass had to
repair it. This guard makes that class of drift a red test instead of a release
incident.

Sources of truth checked against each other (all in-repo, no network):

* ``VERSION`` — the file the runtime reads in an uninstalled checkout;
* ``pyproject.toml`` — ``[project].version``, what a wheel would be stamped with;
* ``CHANGELOG.md`` — the newest **released** heading (``## [X.Y.Z] — date``);
  ``## [Unreleased]`` is not a release and never satisfies the check;
* the installed distribution metadata, when the package is actually installed
  (skipped — not failed — for a bare ``PYTHONPATH`` checkout).

The mismatch path is proven, not assumed: :func:`lockstep_violations` is a pure
function and ``test_the_guard_is_red_on_a_mismatched_fixture`` feeds it a
deliberately inconsistent fixture, so a guard that silently stopped comparing
anything would fail this suite.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path

import pytest
import tomllib

REPO_ROOT = Path(__file__).parents[2]
VERSION_FILE = REPO_ROOT / "VERSION"
PYPROJECT = REPO_ROOT / "pyproject.toml"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

#: ``## [3.13.0] — 2026-09-21`` / ``## [3.13.0] - 2026-09-21`` (em dash or hyphen).
_RELEASED_HEADING = re.compile(r"^##\s*\[(\d+\.\d+\.\d+)\]", re.MULTILINE)
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def read_repository_version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def read_pyproject_version() -> str:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def read_changelog_version(text: str | None = None) -> str:
    """The newest released heading in the changelog (``Unreleased`` excluded)."""
    body = CHANGELOG.read_text(encoding="utf-8") if text is None else text
    match = _RELEASED_HEADING.search(body)
    assert match is not None, "CHANGELOG.md has no released heading such as '## [1.2.3]'"
    return match.group(1)


def lockstep_violations(
    version_file: str,
    pyproject_version: str,
    changelog_version: str,
    installed_version: str | None = None,
) -> list[str]:
    """Every disagreement between the four version sources (empty list = in lock-step)."""
    problems: list[str] = []
    for label, value in (
        ("VERSION", version_file),
        ("pyproject.toml [project].version", pyproject_version),
        ("CHANGELOG.md newest released heading", changelog_version),
    ):
        if not _SEMVER.match(value):
            problems.append(f"{label} is not a semantic version: {value!r}")
    if problems:
        return problems

    canonical = version_file
    for label, value in (
        ("pyproject.toml [project].version", pyproject_version),
        ("CHANGELOG.md newest released heading", changelog_version),
    ):
        if value != canonical:
            problems.append(f"{label} ({value}) != VERSION ({canonical})")
    if installed_version is not None and installed_version != canonical:
        problems.append(f"installed distribution ({installed_version}) != VERSION ({canonical})")
    return problems


# --------------------------------------------------------------------------- #
# the guard, proven on a fixture
# --------------------------------------------------------------------------- #
def test_the_guard_is_red_on_a_mismatched_fixture() -> None:
    problems = lockstep_violations("3.13.0", "3.12.0", "3.11.0")
    assert len(problems) == 2
    assert any("pyproject" in problem for problem in problems)
    assert any("CHANGELOG" in problem for problem in problems)
    assert lockstep_violations("3.13.0", "3.13.0", "3.13.0") == []


def test_the_guard_rejects_a_non_semver_fixture() -> None:
    problems = lockstep_violations("v3.13.0", "3.13.0", "3.13.0")
    assert problems and "not a semantic version" in problems[0]


# --------------------------------------------------------------------------- #
# the repository itself
# --------------------------------------------------------------------------- #
def test_repository_versions_are_in_lock_step() -> None:
    problems = lockstep_violations(
        read_repository_version(), read_pyproject_version(), read_changelog_version()
    )
    assert not problems, "version drift: " + "; ".join(problems)


def test_installed_distribution_matches_the_repository_version() -> None:
    try:
        installed = distribution_version("nexus-ai-agent")
    except PackageNotFoundError:
        pytest.skip("package not installed (bare PYTHONPATH checkout) — CI installs it")
    assert (
        lockstep_violations(
            read_repository_version(), read_pyproject_version(), read_changelog_version(), installed
        )
        == []
    )


def test_changelog_has_an_unreleased_section() -> None:
    """A release cut must leave an ``Unreleased`` section behind for the next work."""
    assert "## [Unreleased]" in CHANGELOG.read_text(encoding="utf-8")
