"""The board-integrity gate must reject every corruption seen in the wild (PR #207)."""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_board_integrity.py"
BOARD = ROOT / ".agents" / "board.json"

_spec = importlib.util.spec_from_file_location("check_board_integrity", SCRIPT)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
sys.modules["check_board_integrity"] = gate
_spec.loader.exec_module(gate)


def _good() -> dict:
    return {
        "schema": 2,
        "protocol": {"protocol_version": 2},
        "zones": [],
        "claims": [{"task": "t1", "status": "done", "zone": "z"}],
        "deferred_log": [],
        "next_work": [],
        "history": {"waves_closed": ["w"], "pull_requests": [{"pr": 1}], "incidents": []},
    }


def _problems(board: dict | bytes) -> list[str]:
    raw = board if isinstance(board, bytes) else json.dumps(board).encode()
    return gate.validate(raw)


def test_the_live_board_is_healthy() -> None:
    assert gate.validate(BOARD.read_bytes()) == []


def test_a_minimal_valid_board_passes() -> None:
    assert _problems(_good()) == []


def test_empty_board_is_rejected() -> None:
    assert _problems(b"") and _problems(b"  \n")


def test_base64_encoded_board_is_rejected_as_in_pr_207() -> None:
    blob = base64.b64encode(BOARD.read_bytes())
    problems = _problems(blob)
    assert problems and "base64" in problems[0]


def test_truncated_json_is_rejected() -> None:
    assert "not valid JSON" in _problems(BOARD.read_bytes()[:2000])[0]


def test_non_utf8_is_rejected() -> None:
    assert "UTF-8" in _problems(b"{\xf5}")[0]


def test_non_object_root_is_rejected() -> None:
    assert _problems(b"[1, 2]")


@pytest.mark.parametrize("key", ["protocol", "zones", "claims", "history", "next_work"])
def test_missing_required_block_is_rejected(key: str) -> None:
    board = _good()
    del board[key]
    assert any(key in p for p in _problems(board))


def test_wrong_schema_version_is_rejected() -> None:
    board = _good()
    board["schema"] = 1
    assert any("schema must be 2" in p for p in _problems(board))


def test_duplicate_task_ids_are_rejected() -> None:
    board = _good()
    board["claims"].append(dict(board["claims"][0]))
    assert any("duplicate" in p for p in _problems(board))


def test_two_active_gates_owners_are_rejected() -> None:
    board = _good()
    board["claims"] = [
        {"task": "a", "status": "active", "zone": "z", "gates_owner": True},
        {"task": "b", "status": "active", "zone": "z", "gates_owner": True},
    ]
    assert any("gates_owner" in p for p in _problems(board))


def test_an_expired_gates_owner_flag_is_not_counted() -> None:
    board = _good()
    board["claims"] = [
        {"task": "a", "status": "expired", "zone": "z", "gates_owner": True},
        {"task": "b", "status": "active", "zone": "z", "gates_owner": True},
    ]
    assert _problems(board) == []


def test_a_drastically_shrunk_board_is_rejected_against_a_baseline() -> None:
    board = _good()
    problems = gate.validate(json.dumps(board).encode(), baseline_claims=97)
    assert any("truncated" in p for p in problems)


def test_cli_exit_codes(tmp_path: Path) -> None:
    run = lambda *a: subprocess.run(  # noqa: E731
        [sys.executable, str(SCRIPT), *a], capture_output=True, text=True
    )
    assert run().returncode == 0
    bad = tmp_path / "board.json"
    bad.write_bytes(base64.b64encode(BOARD.read_bytes()))
    done = run("--board", str(bad))
    assert done.returncode == 1 and "FAIL" in done.stderr
    assert run("--board", str(tmp_path / "missing.json")).returncode == 1
