"""Unit tests for the board-Git truth reconciler (task-163, ADR 0005).

The pure core (``reconcile``) is exercised with synthetic boards/PRs; the
CLI layer is exercised with monkeypatched git/gh so no network is needed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "board_reconcile.py"
_spec = importlib.util.spec_from_file_location("board_reconcile_under_test", SCRIPT)
br = importlib.util.module_from_spec(_spec)
assert _spec is not None and _spec.loader is not None
sys.modules["board_reconcile_under_test"] = br
_spec.loader.exec_module(br)

NOW = datetime(2026, 9, 23, 19, 0, 0, tzinfo=timezone.utc)
TS = "2026-09-23T19:00:00Z"


def _claim(
    task: str,
    branch: str | None,
    *,
    status: str = "active",
    paths: list[str] | None = None,
    claimed_at: str = TS,
    ttl: int = 24,
) -> dict[str, Any]:
    return {
        "task": task,
        "status": status,
        "agent_branch": branch,
        "claimed_at": claimed_at,
        "ttl_hours": ttl,
        "exclusive_paths": paths or [],
    }


def _pr(number: int, branch: str, files: list[str]) -> dict[str, Any]:
    return {"number": number, "branch": branch, "files": files}


def test_clean_board_and_prs_have_no_findings() -> None:
    board = {"claims": [_claim("task-1", "arena/b1", paths=["src/a.py"])]}
    live = {"main", "arena/b1"}
    report = br.reconcile(board, live, [_pr(7, "arena/b1", ["src/a.py"])], [], now=NOW)
    assert report.clean
    assert report.warnings == []


def test_merged_still_open_is_blocking() -> None:
    board = {"claims": [_claim("task-145", "arena/dead", paths=[])]}
    merged = [{"number": 52, "branch": "arena/dead"}]
    report = br.reconcile(board, {"main"}, [], merged, now=NOW)
    kinds = [f.kind for f in report.blocking]
    assert "MERGED_STILL_OPEN" in kinds
    assert "PR#52" in report.blocking[0].detail


def test_branch_gone_is_blocking() -> None:
    board = {"claims": [_claim("task-9", "arena/gone", paths=[])]}
    report = br.reconcile(board, {"main"}, [], [], now=NOW)
    assert [f.kind for f in report.blocking] == ["BRANCH_GONE"]


def test_current_branch_is_live_so_push_in_flight_is_not_branch_gone() -> None:
    # The I/O layer merges the current branch into the live set; here we
    # simulate that (a push is in flight: ls-remote has not seen it yet).
    board = {"claims": [_claim("task-9", "arena/inflight", paths=[])]}
    report = br.reconcile(board, {"main", "arena/inflight"}, [], [], now=NOW)
    assert report.clean


def test_unclaimed_agent_branch_pr_is_blocking() -> None:
    board = {"claims": []}
    report = br.reconcile(
        board, {"main", "arena/orphan"}, [_pr(33, "arena/orphan", ["src/x.py"])], [], now=NOW
    )
    kinds = [f.kind for f in report.blocking]
    assert "UNCLAIMED_OPEN_PR" in kinds
    assert "INVISIBLE_FILE" in kinds  # no claim ⇒ nothing fences its files either


def test_non_agent_prefix_pr_is_not_required_to_be_claimed() -> None:
    board = {"claims": []}
    report = br.reconcile(
        board, {"main", "feature/human"}, [_pr(34, "feature/human", ["src/x.py"])], [], now=NOW
    )
    assert report.clean


def test_invisible_file_is_blocking() -> None:
    board = {"claims": [_claim("task-1", "arena/b1", paths=["src/fenced.py"])]}
    report = br.reconcile(
        board, {"arena/b1"}, [_pr(7, "arena/b1", ["src/fenced.py", "src/secret.py"])], [], now=NOW
    )
    finding = report.blocking[0]
    assert finding.kind == "INVISIBLE_FILE"
    assert finding.files == ("src/secret.py",)


def test_coordination_board_file_is_exempt_from_invisible_file() -> None:
    board = {"claims": [_claim("task-1", "arena/b1", paths=[])]}
    report = br.reconcile(
        board, {"arena/b1"}, [_pr(7, "arena/b1", [".agents/board.json"])], [], now=NOW
    )
    assert report.clean


def test_directory_fence_covers_files_inside_it() -> None:
    board = {"claims": [_claim("task-1", "arena/b1", paths=["src/nexus_ai_agent/stateful/"])]}
    report = br.reconcile(
        board,
        {"arena/b1"},
        [_pr(7, "arena/b1", ["src/nexus_ai_agent/stateful/rate_limit.py"])],
        [],
        now=NOW,
    )
    assert report.clean


def test_lease_expired_is_a_warning_not_blocking() -> None:
    # claimed 25h ago with a 24h lease → expired.
    board = {"claims": [_claim("task-1", "arena/b1", paths=[], claimed_at="2026-09-22T18:00:00Z")]}
    report = br.reconcile(board, {"main", "arena/b1"}, [], [], now=NOW)
    assert report.clean
    assert [f.kind for f in report.warnings] == ["LEASE_EXPIRED"]


def test_scope_truncated_is_a_warning() -> None:
    board = {"claims": [_claim("task-1", "arena/b1", paths=["src/"])]}
    files = [f"src/f{i}.py" for i in range(100)]
    report = br.reconcile(board, {"arena/b1"}, [_pr(7, "arena/b1", files)], [], now=NOW)
    assert report.clean
    assert [f.kind for f in report.warnings] == ["SCOPE_TRUNCATED"]


def test_overlap_with_another_live_claim_is_a_warning() -> None:
    board = {
        "claims": [
            _claim("task-1", "arena/b1", paths=["src/shared.py"]),
            _claim("task-2", "arena/b2", paths=["src/shared.py"]),
        ]
    }
    report = br.reconcile(
        board,
        {"arena/b1", "arena/b2"},
        [_pr(7, "arena/b1", ["src/shared.py"])],
        [],
        now=NOW,
    )
    assert report.clean
    assert [f.kind for f in report.warnings] == ["OVERLAP"]
    assert report.warnings[0].files == ("src/shared.py",)


def test_cli_check_returns_1_on_blocking_and_0_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = tmp_path / "board.json"
    board.write_text(
        json.dumps({"claims": [_claim("task-1", "arena/dead")]}) + "\n", encoding="utf-8"
    )

    monkeypatch.setattr(br, "_live_branches", lambda repo, remote: {"main"})
    monkeypatch.setattr(br, "_open_prs", lambda: [])
    monkeypatch.setattr(br, "_merged_prs", lambda limit: [])

    assert br.main(["--check", "--board", str(board), "--repo", str(tmp_path), "--now", TS]) == 1

    clean = tmp_path / "clean.json"
    clean.write_text(
        json.dumps({"claims": [_claim("task-1", "arena/ok")]}) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(br, "_live_branches", lambda repo, remote: {"main", "arena/ok"})
    assert br.main(["--check", "--board", str(clean), "--repo", str(tmp_path), "--now", TS]) == 0


def test_cli_without_check_always_reports_clean_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = tmp_path / "board.json"
    board.write_text(
        json.dumps({"claims": [_claim("task-1", "arena/dead")]}) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(br, "_live_branches", lambda repo, remote: {"main"})
    monkeypatch.setattr(br, "_open_prs", lambda: [])
    monkeypatch.setattr(br, "_merged_prs", lambda limit: [])
    assert br.main(["--board", str(board), "--repo", str(tmp_path), "--now", TS]) == 0


def test_cli_json_emits_structured_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    board = tmp_path / "board.json"
    board.write_text(
        json.dumps({"claims": [_claim("task-1", "arena/dead")]}) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(br, "_live_branches", lambda repo, remote: {"main"})
    monkeypatch.setattr(br, "_open_prs", lambda: [])
    monkeypatch.setattr(br, "_merged_prs", lambda limit: [])

    assert br.main(["--json", "--board", str(board), "--repo", str(tmp_path), "--now", TS]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"] == {"blocking": 1, "warning": 0}
    assert payload["blocking"][0]["kind"] == "BRANCH_GONE"


def test_live_branches_strip_the_refs_heads_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # git ls-remote returns `sha<TAB>refs/heads/<branch>`; claims carry the
    # bare branch name, so the prefix must be stripped or every live claim
    # reads as BRANCH_GONE (a false blocking drift).
    raw = "abc123\trefs/heads/main\ndef456\trefs/heads/arena/b1\n"

    def _fake_git(repo: Path, args: list[str]) -> str:
        if args[:2] == ["ls-remote", "--heads"]:
            return raw
        if args[:2] == ["branch", "--show-current"]:
            return ""  # no current branch in the fake
        raise AssertionError(args)

    monkeypatch.setattr(br, "_run_git", _fake_git)
    assert br._live_branches(Path("/tmp/nowhere"), "origin") == {"main", "arena/b1"}


def test_gh_file_objects_are_parsed_not_strings() -> None:
    # gh's PR API returns file *objects*; a naive str-only parser would
    # silently drop every file (a false clean).
    files = br._pr_files([{"path": "src/a.py"}, "src/b.py", 42, {"nope": 1}])
    assert files == ["src/a.py", "src/b.py"]
