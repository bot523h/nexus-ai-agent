"""Fail-closed proofs for the single-``gates_owner`` law (schema 2).

``agent_board.py check`` must return exit 2 (UNVERIFIABLE) when the live board
holds zero or multiple active, unexpired ``gates_owner`` claims, and must not
treat an *expired* gate holder as live. The referee already enforces this
(``cmd_check`` -> ``_active_gates_owners``); these tests pin the behaviour so a
future edit cannot silently turn a broken governance state into a clean pass.

Kept in its own module, separate from ``tests/unit/test_agent_board.py``, on
purpose: several long-lived ``arena/*`` branches still edit that file, and a
governance proof must not sit behind a merge conflict.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).parents[2]
SCRIPT = REPO_ROOT / "scripts" / "agent_board.py"
_CLOCK = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)


def _load_cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agent_board_gates_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _claim(
    *,
    task: str,
    gates: bool,
    claimed_at: str = "2026-01-01T00:00:00Z",
    ttl_hours: int = 24,
) -> dict:
    return {
        "task": task,
        "status": "active",
        "zone": "ci-quality",
        "agent_branch": "arena/gates-test",
        "claimed_at": claimed_at,
        "ttl_hours": ttl_hours,
        "gates_owner": gates,
        "exclusive_paths": [],
    }


def _board(claims: list[dict]) -> dict:
    return {
        "schema": 2,
        "protocol": {"protocol_version": 2},
        "zones": [],
        "claims": claims,
        "deferred_log": [],
        "next_work": [],
        "history": [],
    }


def _args(**over: object) -> object:
    base = {
        "files": "docs/x.md",
        "branch": "arena/gates-test",
        "no_remote": False,
        "repo": "",
        "token": "",
    }
    base.update(over)
    return type("A", (), base)()


@pytest.fixture()
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = _load_cli()
    # Freeze the clock: lease liveness is wall-clock arithmetic, so a real board
    # read is a time bomb (the 2026-09-22 clock-bomb class).
    monkeypatch.setattr(module, "_now", lambda: _CLOCK)
    monkeypatch.setattr(module, "BOARD", tmp_path / "board.json")
    return module


def _wire(module: ModuleType, monkeypatch: pytest.MonkeyPatch, board: dict) -> None:
    """Point every source at *board* so only the gate law is under test."""
    module.BOARD.write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(module, "_worktree_boards", lambda exclude: ([], True))
    monkeypatch.setattr(module, "_read_remote_board", lambda: (board, ""))
    monkeypatch.setattr(module, "_open_pr_branches", lambda repo, token: ([], ""))
    monkeypatch.setattr(
        module, "_remote_branch_boards", lambda branches, exclude_branch="": ([], [])
    )


def test_zero_live_gates_owners_is_exit_2(cli: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """No live gate holder must never be reported as a clean pass."""
    _wire(cli, monkeypatch, _board([_claim(task="no-gate", gates=False)]))
    assert cli.cmd_check(_args()) == 2


def test_multiple_live_gates_owners_is_exit_2(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two live gate holders is an ambiguous governance state, not a healthy one."""
    board = _board([_claim(task="gate-a", gates=True), _claim(task="gate-b", gates=True)])
    _wire(cli, monkeypatch, board)
    assert cli.cmd_check(_args()) == 2


def test_expired_gates_owner_is_not_live(cli: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """An expired gate holder is a departed holder: zero live owners, exit 2."""
    board = _board(
        [_claim(task="gate", gates=True, claimed_at="2025-01-01T00:00:00Z", ttl_hours=1)]
    )
    _wire(cli, monkeypatch, board)
    owners, malformed = cli._active_gates_owners(board)
    assert owners == [] and malformed == []
    assert cli.cmd_check(_args()) == 2


def test_exactly_one_live_gates_owner_is_exit_0(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: the harness reaches exit 0 only when the law holds."""
    _wire(cli, monkeypatch, _board([_claim(task="gate", gates=True)]))
    assert cli.cmd_check(_args()) == 0


def test_expired_owner_proof_is_sensitive_to_the_liveness_gate(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation probe: if liveness were ignored, the expired case would pass.

    Patching ``_claim_live`` to always return True (mutation M: drop the lease
    expiry check) turns the expired-owner board into a false clean pass, which
    proves the exit-2 assertion above is exactly the liveness safety property.
    """
    board = _board(
        [_claim(task="gate", gates=True, claimed_at="2025-01-01T00:00:00Z", ttl_hours=1)]
    )
    _wire(cli, monkeypatch, board)
    monkeypatch.setattr(cli, "_claim_live", lambda claim: True)  # mutation M
    assert cli.cmd_check(_args()) == 0
