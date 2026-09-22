"""Tests for `agent_board.py praudit` — open-PR × board visibility audit (task-141).

Read-only guard: an open PR whose scope the board cannot attribute (no claim for
the head branch, or changed files outside every live exclusive path) must be
reported as INVISIBLE — the exact failure mode that made a previous session
duplicate PR#39's work.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
AGENT_BOARD_PATH = REPO_ROOT / "scripts" / "agent_board.py"

spec = importlib.util.spec_from_file_location("agent_board", AGENT_BOARD_PATH)
assert spec is not None and spec.loader is not None
agent_board = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent_board)


def _claim(
    *,
    task: str = "some-task",
    branch: str = "arena/owner-branch",
    status: str = "active",
    claimed_at: str = "2026-09-21T12:00:00Z",
    ttl_hours: int = 24,
    paths: list[str] | None = None,
) -> dict:
    return {
        "task": task,
        "zone": "z",
        "agent_branch": branch,
        "claimed_at": claimed_at,
        "ttl_hours": ttl_hours,
        "status": status,
        "gates_owner": False,
        "exclusive_paths": paths if paths is not None else ["src/pkg/"],
        "scope": "fixture",
        "note": "",
    }


def _board(claims: list[dict]) -> dict:
    return {"schema": 1, "updated_at": "2026-09-21T12:00:00Z", "claims": claims}


def _pr(number: int, head: str, files: list[str], title: str = "t") -> dict:
    return {"number": number, "title": title, "head_branch": head, "files": files}


# ── core: audit_pr_visibility ─────────────────────────────────────────


def test_pr_without_any_claim_is_invisible() -> None:
    result = agent_board.audit_pr_visibility(_board([]), [_pr(99, "arena/newcomer", ["a.py"])])
    row = result["prs"][0]
    assert row["invisible"] is True
    assert "no live board claim for head branch" in row["reasons"]
    assert result["invisible_count"] == 1
    assert result["invisible_numbers"] == [99]


def test_claimed_pr_with_fenced_files_is_visible() -> None:
    board = _board([_claim(branch="arena/owner-branch", paths=["src/pkg/"])])
    prs = [_pr(7, "arena/owner-branch", ["src/pkg/mod.py", ".agents/board.json"])]
    result = agent_board.audit_pr_visibility(board, prs)
    row = result["prs"][0]
    assert row["invisible"] is False
    assert row["own_claims"] == ["some-task"]
    assert row["uncovered_files"] == []  # coordination file skipped


def test_uncovered_files_make_a_claimed_pr_invisible() -> None:
    board = _board([_claim(branch="arena/owner-branch", paths=["src/pkg/"])])
    prs = [_pr(7, "arena/owner-branch", ["src/pkg/mod.py", "src/other/leak.py"])]
    result = agent_board.audit_pr_visibility(board, prs)
    row = result["prs"][0]
    assert row["invisible"] is True
    assert row["uncovered_files"] == ["src/other/leak.py"]
    assert any("outside every exclusive_paths" in r for r in row["reasons"])


def test_foreign_fences_are_reported_but_do_not_hide_a_claimed_pr() -> None:
    board = _board(
        [
            _claim(task="mine", branch="arena/me", paths=["src/mine/"]),
            _claim(task="theirs", branch="arena/them", paths=["docs/"]),
        ]
    )
    prs = [_pr(3, "arena/me", ["src/mine/a.py", "docs/guide.md"])]
    result = agent_board.audit_pr_visibility(board, prs)
    row = result["prs"][0]
    assert row["invisible"] is False
    assert row["foreign_fences"] == ["theirs"]  # collision visible before push


def test_record_claim_with_empty_paths_does_not_fence_anything() -> None:
    board = _board([_claim(task="record", branch="arena/rec", paths=[])])
    prs = [_pr(4, "arena/rec", ["src/whatever.py"])]
    result = agent_board.audit_pr_visibility(board, prs)
    row = result["prs"][0]
    # claim exists → head is known, but zero coverage → invisible by files
    assert row["own_claims"] == ["record"]
    assert row["invisible"] is True
    assert row["uncovered_files"] == ["src/whatever.py"]


def test_expired_lease_provides_no_coverage_and_no_fences() -> None:
    stale = _claim(
        branch="arena/stale",
        claimed_at="2026-09-19T12:00:00Z",  # 48h ago, ttl 24h
        paths=["src/pkg/"],
    )
    board = _board([stale])
    prs = [_pr(5, "arena/stale", ["src/pkg/mod.py"])]
    result = agent_board.audit_pr_visibility(board, prs)
    row = result["prs"][0]
    assert row["own_claims"] == []
    assert row["uncovered_files"] == ["src/pkg/mod.py"]
    assert row["invisible"] is True


def test_active_in_review_claim_counts_as_live() -> None:
    board = _board([_claim(branch="arena/reviewing", status="active_in_review", paths=["docs/"])])
    prs = [_pr(6, "arena/reviewing", ["docs/arch.md"])]
    result = agent_board.audit_pr_visibility(board, prs)
    assert result["prs"][0]["invisible"] is False


def test_files_as_dicts_are_normalized() -> None:
    board = _board([_claim(branch="arena/me", paths=["src/pkg/"])])
    pr = {
        "number": 8,
        "title": "t",
        "head": {"ref": "arena/me"},  # head_branch absent → head.ref fallback
        "files": [{"filename": "src/pkg/x.py"}],
    }
    result = agent_board.audit_pr_visibility(board, [pr])
    row = result["prs"][0]
    assert row["head_branch"] == "arena/me"
    assert row["files"] == ["src/pkg/x.py"]
    assert row["invisible"] is False


# ── command surface: cmd_praudit ──────────────────────────────────────


def _args(tmp_path: Path, prs: list[dict], **overrides: object) -> argparse.Namespace:
    fixture = tmp_path / "prs.json"
    fixture.write_text(json.dumps(prs), encoding="utf-8")
    defaults: dict = {
        "pr_json": str(fixture),
        "repo": "",
        "token": "",
        "fail_on_invisible": False,
        "as_json": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_cmd_praudit_fixture_exit_codes_and_json(tmp_path: Path, capsys: object) -> None:
    board = _board([_claim(branch="arena/me", paths=["src/pkg/"])])
    visible = _pr(1, "arena/me", ["src/pkg/a.py"])
    invisible = _pr(2, "arena/unknown", ["src/pkg/b.py"])

    # real board is loaded by cmd_praudit — patch load_board to the fixture
    original = agent_board.load_board
    agent_board.load_board = lambda: json.loads(json.dumps(board))  # type: ignore[assignment]
    try:
        args = _args(tmp_path, [visible, invisible])
        code = agent_board.cmd_praudit(args)
        out = capsys.readouterr().out  # type: ignore[attr-defined]
        assert code == 0  # report-only by default
        assert "1 invisible" in out or "invisible on the board" in out
        assert "PR#2 [INVISIBLE]" in out
        assert "PR#1 [visible]" in out

        args = _args(tmp_path, [visible, invisible], fail_on_invisible=True)
        assert agent_board.cmd_praudit(args) == 1
        capsys.readouterr()  # drain human report before the JSON assertion

        args = _args(tmp_path, [visible], as_json=True)
        assert agent_board.cmd_praudit(args) == 0
        payload = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
        assert payload["invisible_count"] == 0
        assert payload["total"] == 1
    finally:
        agent_board.load_board = original  # type: ignore[assignment]


def test_cmd_praudit_refuses_live_mode_without_repo(tmp_path: Path) -> None:
    args = argparse.Namespace(pr_json="", repo="", token="", fail_on_invisible=False, as_json=False)
    code = agent_board.cmd_praudit(args)
    assert code == 2


def test_path_matches_directory_and_exact_semantics() -> None:
    assert agent_board._path_matches("docs/", "docs/a.md") is True
    assert agent_board._path_matches("docs/", "docs") is True
    assert agent_board._path_matches("docs/", "docsx/a.md") is False
    assert agent_board._path_matches("docs/x.py", "docs/x.py") is True
    assert agent_board._path_matches("docs/x.py", "docs/x.py.bak") is False
