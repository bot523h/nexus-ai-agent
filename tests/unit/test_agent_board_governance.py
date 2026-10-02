"""Executable board governance invariants (task-228).

AGENTS.md §2 and the board's ``protocol.verification_rule`` both say a claimable
task must carry ``evidence_required`` and that exactly one agent holds
``gates_owner``.  Before this module those were *documented* rules only: no
script or test read the field, so a task could be claimed without it and the
gates owner could silently be zero or many.  These tests pin the executed rule,
not the prose:

* :func:`validate_board` reports missing ``evidence_required`` as an ERROR for
  the forward plan (``next_work``) and for active claims, and as a WARN for
  grandfathered legacy claimable claims (historical board state is not
  rewritten);
* the ``gates_owner`` cardinality invariant fails on 0 or >1 active holders and
  passes on exactly one;
* ``claim`` refuses a task that has no ``evidence_required`` (so new work cannot
  be claimed without it) and accepts one that has it.

The real board is exercised read-only; the mutating checks run against copies in
``tmp_path`` so the repository's board is never touched.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).parents[2]
BOARD_PATH = REPO_ROOT / ".agents" / "board.json"
SCRIPT = REPO_ROOT / "scripts" / "agent_board.py"

STEWARD_ACTIVE = {
    "task": "steward",
    "status": "active",
    "zone": "z",
    "agent_branch": "arena/s",
    "claimed_at": "2026-01-01T00:00:00Z",
    "ttl_hours": 24,
    "gates_owner": True,
    "exclusive_paths": ["src/z/"],
    "evidence_required": ["x"],
}
STEWARD_DONE = {
    "task": "steward",
    "status": "done",
    "zone": "z",
    "gates_owner": True,
    "exclusive_paths": ["src/z/"],
}


def _load_board_cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agent_board_governance_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def board_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = _load_board_cli()
    board = json.loads(BOARD_PATH.read_text(encoding="utf-8"))
    copy = tmp_path / "board.json"
    copy.write_text(json.dumps(board, indent=2, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(module, "BOARD", copy)
    return module


def _board(claims: list[dict], next_work: list[dict] | None = None) -> dict:
    return {
        "schema": 2,
        "protocol": {"protocol_version": 2},
        "claims": claims,
        "zones": [{"id": "z", "paths": ["src/z/"]}],
        "deferred_log": [],
        "next_work": next_work
        if next_work is not None
        else [
            {
                "id": "task-n",
                "priority": "P1",
                "zone": "z",
                "title": "t",
                "acceptance_criteria": ["a"],
                "evidence_required": ["pytest -q tests/unit/test_n.py"],
            }
        ],
        "history": {"waves_closed": [], "pull_requests": [], "incidents": []},
    }


def _active_with_evidence() -> dict:
    return {
        "task": "task-x",
        "status": "active",
        "zone": "z",
        "agent_branch": "arena/x",
        "claimed_at": "2026-01-01T00:00:00Z",
        "ttl_hours": 24,
        "gates_owner": False,
        "exclusive_paths": ["src/z/"],
        "evidence_required": ["pytest -q tests/unit/test_x.py"],
    }


def _claim_args(task: str, branch: str = "arena/999-new") -> object:
    return type("A", (), {"task": task, "branch": branch, "ttl": 24, "gates": False})()


# --------------------------------------------------------------------------- #
# the live board satisfies the invariants
# --------------------------------------------------------------------------- #
def test_live_board_has_no_governance_errors(board_module: ModuleType) -> None:
    result = board_module.validate_board(board_module.load_board())
    assert result["errors"] == [], f"live board governance errors: {result['errors']}"
    assert result["ok"] is True


def test_live_board_flags_legacy_gaps_as_warnings_not_errors(board_module: ModuleType) -> None:
    """Grandfathering: historical claimable claims warn, they do not fail the gate."""
    result = board_module.validate_board(board_module.load_board())
    assert any("legacy claimable" in w for w in result["warnings"])
    assert not any("legacy claimable" in e for e in result["errors"])


# --------------------------------------------------------------------------- #
# evidence_required — ERROR for the forward plan and for active claims
# --------------------------------------------------------------------------- #
def test_next_work_entry_without_evidence_required_is_an_error(board_module: ModuleType) -> None:
    board = _board([_active_with_evidence(), STEWARD_ACTIVE])
    del board["next_work"][0]["evidence_required"]
    result = board_module.validate_board(board)
    assert result["ok"] is False
    assert any("next_work task-n" in e and "evidence_required" in e for e in result["errors"])


def test_active_claim_without_evidence_required_is_an_error(board_module: ModuleType) -> None:
    board = _board([_active_with_evidence(), STEWARD_ACTIVE])
    del board["claims"][0]["evidence_required"]
    result = board_module.validate_board(board)
    assert result["ok"] is False
    assert any("active claim task-x" in e for e in result["errors"])


def test_legacy_claimable_without_evidence_required_is_only_a_warning(
    board_module: ModuleType,
) -> None:
    legacy = {
        "task": "legacy-task",
        "status": "queued",
        "zone": "z",
        "exclusive_paths": ["src/z/"],
        "acceptance_criteria": ["a"],
    }
    result = board_module.validate_board(_board([legacy, STEWARD_ACTIVE]))
    assert result["ok"] is True
    assert any("legacy-task" in w for w in result["warnings"])


def test_strict_new_promotes_legacy_warning_to_error(board_module: ModuleType) -> None:
    legacy = {
        "task": "legacy-task",
        "status": "queued",
        "zone": "z",
        "exclusive_paths": ["src/z/"],
        "acceptance_criteria": ["a"],
    }
    strict = board_module.validate_board(_board([legacy, STEWARD_ACTIVE]), strict_new=True)
    assert strict["ok"] is False
    assert any("legacy-task" in e for e in strict["errors"])


# --------------------------------------------------------------------------- #
# gates_owner cardinality
# --------------------------------------------------------------------------- #
def test_zero_gates_owners_is_an_error(board_module: ModuleType) -> None:
    result = board_module.validate_board(_board([_active_with_evidence()]))
    assert result["ok"] is False
    assert any("gates_owner invariant" in e and "found 0" in e for e in result["errors"])


def test_two_gates_owners_is_an_error(board_module: ModuleType) -> None:
    second = dict(STEWARD_ACTIVE, task="steward2", agent_branch="arena/s2")
    result = board_module.validate_board(_board([_active_with_evidence(), STEWARD_ACTIVE, second]))
    assert result["ok"] is False
    assert any("gates_owner invariant" in e and "found 2" in e for e in result["errors"])


def test_a_non_active_gates_owner_does_not_count(board_module: ModuleType) -> None:
    """A finished gates steward must not satisfy the invariant for live work."""
    result = board_module.validate_board(_board([_active_with_evidence(), STEWARD_DONE]))
    assert result["ok"] is False
    assert any("found 0" in e for e in result["errors"])


def test_exactly_one_gates_owner_passes(board_module: ModuleType) -> None:
    result = board_module.validate_board(_board([_active_with_evidence(), STEWARD_ACTIVE]))
    assert result["ok"] is True


def test_an_active_in_review_gates_owner_counts(board_module: ModuleType) -> None:
    """active_in_review is an active lease: its gates_owner must satisfy the invariant."""
    steward = dict(STEWARD_ACTIVE, status="active_in_review")
    result = board_module.validate_board(_board([_active_with_evidence(), steward]))
    assert result["ok"] is True


def test_active_in_review_claim_without_evidence_required_is_an_error(
    board_module: ModuleType,
) -> None:
    claim = dict(_active_with_evidence(), status="active_in_review")
    del claim["evidence_required"]
    result = board_module.validate_board(_board([claim, STEWARD_ACTIVE]))
    assert any("active claim" in e and "evidence_required" in e for e in result["errors"])


# --------------------------------------------------------------------------- #
# claim-time enforcement: new work cannot be claimed without evidence_required
# --------------------------------------------------------------------------- #
def test_claim_refuses_a_task_without_evidence_required(board_module: ModuleType) -> None:
    board = _board(
        [
            {
                "task": "task-no-evidence",
                "status": "queued",
                "zone": "z",
                "exclusive_paths": ["src/z/"],
                "acceptance_criteria": ["a"],
            },
            STEWARD_ACTIVE,
        ]
    )
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")
    assert board_module.cmd_claim(_claim_args("task-no-evidence")) == 2
    reloaded = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    refused = next(c for c in reloaded["claims"] if c["task"] == "task-no-evidence")
    assert refused["status"] == "queued", "a refused claim must not mutate the board"


def test_claim_accepts_a_task_with_evidence_required(board_module: ModuleType) -> None:
    board = _board(
        [
            {
                "task": "task-with-evidence",
                "status": "queued",
                "zone": "z",
                "exclusive_paths": ["src/z/"],
                "acceptance_criteria": ["a"],
                "evidence_required": ["pytest -q tests/unit/test_x.py"],
            },
            STEWARD_ACTIVE,
        ]
    )
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")
    assert board_module.cmd_claim(_claim_args("task-with-evidence")) == 0
    reloaded = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    claimed = next(c for c in reloaded["claims"] if c["task"] == "task-with-evidence")
    assert claimed["status"] == "active"


# --------------------------------------------------------------------------- #
# evidence resolver: a cited test name must be derivable from the tree
# --------------------------------------------------------------------------- #
def test_evidence_resolver_finds_a_real_function_and_file(board_module: ModuleType) -> None:
    # A file-form citation and a function-form citation both resolve.
    result = board_module.resolve_evidence_names(
        "HEAD", ["test_agent_board", "test_top_level_shape_is_schema_2"]
    )
    assert result["missing"] == []
    by_name = {row["name"]: row for row in result["names"]}
    assert by_name["test_agent_board"]["file"] == "tests/unit/test_agent_board.py"
    assert by_name["test_top_level_shape_is_schema_2"]["function_file"].endswith(
        "test_agent_board.py"
    )


def test_evidence_resolver_flags_a_phantom_name(board_module: ModuleType) -> None:
    result = board_module.resolve_evidence_names(
        "HEAD", ["test_this_name_does_not_exist_anywhere_zzz"]
    )
    assert result["missing"] == ["test_this_name_does_not_exist_anywhere_zzz"]


def test_evidence_resolver_is_deterministic(board_module: ModuleType) -> None:
    names = ["test_agent_board", "test_top_level_shape_is_schema_2"]
    first = board_module.resolve_evidence_names("HEAD", names)
    second = board_module.resolve_evidence_names("HEAD", names)
    assert json.dumps(first, sort_keys=False) == json.dumps(second, sort_keys=False)
