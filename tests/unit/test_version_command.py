"""The ``/version`` command must report the real running version.

Regression cover for the v3.0.0 hardcoded string that survived until
v3.10.0.  The distribution metadata is the primary source (the ``VERSION``
file at the repository root is not shipped in a wheel); the repository file
is a fallback for an uninstalled checkout.

Version-lockstep guard (wave-4 step1): ``VERSION`` == ``pyproject.toml``
version == latest ``CHANGELOG.md`` ``## [x.y.z]`` heading.  This prevents
the 3.12.0-vs-v3.13.0 drift that the hygiene pass fixed.  The check is
implemented as a unit test so ``pytest`` is the single CI gate — no new
dependencies, no network, deterministic on fixtures.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nexus_ai_agent.bot import update_handlers
from nexus_ai_agent.bot.update_handlers import running_version, version_cmd

REPO_ROOT = Path(__file__).parents[2]

# -- helpers for the lockstep check ---------------------------------------

try:  # Python 3.11+: the real TOML parser, stdlib, no new dependency.
    import tomllib
except ImportError:  # Python 3.10: tomllib does not exist there.
    # NOTE: no ``tomli`` fallback — tomli is not a dependency (not even
    # transitively), so importing it crashes a bare 3.10 interpreter at
    # collection.  The 3.10 path is a stdlib-only text scan, proven equal.
    tomllib = None  # type: ignore[assignment]

_CHANGELOG_HEADING = re.compile(r"^## \[(?P<ver>\d+\.\d+\.\d+)\]")

# Match any semver heading; we intentionally ignore date suffixes like
# " — 2026-09-21" and the "[Unreleased]" heading.


def _read_version_file(root: Path) -> str:
    return (root / "VERSION").read_text(encoding="utf-8").strip()


def _read_pyproject_version(root: Path) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    if tomllib is not None:
        return str(tomllib.loads(text)["project"]["version"]).strip()
    return _project_version_text_scan(text).strip()


def _project_version_text_scan(pyproject_text: str) -> str:
    """The 3.10 fallback: ``version`` from the ``[project]`` table body.

    Scoped to the table (the match must sit between ``[project]`` and the
    next table header) so similarly-named keys in other tables can never
    leak in.  Raises instead of guessing when the table or key is absent.
    """
    start = re.search(r"^\[project\][ \t]*$", pyproject_text, re.MULTILINE)
    if start is None:
        raise ValueError("pyproject.toml has no [project] table")
    rest = pyproject_text[start.end() :]
    next_table = re.search(r"^\[[^\[\]]+\][ \t]*$", rest, re.MULTILINE)
    body = rest if next_table is None else rest[: next_table.start()]
    match = re.search(r'^version\s*=\s*"([^"]+)"', body, re.MULTILINE)
    if match is None:
        raise ValueError("pyproject.toml [project] has no version key")
    return match.group(1)


def _read_changelog_latest(root: Path) -> str | None:
    """Return the version of the latest ``## [x.y.z]`` heading in CHANGELOG.md.

    ``## [Unreleased]`` is ignored — the first *released* heading wins.
    Returns ``None`` if no released heading exists (fresh repo).
    """
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    for line in text.splitlines():
        m = _CHANGELOG_HEADING.match(line.strip())
        if m:
            return m.group("ver")
    return None


def assert_versions_in_lockstep(root: Path) -> None:
    """Fail with a human-readable message if the three sources diverge.

    Raises ``AssertionError`` with a message that names the mismatched
    sources — the test harness asserts on the message, so a mismatch is
    demonstrably red (see ``test_lockstep_fails_on_mismatched_fixture``).
    """
    version_file = _read_version_file(root)
    pyproject_version = _read_pyproject_version(root)
    changelog_version = _read_changelog_latest(root)

    if changelog_version is None:
        raise AssertionError("CHANGELOG.md contains no released ## [x.y.z] heading")

    if not (version_file == pyproject_version == changelog_version):
        raise AssertionError(
            "version lockstep drift: "
            f"VERSION={version_file!r} "
            f"pyproject={pyproject_version!r} "
            f"CHANGELOG latest={changelog_version!r} — "
            "all three must match; bump them together"
        )


# -- existing handler tests -------------------------------------------------


class _FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self) -> None:
        self.message = _FakeMessage()


def test_running_version_matches_installed_distribution() -> None:
    assert running_version() == f"v{distribution_version('nexus-ai-agent')}"
    assert "3.0.0" not in running_version()


def test_running_version_matches_the_release_files() -> None:
    """`VERSION`, `pyproject.toml` and the installed metadata must agree."""
    file_version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert running_version() == f"v{file_version}"


def test_running_version_falls_back_to_the_repo_version_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing(_name: str) -> str:
        raise PackageNotFoundError(_name)

    monkeypatch.setattr(update_handlers, "_distribution_version", _missing)
    expected = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert running_version() == f"v{expected}"


def test_running_version_reports_unknown_only_when_nothing_is_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _missing(_name: str) -> str:
        raise PackageNotFoundError(_name)

    monkeypatch.setattr(update_handlers, "_distribution_version", _missing)
    monkeypatch.setattr(update_handlers, "_version_from_repo_checkout", lambda: None, raising=True)
    assert running_version() == "v0.0.0+unknown"


async def test_version_cmd_replies_with_the_running_version() -> None:
    update = _FakeUpdate()
    context = SimpleNamespace(bot=SimpleNamespace())
    await version_cmd(update, context)  # type: ignore[arg-type]
    assert update.message.replies == [f"🤖 نسخه فعلی: {running_version()}"]


# -- lockstep guard (the CI gate itself) ----------------------------------


def test_versions_in_lockstep_on_repo_root() -> None:
    """On the real repo, the three sources must agree (green on main)."""
    assert_versions_in_lockstep(REPO_ROOT)


def test_changelog_latest_is_parseable() -> None:
    assert _read_changelog_latest(REPO_ROOT) is not None
    # Must be a valid semver triple
    ver = _read_changelog_latest(REPO_ROOT)
    assert ver is not None
    assert re.fullmatch(r"\d+\.\d+\.\d+", ver)


def test_pyproject_version_is_parseable() -> None:
    ver = _read_pyproject_version(REPO_ROOT)
    assert re.fullmatch(r"\d+\.\d+\.\d+", ver)


def test_lockstep_fails_on_mismatched_fixture(tmp_path: Path) -> None:
    """A mismatched fixture must be demonstrably red (the guard works)."""
    # Arrange a minimal repo layout in tmp_path with intentional drift
    (tmp_path / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nversion="9.9.8"\n', encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [9.9.7] — 2026-01-01\n\n- old\n", encoding="utf-8"
    )
    with pytest.raises(AssertionError, match="version lockstep drift"):
        assert_versions_in_lockstep(tmp_path)

    # Single-source drift is also red
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nversion="9.9.9"\n', encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [9.9.8] — 2026-01-01\n\n- old\n", encoding="utf-8"
    )
    with pytest.raises(AssertionError, match="CHANGELOG latest"):
        assert_versions_in_lockstep(tmp_path)


def test_lockstep_fails_when_changelog_has_no_release(tmp_path: Path) -> None:
    (tmp_path / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nversion="1.0.0"\n', encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n- nothing released yet\n", encoding="utf-8"
    )
    with pytest.raises(AssertionError, match="no released"):
        assert_versions_in_lockstep(tmp_path)


def test_changelog_unreleased_heading_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "VERSION").write_text("2.0.0\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nversion="2.0.0"\n', encoding="utf-8"
    )
    unreleased = (
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- new\n\n"
        "## [2.0.0] — 2026-09-21\n\n- first release\n"
    )
    (tmp_path / "CHANGELOG.md").write_text(unreleased, encoding="utf-8")
    # Must remain green — Unreleased does not participate
    assert_versions_in_lockstep(tmp_path)
    assert _read_changelog_latest(tmp_path) == "2.0.0"
