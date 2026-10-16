"""The PR-census compare direction *and* its base (tooling defects, 2026-10-16).

``scripts/pr_census.py`` feeds every integration ledger, so a mislabelled column
there becomes a wrong decision later.  Two defects were found and fixed:

1. **direction** — the first version compared ``<head>...<base_branch>`` and
   published GitHub's two numbers under each other's names: the row for #218 read
   "26 behind" although ``main`` is an *ancestor* of that head — 26 is the count
   of commits the branch adds.
2. **base** — the compare runs against each PR's *own* base branch, and every row
   published those numbers under main-relative names.  For the two stacked PRs in
   this repository that was not a rounding error: #213 (base = #210's branch) read
   ``unique_commits=12`` / ``commits_behind_main=19`` while the main-relative truth
   was ``26 / 6``.

These tests pin both without touching the network: the module's ``_get`` is
replaced by a recorder.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
CENSUS = REPO / "scripts" / "pr_census.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("nexus_pr_census", CENSUS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["nexus_pr_census"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def census(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, dict[str, str]]:
    module = _load()
    seen: dict[str, str] = {}

    def fake_get(url: str, _token: str) -> dict[str, Any]:
        seen["url"] = url
        return {
            "files": [{"filename": "src/a.py"}, {"filename": "tests/b.py"}],
            "ahead_by": 3,  # commits the PR has that main does not
            "behind_by": 25,  # commits main has that the PR does not
            "total_commits": 3,
        }

    monkeypatch.setattr(module, "_get", fake_get)
    return module, seen


def test_compare_is_asked_in_the_base_then_head_direction(
    census: tuple[Any, dict[str, str]],
) -> None:
    module, seen = census
    module._base_changed_since_merge_base("bot523h/nexus-ai-agent", "deadbeef", "main", "tok")

    assert seen["url"].endswith("/compare/main...deadbeef"), (
        "the compare must be <base_branch>...<head>: the reversed form is the bug"
    )
    assert "/compare/deadbeef..." not in seen["url"]


def test_the_returned_numbers_mean_unique_commits_then_commits_behind(
    census: tuple[Any, dict[str, str]],
) -> None:
    module, _ = census
    files, unique_commits, commits_behind_main, truncated = module._base_changed_since_merge_base(
        "bot523h/nexus-ai-agent", "deadbeef", "main", "tok"
    )

    assert files == ["src/a.py", "tests/b.py"]
    assert unique_commits == 3, "ahead_by belongs to the PR branch, not to main"
    assert commits_behind_main == 25, "behind_by is how far the branch trails main"
    assert truncated is False


def test_a_head_reaching_the_file_cap_is_flagged_as_truncated(
    census: tuple[Any, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    module, _ = census
    monkeypatch.setattr(
        module,
        "_get",
        lambda _url, _token: {
            "files": [{"filename": f"f{i}.py"} for i in range(300)],
            "ahead_by": 1,
            "behind_by": 1,
            "total_commits": 1,
        },
    )
    assert module._base_changed_since_merge_base("o/r", "sha", "main", "tok")[3] is True


def test_the_row_schema_uses_the_corrected_names() -> None:
    """A text-level guard: the inverted or mislabelled names must not come back."""
    source = CENSUS.read_text(encoding="utf-8")

    assert '"unique_commits_vs_base": ahead' in source
    assert '"base_commits_not_in_head": behind' in source
    assert '"unique_commits_vs_main": base_ahead' in source
    assert '"ahead_by": ahead' not in source
    assert '"behind_by": behind' not in source
    # the bare main-relative names may only be written by _main_relative_counts
    assert '"unique_commits": ahead' not in source


def test_a_main_based_pr_is_counted_once_and_copied_into_the_main_fields(
    census: tuple[Any, dict[str, str]],
) -> None:
    module, seen = census
    calls: list[str] = []
    original = module._get

    def counting_get(url: str, token: str) -> dict[str, Any]:
        calls.append(url)
        return original(url, token)

    module._get = counting_get
    try:
        out = module._main_relative_counts(
            "bot523h/nexus-ai-agent", "deadbeef", "main", "main", "tok", 3, 25, 2
        )
    finally:
        module._get = original

    assert calls == [], "a main-based PR needs no second compare"
    assert out == {
        "unique_commits_vs_main": 3,
        "commits_behind_main": 25,
        "changed_files_vs_main": 2,
    }


def test_a_stacked_pr_gets_main_relative_numbers_from_a_second_compare(
    census: tuple[Any, dict[str, str]],
) -> None:
    """#213's real shape: base is another feature branch, main says 26 / 6."""
    module, seen = census
    seen.clear()
    out = module._main_relative_counts(
        "bot523h/nexus-ai-agent", "deadbeef", "feature/base-branch", "main", "tok", 12, 19, 15
    )

    assert seen["url"].endswith("/compare/main...deadbeef"), seen["url"]
    assert out == {
        "unique_commits_vs_main": 3,  # from the recorder, not the stacked 12
        "commits_behind_main": 25,  # from the recorder, not the stacked 19
        "changed_files_vs_main": 2,
    }


def test_a_failed_main_compare_is_negative_and_never_zero(
    census: tuple[Any, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero would read as "level with main"; a failure must not look like that."""
    module, _ = census
    import urllib.error

    def boom(url: str, token: str) -> dict[str, Any]:
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(module, "_get", boom)
    out = module._main_relative_counts(
        "bot523h/nexus-ai-agent", "deadbeef", "feature/base-branch", "main", "tok", 12, 19, 15
    )

    assert out == {
        "unique_commits_vs_main": -1,
        "commits_behind_main": -1,
        "changed_files_vs_main": -1,
    }
