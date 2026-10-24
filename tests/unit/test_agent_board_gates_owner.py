"""Advisory ``gates_owner`` proofs for ``agent_board.py check`` (schema 2).

POLICY (2026-10-22, PR #211): there is **no mandatory global gates_owner**.
Every agent runs its own gates and records its own evidence; ``check`` must not
be blocked by a missing, doubled, or expired gate holder. ``_active_gates_owners``
therefore returns ``(live_owners, malformed)`` as *information*, and ``cmd_check``
stays exit 0 regardless of how many owners exist. These tests pin that law so a
future edit cannot silently reintroduce a fail-closed gate requirement — while
still proving the **lease-liveness** property that decides which claims are
"live" at all (the mutation probe below fails if the expiry check is dropped).

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


def test_zero_live_gates_owners_is_an_advisory_pass(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No live gate holder is a valid governance state: check stays exit 0."""
    _wire(cli, monkeypatch, _board([_claim(task="no-gate", gates=False)]))
    owners, malformed = cli._active_gates_owners(cli.load_board())
    assert owners == [] and malformed == []
    assert cli.cmd_check(_args()) == 0


def test_multiple_live_gates_owners_is_an_advisory_pass(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two live gate holders is informative, not an ambiguous failure."""
    board = _board([_claim(task="gate-a", gates=True), _claim(task="gate-b", gates=True)])
    _wire(cli, monkeypatch, board)
    owners, malformed = cli._active_gates_owners(board)
    assert len(owners) == 2 and malformed == []
    assert cli.cmd_check(_args()) == 0


def test_expired_gates_owner_is_not_live(cli: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """An expired gate holder is a departed holder: not counted as live."""
    board = _board(
        [_claim(task="gate", gates=True, claimed_at="2025-01-01T00:00:00Z", ttl_hours=1)]
    )
    _wire(cli, monkeypatch, board)
    owners, malformed = cli._active_gates_owners(board)
    assert owners == [] and malformed == []
    assert cli.cmd_check(_args()) == 0


def test_exactly_one_live_gates_owner_is_reported(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: a single live owner is reported as live."""
    board = _board([_claim(task="gate", gates=True)])
    _wire(cli, monkeypatch, board)
    owners, malformed = cli._active_gates_owners(board)
    assert len(owners) == 1 and malformed == []
    assert cli.cmd_check(_args()) == 0


def test_expired_owner_proof_is_sensitive_to_the_liveness_gate(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation probe: dropping the lease-expiry check makes the expired owner live.

    Patching ``_claim_live`` to always return True (mutation M: remove the lease
    expiry check) turns the expired claim into a live owner, which proves the
    ``owners == []`` assertion above is exactly the liveness safety property and
    not an artifact of the advisory policy.
    """
    board = _board(
        [_claim(task="gate", gates=True, claimed_at="2025-01-01T00:00:00Z", ttl_hours=1)]
    )
    _wire(cli, monkeypatch, board)
    monkeypatch.setattr(cli, "_claim_live", lambda claim: True)  # mutation M
    owners, malformed = cli._active_gates_owners(board)
    assert len(owners) == 1 and malformed == []
