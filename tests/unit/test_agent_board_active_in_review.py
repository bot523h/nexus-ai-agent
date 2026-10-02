"""Regression tests for review-phase claim visibility on the multi-agent board.

Open PRs use ``status: active_in_review`` once coding stops but the branch still
owns its files.  The referee must therefore treat those claims exactly like an
active lease; otherwise another agent can edit a review branch's exclusive path
without any warning.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
AGENT_BOARD_PATH = REPO_ROOT / "scripts" / "agent_board.py"

spec = importlib.util.spec_from_file_location("agent_board", AGENT_BOARD_PATH)
assert spec is not None and spec.loader is not None
agent_board = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent_board)


@pytest.fixture(autouse=True)
def _pin_board_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze board time so lease-liveness assertions never read the wall clock.

    The claim fixtures pin ``claimed_at`` to ``2026-09-21T12:00:00Z``; without
    a pinned ``agent_board._now`` the liveness tests went red the instant real
    time passed ``claimed_at + ttl_hours`` (first red CI run: 2026-09-22T12:44Z,
    every push afterwards).  Same hermetic pattern as
    ``tests/unit/test_agent_board_pr_visibility.py``; 13:00Z keeps every
    default-claim fixture exactly one hour old.
    """
    monkeypatch.setattr(
        agent_board,
        "_now",
        lambda: datetime(2026, 9, 21, 13, 0, tzinfo=timezone.utc),
    )


def _claim(
    *,
    task: str = "pr-open",
    status: str = "active_in_review",
    branch: str = "arena/other-agent",
    claimed_at: str = "2026-09-21T12:00:00Z",
    paths: list[str] | None = None,
) -> dict:
    return {
        "task": task,
        "zone": "docs",
        "agent_branch": branch,
        "claimed_at": claimed_at,
        "ttl_hours": 24,
        "status": status,
        "gates_owner": False,
        "exclusive_paths": paths or ["docs/", "tests/unit/test_docs_integrity.py"],
        "scope": "fixture",
        "note": "",
    }


def test_active_in_review_paths_block_other_branches() -> None:
    board = {"claims": [_claim()]}

    hits = agent_board._conflicting_paths(
        board,
        "arena/new-agent",
        ["docs/architecture.md", "src/unrelated.py"],
    )

    assert hits == [("pr-open", "docs/architecture.md")]


def test_active_in_review_keeps_directory_and_exact_file_semantics() -> None:
    board = {
        "claims": [
            _claim(paths=["docs/", "tests/unit/test_docs_integrity.py"]),
        ]
    }

    hits = agent_board._conflicting_paths(
        board,
        "arena/new-agent",
        [
            "docs",  # directory root is also owned by a docs/ exclusive path
            "tests/unit/test_docs_integrity.py",  # exact file match
            "tests/unit/test_docs_integrity_extra.py",  # not an exact match
        ],
    )

    assert hits == [
        ("pr-open", "docs"),
        ("pr-open", "tests/unit/test_docs_integrity.py"),
    ]


def test_active_in_review_does_not_block_its_own_branch() -> None:
    board = {"claims": [_claim(branch="arena/same-agent")]}

    assert agent_board._conflicting_paths(board, "arena/same-agent", ["docs/a.md"]) == []


def test_gc_expires_stale_active_in_review_leases(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(agent_board, "_now", lambda: fixed_now)
    stale = fixed_now - timedelta(hours=25)
    board = {"claims": [_claim(claimed_at=agent_board._iso(stale))]}

    freed = agent_board.gc_expired(board)

    assert freed == ["pr-open"]
    claim = board["claims"][0]
    assert claim["status"] == "expired"
    assert "stale active_in_review lease" in claim["note"]


def test_claim_refuses_a_live_active_in_review_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    board_path = tmp_path / "board.json"
    board_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "updated_at": "2026-09-21T12:00:00Z",
                "claims": [_claim()],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_board, "BOARD", board_path)

    rc = agent_board.cmd_claim(
        argparse.Namespace(task="pr-open", branch="arena/new-agent", ttl=24, gates=False)
    )

    assert rc == 2
    out = capsys.readouterr().out
    assert "status active_in_review" in out
    persisted = json.loads(board_path.read_text(encoding="utf-8"))
    assert persisted["claims"][0]["agent_branch"] == "arena/other-agent"
    assert persisted["claims"][0]["status"] == "active_in_review"
