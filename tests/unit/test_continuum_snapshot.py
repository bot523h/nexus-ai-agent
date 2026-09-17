"""Unit tests for the cross-turn continuum snapshot.

The snapshot's whole job is to be *trustworthy*: a file that silently mis-reads
is worse than no file, because a later session acts on it.  These tests pin the
three properties that make it trustworthy:

1. round-tripping is lossless;
2. an unknown or malformed file is refused, never guessed at;
3. ``verify`` distinguishes "stale" (normal) from "diverged" (history was
   rewritten) from "unchecked" (no git available), and never mutates anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from nexus_ai_agent.continuum import (
    SCHEMA_VERSION,
    ContinuumError,
    ContinuumSnapshot,
    PendingItem,
    PhaseRecord,
    current_git_state,
    default_snapshot_path,
    is_ancestor,
    load,
    save,
    verify,
)


def _snapshot(**overrides: object) -> ContinuumSnapshot:
    base: dict[str, object] = {
        "phase": "D",
        "phase_status": "complete",
        "last_good_commit": "f448e453d13694a6730a1873bec30d054b",
        "last_good_branch": "arena/01a0b0bf-nexus-ai-agent",
        "test_count_expected": 179,
        "env_fingerprint": {"python": "3.11.2", "alembic": "1.20.0"},
        "phases_completed": (PhaseRecord(id="C1", commit="c81f299"),),
        "pending": (PendingItem(id="D9", status="blocked", reason="awaiting_neon_url"),),
        "next_phase": "E",
    }
    base.update(overrides)
    return ContinuumSnapshot(**base)  # type: ignore[arg-type]


class TestRoundTrip:
    def test_to_dict_from_dict_is_lossless(self) -> None:
        original = _snapshot()
        assert ContinuumSnapshot.from_dict(original.to_dict()) == original

    def test_survives_a_real_file(self, tmp_path: Path) -> None:
        target = tmp_path / ".nexus" / "continuum.json"
        save(_snapshot(), target)
        assert load(target) == _snapshot()

    def test_legacy_d_phases_key_is_accepted(self, tmp_path: Path) -> None:
        """The phase-D-era key name still parses, so old snapshots stay readable."""
        data = _snapshot().to_dict()
        data["d_phases_completed"] = data.pop("phases_completed")
        target = tmp_path / "continuum.json"
        target.write_text(json.dumps(data), encoding="utf-8")
        assert load(target).phases_completed == (PhaseRecord(id="C1", commit="c81f299"),)

    def test_save_is_deterministic_and_reviewable(self, tmp_path: Path) -> None:
        target = tmp_path / "continuum.json"
        save(_snapshot(), target)
        first = target.read_text(encoding="utf-8")
        save(_snapshot(), target)
        assert target.read_text(encoding="utf-8") == first
        assert first.endswith("\n")
        assert json.loads(first)["schema_version"] == SCHEMA_VERSION

    def test_save_creates_the_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / ".nexus" / "continuum.json"
        assert save(_snapshot(), target) == target
        assert target.exists()

    def test_default_path_is_inside_the_repository(self) -> None:
        path = default_snapshot_path()
        assert path.name == "continuum.json"
        assert path.parent.name == ".nexus"
        assert (path.parents[1] / "pyproject.toml").exists()


class TestRefusals:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ContinuumError, match="no continuum snapshot"):
            load(tmp_path / "absent.json")

    def test_corrupt_json_raises(self, tmp_path: Path) -> None:
        target = tmp_path / "continuum.json"
        target.write_text("{not json", encoding="utf-8")
        with pytest.raises(ContinuumError, match="unreadable"):
            load(target)

    def test_non_object_json_raises(self, tmp_path: Path) -> None:
        target = tmp_path / "continuum.json"
        target.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ContinuumError, match="not a JSON object"):
            load(target)

    def test_unknown_schema_version_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "continuum.json"
        data = _snapshot().to_dict()
        data["schema_version"] = SCHEMA_VERSION + 1
        target.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ContinuumError, match="schema_version"):
            load(target)

    def test_missing_required_key_is_reported(self, tmp_path: Path) -> None:
        target = tmp_path / "continuum.json"
        data = _snapshot().to_dict()
        del data["phase"]
        target.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ContinuumError, match="missing key"):
            load(target)


class TestVerify:
    def test_reports_a_problem_when_no_snapshot_exists(self, tmp_path: Path) -> None:
        report = verify(path=tmp_path / "absent.json")
        assert report.ok is False
        assert report.checks["loaded"] is False

    def test_incomplete_phase_is_a_problem(self, tmp_path: Path) -> None:
        target = save(_snapshot(phase_status="in_progress"), tmp_path / "c.json")
        report = verify(path=target)
        assert report.ok is False
        assert any("not complete" in problem for problem in report.problems)

    def test_test_count_mismatch_is_a_problem(self, tmp_path: Path) -> None:
        target = save(_snapshot(), tmp_path / "c.json")
        report = verify(path=target, actual_test_count=12)
        assert report.ok is False
        assert any("collected 12 tests" in problem for problem in report.problems)

    def test_matching_test_count_is_not_a_problem(self, tmp_path: Path) -> None:
        target = save(_snapshot(), tmp_path / "c.json")
        report = verify(path=target, actual_test_count=179)
        assert not any("collected" in problem for problem in report.problems)

    def test_head_at_the_snapshot_commit_is_consistent(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path / "repo")
        head = repo.head
        target = save(
            _snapshot(last_good_commit=head, last_good_branch=repo.branch), tmp_path / "c.json"
        )
        report = verify(path=target, root=repo.path)
        assert report.checks["head_relation"] == "at snapshot commit"
        assert report.ok is True

    def test_head_ahead_of_snapshot_is_reported_as_stale(self, tmp_path: Path) -> None:
        """Normal, healthy drift: work continued after the snapshot was written."""
        repo = _make_repo(tmp_path / "repo")
        target = save(_snapshot(last_good_commit=repo.commits[0]), tmp_path / "c.json")
        report = verify(path=target, root=repo.path)
        assert "ancestor of HEAD" in str(report.checks["head_relation"])
        assert "1 commit(s) newer" in str(report.checks["head_relation"])
        # Ordinary progress is reported, not failed...
        assert not any("re-save it" in problem for problem in report.problems)
        # ...unless the caller asks for strictness.
        strict = verify(path=target, root=repo.path, strict=True)
        assert any("re-save it" in problem for problem in strict.problems)

    def test_unrelated_commit_is_reported_as_diverged(self, tmp_path: Path) -> None:
        """A commit on a sibling branch means history diverged or was rewritten."""
        repo = _make_repo(tmp_path / "repo", sibling=True)
        target = save(_snapshot(last_good_commit=repo.sibling_commit or ""), tmp_path / "c.json")
        report = verify(path=target, root=repo.path)
        assert report.checks["head_relation"] == "diverged"
        assert any("diverged" in problem for problem in report.problems)

    def test_git_checks_degrade_to_unchecked_without_a_repository(self, tmp_path: Path) -> None:
        """A source tarball has no git; verify must still run and say so."""
        target = save(_snapshot(), tmp_path / "c.json")
        report = verify(path=target, root=tmp_path / "not-a-repo")
        assert report.checks["head"] == "unchecked"
        assert report.checks["branch"] == "unchecked"
        assert not any("diverged" in problem for problem in report.problems)

    def test_verify_never_writes(self, tmp_path: Path) -> None:
        target = save(_snapshot(), tmp_path / "c.json")
        before = target.read_bytes()
        verify(path=target, actual_test_count=1)
        assert target.read_bytes() == before

    def test_branch_drift_is_informational_only(self, tmp_path: Path) -> None:
        """A closed phase is reviewed from main after the merge, so drift is normal."""
        repo = _make_repo(tmp_path / "repo")
        target = save(
            _snapshot(last_good_commit=repo.head, last_good_branch="some-other-branch"),
            tmp_path / "c.json",
        )
        report = verify(path=target, root=repo.path)
        assert report.checks["branch_matches"] is False
        assert not any("branch" in problem for problem in report.problems)


class TestGitHelpers:
    def test_is_ancestor_distinguishes_direction(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path / "repo")
        assert is_ancestor(repo.commits[0], repo.head, repo.path) is True
        assert is_ancestor(repo.head, repo.commits[0], repo.path) is False

    def test_is_ancestor_returns_none_for_an_unknown_object(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path / "repo")
        assert is_ancestor("deadbeef" * 5, repo.head, repo.path) is None

    def test_current_git_state_is_none_outside_a_repository(self, tmp_path: Path) -> None:
        assert current_git_state(tmp_path / "not-a-repo") is None


@dataclass(frozen=True)
class _Repo:
    """A throwaway git repository used to make the ancestry tests hermetic."""

    path: Path
    commits: tuple[str, ...]
    sibling_commit: str | None

    @property
    def head(self) -> str:
        return self.commits[-1]

    @property
    def branch(self) -> str:
        return "main"


def _git(path: Path, *args: str) -> str:
    """Run git inside ``path`` and return stripped stdout."""
    import subprocess

    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _make_repo(path: Path, *, sibling: bool = False) -> _Repo:
    """Create a small repo with two commits (and optionally a sibling branch)."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Continuum Test")
    _git(path, "config", "commit.gpgsign", "false")

    commits: list[str] = []
    for index in (1, 2):
        (path / f"f{index}.txt").write_text(f"{index}\n", encoding="utf-8")
        _git(path, "add", f"f{index}.txt")
        _git(path, "commit", "-q", "-m", f"commit {index}")
        commits.append(_git(path, "rev-parse", "HEAD"))

    sibling_commit: str | None = None
    if sibling:
        _git(path, "checkout", "-q", "-b", "sibling", commits[0])
        (path / "sibling.txt").write_text("divergent\n", encoding="utf-8")
        _git(path, "add", "sibling.txt")
        _git(path, "commit", "-q", "-m", "sibling commit")
        sibling_commit = _git(path, "rev-parse", "HEAD")
        _git(path, "checkout", "-q", "main")

    return _Repo(path=path, commits=tuple(commits), sibling_commit=sibling_commit)
