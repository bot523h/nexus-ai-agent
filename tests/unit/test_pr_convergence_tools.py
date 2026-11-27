"""Unit tests for the PR-backlog convergence tooling and the board zone fence.

Two audit findings drove this suite:

* the convergence evidence tools measured *name* collisions rather than
  module-aware contracts, and closed PRs without a compare-and-swap on the head
  they audited;
* the ownership referee ignored a live claim's declared zone when the claim
  carried an empty ``exclusive_paths`` — so a PR could rewrite another agent's
  files while ``check`` reported no overlap.

Every assertion is about a pure function; nothing touches the network, and the
board tests patch ``load_board``/``save_board`` so the real
``.agents/board.json`` is never read or written.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / ".agents" / "evidence" / "pr-convergence-2026-11-27" / "tools"
BOARD_CLI = REPO_ROOT / "scripts" / "agent_board.py"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def analyze() -> ModuleType:
    return _load("conv_analyze_under_test", TOOLS / "analyze.py")


@pytest.fixture(scope="module")
def close_prs() -> ModuleType:
    os.environ.setdefault("GITHUB_PERSONAL_ACCESS_TOKEN", "unit-test-token")
    return _load("conv_close_prs_under_test", TOOLS / "close_prs.py")


@pytest.fixture(scope="module")
def board() -> ModuleType:
    return _load("conv_agent_board_under_test", BOARD_CLI)


# --------------------------------------------------------------------------- #
# analyze.py — AST contract extraction
# --------------------------------------------------------------------------- #
def test_extract_symbols_records_module_class_and_method(analyze: ModuleType) -> None:
    source = """
def module_function(a, b=1):
    def inner(x):
        return x
    return inner

class Service:
    def __init__(self, root):
        self.root = root

    async def run(self, request, *args, **kwargs):
        return request
