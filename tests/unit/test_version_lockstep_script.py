"""The fast-rail lock-step script is a CI entry point, so test it as one (black box).

``scripts/check_version_lockstep.py`` runs in the ``lint-fast`` job **before** any
dependency is installed (task-111 residue R1).  Two consequences drive this file:

* it cannot be imported from the package and it must stay executable with the bare
  interpreter, so the tests drive it through ``subprocess`` exactly like CI does —
  asserting the contract that actually matters (exit codes and the message);
* it must never grow a third-party import, because the rail that runs it installs
  nothing.  The AST check below fails the suite if that changes, which is the only
  place the regression could be caught before a red CI run in the fast rail itself.

``tests/architecture/test_scripts_import_boundary.py`` is respected throughout: the
unpackaged ``scripts`` namespace is never imported, only executed by path.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_version_lockstep.py"

_AGREEING_PYPROJECT = """[project]
name = "fixture"
version = "{version}"

[tool.ruff]
version = "9.9.9"
"""
_MISLEADING_NOTE = "# a version outside [project] must never satisfy the check\n"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


def _fixture(
    root: Path, *, version: str = "3.13.0", pyproject: str = "3.13.0", changelog: str = "3.13.0"
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        _MISLEADING_NOTE + _AGREEING_PYPROJECT.format(version=pyproject), encoding="utf-8"
    )
    (root / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [Unreleased]\n\n## [{changelog}] - 2026-01-01\n",
        encoding="utf-8",
    )
    return root


# --------------------------------------------------------------------------- #
# the real repository
# --------------------------------------------------------------------------- #
def test_the_script_passes_on_the_real_repository() -> None:
    result = _run()
    assert result.returncode == 0, result.stderr
    assert "version lock-step ok" in result.stdout


def test_quiet_is_silent_when_the_repository_is_in_lock_step() -> None:
    result = _run("--quiet")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


# --------------------------------------------------------------------------- #
# red on a mismatched fixture — the guard is proven, not assumed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("pyproject", "changelog", "expected_offender"),
    [
        ("3.12.0", "3.13.0", "pyproject.toml"),
        ("3.13.0", "3.11.0", "CHANGELOG.md"),
    ],
)
def test_the_script_is_red_when_one_source_drifts(
    tmp_path: Path, pyproject: str, changelog: str, expected_offender: str
) -> None:
    root = _fixture(tmp_path / "drifted", pyproject=pyproject, changelog=changelog)
    result = _run("--root", str(root))
    assert result.returncode == 1
    assert expected_offender in result.stderr
    assert "drift detected" in result.stderr


def test_a_version_outside_the_project_table_is_ignored(tmp_path: Path) -> None:
    """The fixture carries ``[tool.ruff] version = "9.9.9"``: it must not be read as the version."""
    root = _fixture(tmp_path / "scoped")
    result = _run("--root", str(root))
    assert result.returncode == 0, result.stderr


def test_a_non_semver_declaration_is_reported_as_such(tmp_path: Path) -> None:
    root = _fixture(tmp_path / "prefixed", version="v3.13.0")
    result = _run("--root", str(root))
    assert result.returncode == 1
    assert "not a semantic version" in result.stderr


def test_a_missing_declaration_exits_with_two(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _run("--root", str(empty))
    assert result.returncode == 2
    assert "cannot check the release version" in result.stderr


def test_a_changelog_without_a_released_heading_exits_with_two(tmp_path: Path) -> None:
    root = _fixture(tmp_path / "unreleased-only")
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n", encoding="utf-8")
    result = _run("--root", str(root))
    assert result.returncode == 2
    assert "released heading" in result.stderr


# --------------------------------------------------------------------------- #
# the rail's one hard constraint: nothing to install
# --------------------------------------------------------------------------- #
def test_the_script_imports_standard_library_only() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    # tomllib is standard from 3.11 and the parser degrades gracefully on 3.10 (see
    # toml_cross_check): allow it explicitly so the test is version-independent.
    # tomli is deliberately NOT allowed: it is not a dependency (not even
    # transitively), so any import of it would crash the rail at runtime.
    allowed = set(sys.stdlib_module_names) | {"tomllib"}
    third_party = sorted(name for name in imported if name not in allowed)
    assert not third_party, (
        f"{SCRIPT.name} must stay standard-library only — lint-fast installs nothing, "
        f"so these imports would break the rail at runtime: {third_party}"
    )