"""
    keys = analyze.extract_symbols(source, "src/pkg/mod.py")
    assert ("src/pkg/mod.py", "module_function", ("a", "b")) in keys
    assert ("src/pkg/mod.py", "module_function.inner", ("x",)) in keys
    assert ("src/pkg/mod.py", "Service", ()) in keys
    assert ("src/pkg/mod.py", "Service.run", ("self", "request", "*args", "**kwargs")) in keys
    assert not any(key[1].endswith("__init__") for key in keys)


def test_extract_symbols_covers_positional_only_and_keyword_only(analyze: ModuleType) -> None:
    source = "def f(a, /, b, *, c):\n    return a\n"
    assert analyze.extract_symbols(source, "m.py") == {("m.py", "f", ("a", "b", "c"))}


def test_extract_symbols_returns_empty_for_invalid_python(analyze: ModuleType) -> None:
    assert analyze.extract_symbols("def broken(:\n", "m.py") == set()


def test_symbol_delta_reports_only_new_contracts(analyze: ModuleType) -> None:
    base = {"m.py": "def keep(a):\n    return a\n"}
    head = {
        "m.py": "def keep(a):\n    return a\n\ndef added(b):\n    return b\n",
        "new.py": "def fresh(c):\n    return c\n",
        "notes.txt": "def not_python(x): pass\n",
    }
    delta = analyze.symbol_delta(base, head)
    assert delta == {("m.py", "added", ("b",)), ("new.py", "fresh", ("c",))}


def test_symbol_delta_detects_a_changed_signature(analyze: ModuleType) -> None:
    base = {"m.py": "def f(a):\n    return a\n"}
    head = {"m.py": "def f(a, b):\n    return a\n"}
    assert analyze.symbol_delta(base, head) == {("m.py", "f", ("a", "b"))}


def test_build_index_and_bare_name_index(analyze: ModuleType) -> None:
    sources = {"a.py": "def f(x):\n    pass\n", "b.py": "class K:\n    pass\n", "c.md": "x"}
    index = analyze.build_index(sources)
    assert index == {("a.py", "f", ("x",)), ("b.py", "K", ())}
    assert analyze.bare_name_index(index) == {"f", "K"}


def test_coverage_report_separates_bare_from_qualified(analyze: ModuleType) -> None:
    main_index = {
        ("src/pkg/other.py", "execute", ("self", "plan")),
        ("src/pkg/mod.py", "keep", ("a",)),
    }
    added = {
        ("src/pkg/mod.py", "keep", ("a",)),  # exact
        ("src/pkg/mod.py", "execute", ("self", "plan")),  # same name+args, other module
        ("src/pkg/mod.py", "keep", ("a", "b")),  # same name, different signature
    }
    report = analyze.coverage_report(added, main_index)
    assert report["added"] == 3
    assert report["bare_hits"] == 3
    assert report["qualified_hits"] == 1
    assert report["qualified_coverage"] == pytest.approx(1 / 3)
    assert report["bare_coverage"] == pytest.approx(1.0)
    assert len(report["missing_contracts"]) == 2


def test_coverage_report_returns_none_for_an_empty_delta(analyze: ModuleType) -> None:
    report = analyze.coverage_report(set(), set())
    assert report["bare_coverage"] is None
    assert report["qualified_coverage"] is None
    assert report["missing_contracts"] == []


def test_verify_superseded_contract_parity_exact(analyze: ModuleType) -> None:
    record = {"number": 1, "added_contracts": [["m.py", "f", ["a"]]]}
    result = analyze.verify_superseded_contract_parity(record, {("m.py", "f", ("a",))})
    assert result["parity"] is True
    assert result["exact_matches"] == 1
    assert result["relocated"] == []
    assert result["missing"] == []


def test_verify_superseded_contract_parity_flags_relocation(analyze: ModuleType) -> None:
    record = {"number": 2, "added_contracts": [["m.py", "f", ["a"]]]}
    result = analyze.verify_superseded_contract_parity(record, {("other.py", "f", ("a",))})
    assert result["parity"] is False
    assert result["relocated"] == [["m.py", "f", ["a"]]]
    assert result["missing"] == []
    assert result["coverage"] == pytest.approx(0.0)


def test_verify_superseded_contract_parity_flags_missing(analyze: ModuleType) -> None:
    record = {"number": 3, "added_symbol_keys": [["m.py", "gone", ["a"]]]}
    result = analyze.verify_superseded_contract_parity(record, set())
    assert result["parity"] is False
    assert result["missing"] == [["m.py", "gone", ["a"]]]


def test_verify_superseded_contract_parity_refuses_an_empty_claim(analyze: ModuleType) -> None:
    result = analyze.verify_superseded_contract_parity({"number": 4}, set())
    assert result["parity"] is False
    assert result["coverage"] is None


def test_coerce_keys_ignores_malformed_entries(analyze: ModuleType) -> None:
    assert analyze._coerce_keys([["m.py", "f", None], ["too", "short"], "junk"]) == {
        ("m.py", "f", ())
    }


def test_const_names_only_counts_new_public_constants(analyze: ModuleType) -> None:
    diff = "+NEW_LIMIT = 3\n+test = 1\n-LIMIT = 2\n+lower = 3\n"
    assert analyze._const_names(diff) == {"NEW_LIMIT"}


# --------------------------------------------------------------------------- #
# close_prs.py — compare-and-swap + idempotency
# --------------------------------------------------------------------------- #
def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "number": 101,
        "disposition": "DEFERRED_TO_BOARD_AND_CLOSED",
        "head_sha": "a" * 40,
        "main_sha": "b" * 40,
        "ahead": 3,
        "behind": 1,
        "changed_files": 2,
        "additions": 10,
        "deletions": 4,
        "merge_tree_conflicts": 0,
        "unresolved_threads": 1,
        "symbols_on_main": 1,
        "added_symbols": 5,
        "rationale": "needs a rebase",
        "evidence": ["docs/x.md"],
        "head_ref": "feat/x",
        "board_task": "pr-backlog-x",
        "resume_condition": "rebase onto main",
    }
    row.update(overrides)
    return row


def test_verify_live_head_accepts_an_open_matching_pr(close_prs: ModuleType) -> None:
    payload = {"state": "open", "head": {"sha": "a" * 40}}
    assert close_prs.verify_live_pr_head_matches_snapshot(1, "a" * 40, payload) is True


def test_verify_live_head_refuses_a_closed_pr(close_prs: ModuleType) -> None:
    payload = {"state": "closed", "head": {"sha": "a" * 40}}
    with pytest.raises(close_prs.StalePRSnapshotError):
        close_prs.verify_live_pr_head_matches_snapshot(1, "a" * 40, payload)


def test_verify_live_head_refuses_a_moved_head(close_prs: ModuleType) -> None:
    payload = {"state": "open", "head": {"sha": "c" * 40}}
    with pytest.raises(close_prs.StalePRSnapshotError):
        close_prs.verify_live_pr_head_matches_snapshot(1, "a" * 40, payload)


def test_verify_live_head_refuses_a_missing_snapshot_sha(close_prs: ModuleType) -> None:
    with pytest.raises(close_prs.StalePRSnapshotError):
        close_prs.verify_live_pr_head_matches_snapshot(1, "", {"state": "open", "head": {}})


def test_verify_live_head_refuses_a_payload_without_a_head(close_prs: ModuleType) -> None:
    with pytest.raises(close_prs.StalePRSnapshotError):
        close_prs.verify_live_pr_head_matches_snapshot(1, "a" * 40, {"state": "open"})


def test_verify_live_head_refuses_a_non_mapping_head(close_prs: ModuleType) -> None:
    payload: dict[str, Any] = {"state": "open", "head": "a" * 40}
    with pytest.raises(close_prs.StalePRSnapshotError):
        close_prs.verify_live_pr_head_matches_snapshot(1, "a" * 40, payload)


def test_has_existing_closure_comment_detects_the_marker(close_prs: ModuleType) -> None:
    comments = [{"body": "hello"}, {"body": f"done {close_prs.CLOSURE_MARKER}"}]
    assert close_prs.has_existing_closure_comment(comments) is True


def test_has_existing_closure_comment_is_false_without_the_marker(close_prs: ModuleType) -> None:
    assert close_prs.has_existing_closure_comment([{"body": "hello"}, {"body": None}]) is False
    assert close_prs.has_existing_closure_comment([]) is False


def test_closure_comment_carries_the_marker_and_the_audited_head(close_prs: ModuleType) -> None:
    text = close_prs.comment(_row())
    assert close_prs.CLOSURE_MARKER in text
    assert "a" * 40 in text
    assert "DEFERRED_TO_BOARD_AND_CLOSED" in text
    assert "rebase onto main" in text


def test_closure_comment_explains_supersession(close_prs: ModuleType) -> None:
    text = close_prs.comment(_row(disposition="SUPERSEDED_AND_CLOSED"))
    assert "already exists on canonical `main`" in text


def test_closure_comment_explains_extraction(close_prs: ModuleType) -> None:
    text = close_prs.comment(_row(disposition="EXTRACTED_AND_CLOSED", evidence=[]))
    assert "was extracted into the convergence branch" in text
    assert "**Evidence**" not in text


# --------------------------------------------------------------------------- #
# scripts/agent_board.py — zone-aware ownership fence
# --------------------------------------------------------------------------- #
def _live_claim(**overrides: Any) -> dict[str, Any]:
    claim: dict[str, Any] = {
        "task": "task-live",
        "status": "active_in_review",
        "zone": "zone-live",
        "agent_branch": "arena/live",
        "claimed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ttl_hours": 24,
        "gates_owner": False,
        "exclusive_paths": [],
    }
    claim.update(overrides)
    return claim


def _state(claims: list[dict[str, Any]], zones: list[dict[str, Any]]) -> dict[str, Any]:
    return {"claims": claims, "zones": zones}


def test_effective_paths_prefers_explicit_paths(board: ModuleType) -> None:
    claim = _live_claim(exclusive_paths=["src/a/"])
    zones = [{"id": "zone-live", "paths": ["src/b/"]}]
    assert board._effective_paths(_state([claim], zones), claim) == ["src/a/"]


def test_effective_paths_falls_back_to_the_zone(board: ModuleType) -> None:
    claim = _live_claim()
    zones = [{"id": "zone-live", "paths": ["src/owned/"]}]
    assert board._effective_paths(_state([claim], zones), claim) == ["src/owned/"]


def test_effective_paths_is_empty_without_a_declared_zone(board: ModuleType) -> None:
    claim = _live_claim(zone="missing")
    assert board._effective_paths(_state([claim], []), claim) == []


def test_conflicting_paths_uses_the_zone_when_exclusive_paths_is_empty(
    board: ModuleType,
) -> None:
    claim = _live_claim()
    zones = [{"id": "zone-live", "paths": ["src/owned/"]}]
    state = _state([claim], zones)
    hits = board._conflicting_paths(state, "arena/mine", ["src/owned/service.py"])
    assert hits == [("task-live", "src/owned/service.py")]


def test_conflicting_paths_ignores_the_same_branch(board: ModuleType) -> None:
    claim = _live_claim(agent_branch="arena/mine")
    zones = [{"id": "zone-live", "paths": ["src/owned/"]}]
    assert board._conflicting_paths(_state([claim], zones), "arena/mine", ["src/owned/x.py"]) == []


def test_conflicting_paths_ignores_an_expired_lease(board: ModuleType) -> None:
    stale = _live_claim(
        claimed_at=(datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    zones = [{"id": "zone-live", "paths": ["src/owned/"]}]
    assert board._conflicting_paths(_state([stale], zones), "arena/mine", ["src/owned/x.py"]) == []


def test_conflicting_paths_skips_blank_entries(board: ModuleType) -> None:
    claim = _live_claim()
    zones = [{"id": "zone-live", "paths": ["src/owned/"]}]
    assert board._conflicting_paths(_state([claim], zones), "arena/mine", ["  "]) == []


def test_validate_zone_disjointness_reports_overlaps(board: ModuleType) -> None:
    zones = [
        {"id": "a", "paths": ["src/shared/"]},
        {"id": "b", "paths": ["src/shared/mod.py"]},
        {"id": "c", "paths": ["docs/"]},
    ]
    assert board.validate_zone_disjointness(zones) == [("a", "b", "src/shared/")]


def test_validate_zone_disjointness_tolerates_malformed_input(board: ModuleType) -> None:
    assert board.validate_zone_disjointness(None) == []
    malformed: list[Any] = ["junk", {"id": "x"}, {"id": "y", "paths": [None]}]
    assert board.validate_zone_disjointness(malformed) == []


def test_path_matches_directory_and_exact_file(board: ModuleType) -> None:
    assert board._path_matches("src/a/", "src/a/b.py") is True
    assert board._path_matches("src/a/", "src/a") is True
    assert board._path_matches("src/a/b.py", "src/a/b.py") is True
    assert board._path_matches("src/a/", "src/ab/b.py") is False


def _claim_args(task: str, branch: str) -> Any:
    return type(
        "A",
        (),
        {"task": task, "branch": branch, "ttl": 24, "gates": False, "expected_generation": None},
    )()


def _patch_board_io(
    board: ModuleType, monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]
) -> dict[str, Any]:
    saved: dict[str, Any] = {}
    monkeypatch.setattr(board, "load_board", lambda: state)
    monkeypatch.setattr(board, "save_board", lambda payload: saved.update(board=payload))
    monkeypatch.setattr(board, "board_lock", contextlib.nullcontext)
    return saved


def test_claim_refuses_a_lease_inside_another_live_zone(
    board: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _state(
        [_live_claim(), _live_claim(task="task-new", status="queued", agent_branch="")],
        [{"id": "zone-live", "paths": ["src/owned/"]}],
    )
    saved = _patch_board_io(board, monkeypatch, state)
    assert board.cmd_claim(_claim_args("task-new", "arena/new")) == 2
    assert "would fence path(s) another live claim owns" in capsys.readouterr().out
    assert saved == {}, "a refused claim must not be persisted"


def test_claim_allows_a_lease_in_a_disjoint_zone(
    board: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(
        [
            _live_claim(),
            _live_claim(
                task="task-new",
                status="queued",
                agent_branch="",
                zone="zone-free",
                exclusive_paths=["src/free/"],
            ),
        ],
        [
            {"id": "zone-live", "paths": ["src/owned/"]},
            {"id": "zone-free", "paths": ["src/free/"]},
        ],
    )
    saved = _patch_board_io(board, monkeypatch, state)
    assert board.cmd_claim(_claim_args("task-new", "arena/new")) == 0
    assert saved["board"]["claims"][1]["status"] == "active"


def test_tools_are_not_exempt_from_line_length() -> None:
    """The per-file ``E501`` waiver must stay deleted (audit finding)."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "pr-convergence-2026-11-27/tools" not in text


def test_analyze_exposes_contract_parity_helper() -> None:
    source = (TOOLS / "analyze.py").read_text(encoding="utf-8")
    assert "def verify_superseded_contract_parity(" in source
    assert "qualified_contract_coverage" in source


def test_close_prs_exposes_the_cas_helpers() -> None:
    source = (TOOLS / "close_prs.py").read_text(encoding="utf-8")
    assert "def verify_live_pr_head_matches_snapshot(" in source
    assert "def has_existing_closure_comment(" in source
    assert "nexus-convergence:2026-11-27:closure" in source


def test_json_round_trip_of_a_closure_row_is_stable() -> None:
    assert json.loads(json.dumps(_row()))["head_sha"] == "a" * 40
