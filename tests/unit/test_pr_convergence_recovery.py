"""Crash-recovery and safety tests for the convergence tooling.

The first convergence run posted 43 comments and closed 43 PRs with no durable
record of *how far* it had got. A run killed between the comment and the close
could only be reasoned about afterwards. These tests pin the replacement
contract:

* ``close_prs.py`` is a **dry run unless ``--apply`` is passed**, and a dry run
  makes zero mutating calls;
* every closure walks ``PREPARED -> COMMENTED -> CLOSED -> VERIFIED ->
  RECORDED`` with an atomic journal write per step, so a killed run resumes
  instead of re-commenting or skipping the record;
* the compare-and-swap on the audited head survives the rewrite;
* ``board_reconcile.py`` emits complete, non-blocking path metadata;
* ``analyze.py`` labels its comparison as *structural signature* parity, which
  is all an AST comparison can prove.

Nothing here touches the network or the real board: the API is replaced by a
recording fake and every path points into ``tmp_path``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / ".agents" / "evidence" / "pr-convergence-2026-11-27" / "tools"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def close_prs() -> ModuleType:
    os.environ.setdefault("GITHUB_PERSONAL_ACCESS_TOKEN", "unit-test-token")
    return _load("conv_close_prs_recovery_under_test", TOOLS / "close_prs.py")


@pytest.fixture(scope="module")
def analyze() -> ModuleType:
    return _load("conv_analyze_parity_under_test", TOOLS / "analyze.py")


@pytest.fixture(scope="module")
def board_reconcile() -> ModuleType:
    return _load("conv_board_reconcile_under_test", TOOLS / "board_reconcile.py")


HEAD = "a" * 40


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "number": 101,
        "disposition": "DEFERRED_TO_BOARD_AND_CLOSED",
        "head_sha": HEAD,
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


class FakeGitHub:
    """A recording stand-in for ``api``: no network, every call inspectable."""

    def __init__(
        self,
        *,
        state: str = "open",
        head: str = HEAD,
        comments: list[dict[str, Any]] | None = None,
        closes: bool = True,
    ) -> None:
        self.state = state
        self.head = head
        self.comments = list(comments or [])
        self.closes = closes
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self, path: str, method: str = "GET", payload: dict[str, Any] | None = None
    ) -> Any:
        self.calls.append((method, path))
        if path.startswith("/pulls/") and method == "GET":
            return {"state": self.state, "head": {"sha": self.head}}
        if path.startswith("/pulls/") and method == "PATCH":
            if self.closes:
                self.state = "closed"
            return {"state": self.state}
        if path.startswith("/issues/") and path.endswith("/comments?per_page=100"):
            return list(self.comments)
        if path.startswith("/issues/") and method == "POST":
            body = (payload or {}).get("body", "")
            comment = {"body": body, "html_url": "https://example.test/c/1"}
            self.comments.append(comment)
            return comment
        raise AssertionError(f"unexpected call {method} {path}")

    def mutations(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] in {"POST", "PATCH", "DELETE"}]

    def posts(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] == "POST"]

    def patches(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] == "PATCH"]


# ---------------------------------------------------------------------------
# Safe by default
# ---------------------------------------------------------------------------
def _matrix_file(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps({"records": rows}), encoding="utf-8")
    return path


def test_no_flags_means_a_dry_run_with_zero_mutating_calls(
    close_prs: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    matrix = _matrix_file(tmp_path / "matrix.json", [_row()])
    github = FakeGitHub()
    monkeypatch.setattr(close_prs, "api", github)

    exit_code = close_prs.main(
        [
            "--matrix",
            str(matrix),
            "--result",
            str(tmp_path / "result.json"),
            "--journal",
            str(tmp_path / "journal.json"),
        ]
    )

    assert exit_code == 0
    assert github.mutations() == [], "a bare invocation must not mutate anything"
    assert github.calls == [], "a dry run should not even read from the API"
    assert close_prs.CLOSURE_MARKER in capsys.readouterr().out
    assert not (tmp_path / "journal.json").exists(), "a dry run writes no journal"


def test_dry_run_wins_over_apply(
    close_prs: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix = _matrix_file(tmp_path / "matrix.json", [_row()])
    github = FakeGitHub()
    monkeypatch.setattr(close_prs, "api", github)
    exit_code = close_prs.main(
        [
            "--apply",
            "--dry-run",
            "--matrix",
            str(matrix),
            "--result",
            str(tmp_path / "result.json"),
            "--journal",
            str(tmp_path / "journal.json"),
        ]
    )
    assert exit_code == 0
    assert github.mutations() == []


def test_apply_comments_then_closes_then_records(
    close_prs: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix = _matrix_file(tmp_path / "matrix.json", [_row()])
    result = tmp_path / "result.json"
    journal = tmp_path / "journal.json"
    github = FakeGitHub()
    monkeypatch.setattr(close_prs, "api", github)

    assert (
        close_prs.main(
            [
                "--apply",
                "--matrix",
                str(matrix),
                "--result",
                str(result),
                "--journal",
                str(journal),
            ]
        )
        == 0
    )

    assert len(github.posts()) == 1
    assert len(github.patches()) == 1
    assert json.loads(result.read_text(encoding="utf-8"))[0]["number"] == 101
    state = json.loads(journal.read_text(encoding="utf-8"))["records"]["101"]["state"]
    assert state == "RECORDED"


def test_merges_and_owner_blocked_prs_are_never_touched(
    close_prs: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        _row(number=1, disposition="MERGED"),
        _row(number=2, disposition="MERGE_CANDIDATE"),
        _row(number=3, disposition="BLOCKED_ACTIVE_OWNER"),
    ]
    matrix = _matrix_file(tmp_path / "matrix.json", rows)
    github = FakeGitHub()
    monkeypatch.setattr(close_prs, "api", github)
    close_prs.main(["--apply", "--matrix", str(matrix), "--journal", str(tmp_path / "j.json")])
    assert github.mutations() == []


# ---------------------------------------------------------------------------
# The journal state machine
# ---------------------------------------------------------------------------
def test_journal_enforces_the_transition_order(close_prs: ModuleType, tmp_path: Path) -> None:
    journal = close_prs.ClosureJournal(tmp_path / "journal.json")
    journal.transition(7, close_prs.PREPARED)
    assert journal.state(7) == "PREPARED"
    with pytest.raises(close_prs.JournalStateError):
        journal.transition(7, close_prs.CLOSED), "skipping COMMENTED must be refused"
    journal.transition(7, close_prs.COMMENTED)
    journal.transition(7, close_prs.CLOSED)
    assert journal.reached(7, close_prs.COMMENTED) is True
    assert journal.reached(7, close_prs.VERIFIED) is False
    with pytest.raises(close_prs.JournalStateError):
        journal.transition(7, "NOT_A_STATE")


def test_journal_reentry_is_idempotent_and_survives_a_reload(
    close_prs: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "journal.json"
    journal = close_prs.ClosureJournal(path)
    journal.transition(9, close_prs.PREPARED)
    journal.transition(9, close_prs.PREPARED)  # replay of the same step
    reloaded = close_prs.ClosureJournal(path)
    assert reloaded.state(9) == "PREPARED"
    assert sorted(json.loads(path.read_text(encoding="utf-8"))["records"]) == ["9"]


def test_journal_writes_are_atomic(close_prs: ModuleType, tmp_path: Path) -> None:
    """No temp file survives, and the document is complete JSON after each write."""
    path = tmp_path / "journal.json"
    journal = close_prs.ClosureJournal(path)
    journal.transition(1, close_prs.PREPARED)
    journal.transition(1, close_prs.COMMENTED, comment_url="https://example.test/c/1")
    assert [p.name for p in tmp_path.iterdir()] == ["journal.json"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["records"]["1"]["comment_url"] == "https://example.test/c/1"
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_a_torn_journal_is_not_silently_accepted(close_prs: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "journal.json"
    path.write_text('{"version": 1, "records": {"1": ', encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        close_prs.ClosureJournal(path)


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------
def test_a_crash_after_commenting_does_not_comment_twice(
    close_prs: ModuleType, tmp_path: Path
) -> None:
    """State COMMENTED with the PR still open: close it, never re-post."""
    journal = close_prs.ClosureJournal(tmp_path / "journal.json")
    journal.transition(101, close_prs.PREPARED)
    journal.transition(101, close_prs.COMMENTED, comment_url="https://example.test/c/1")

    github = FakeGitHub(state="open")
    result = tmp_path / "result.json"
    outcome = close_prs.close_pr(_row(), journal=journal, call=github, result_path=result)

    assert outcome == "closed"
    assert github.posts() == [], "the comment is already durable"
    assert len(github.patches()) == 1
    assert journal.state(101) == "RECORDED"
    assert json.loads(result.read_text(encoding="utf-8"))[0]["comment_url"] == (
        "https://example.test/c/1"
    )


def test_a_crash_after_closing_still_records_the_result(
    close_prs: ModuleType, tmp_path: Path
) -> None:
    """State CLOSED with the PR already closed: verify and record, mutate nothing."""
    journal = close_prs.ClosureJournal(tmp_path / "journal.json")
    for state in (close_prs.PREPARED, close_prs.COMMENTED, close_prs.CLOSED):
        journal.transition(101, state)

    github = FakeGitHub(state="closed")
    result = tmp_path / "result.json"
    outcome = close_prs.close_pr(_row(), journal=journal, call=github, result_path=result)

    assert outcome == "closed"
    assert github.mutations() == []
    assert journal.state(101) == "RECORDED"
    assert json.loads(result.read_text(encoding="utf-8"))[0]["number"] == 101


def test_a_crash_after_verifying_does_not_double_append_the_result(
    close_prs: ModuleType, tmp_path: Path
) -> None:
    journal = close_prs.ClosureJournal(tmp_path / "journal.json")
    for state in (close_prs.PREPARED, close_prs.COMMENTED, close_prs.CLOSED, close_prs.VERIFIED):
        journal.transition(101, state)
    result = tmp_path / "result.json"
    result.write_text(json.dumps([{"number": 101, "disposition": "x"}]), encoding="utf-8")

    github = FakeGitHub(state="closed")
    close_prs.close_pr(_row(), journal=journal, call=github, result_path=result)

    assert len(json.loads(result.read_text(encoding="utf-8"))) == 1
    assert journal.state(101) == "RECORDED"


def test_a_recorded_pr_is_never_touched_again(close_prs: ModuleType, tmp_path: Path) -> None:
    journal = close_prs.ClosureJournal(tmp_path / "journal.json")
    for state in close_prs.STATES:
        journal.transition(101, state)
    github = FakeGitHub()
    assert (
        close_prs.close_pr(
            _row(), journal=journal, call=github, result_path=tmp_path / "result.json"
        )
        == "already-recorded"
    )
    assert github.calls == []


def test_a_replayed_apply_run_is_a_no_op(
    close_prs: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix = _matrix_file(tmp_path / "matrix.json", [_row()])
    arguments = [
        "--apply",
        "--matrix",
        str(matrix),
        "--result",
        str(tmp_path / "result.json"),
        "--journal",
        str(tmp_path / "journal.json"),
    ]
    first = FakeGitHub()
    monkeypatch.setattr(close_prs, "api", first)
    assert close_prs.main(arguments) == 0
    assert len(first.mutations()) == 2

    second = FakeGitHub(state="closed", comments=first.comments)
    monkeypatch.setattr(close_prs, "api", second)
    assert close_prs.main(arguments) == 0
    assert second.mutations() == [], "a completed closure must not repeat itself"


# ---------------------------------------------------------------------------
# Compare-and-swap still fails closed
# ---------------------------------------------------------------------------
def test_a_moved_head_is_refused_and_nothing_is_mutated(
    close_prs: ModuleType, tmp_path: Path
) -> None:
    journal = close_prs.ClosureJournal(tmp_path / "journal.json")
    github = FakeGitHub(head="c" * 40)
    with pytest.raises(close_prs.StalePRSnapshotError):
        close_prs.close_pr(
            _row(), journal=journal, call=github, result_path=tmp_path / "result.json"
        )
    assert github.mutations() == []
    assert journal.state(101) == "PREPARED", "the attempt is durably recorded"
    assert not (tmp_path / "result.json").exists()


def test_a_close_that_did_not_take_effect_is_not_recorded(
    close_prs: ModuleType, tmp_path: Path
) -> None:
    journal = close_prs.ClosureJournal(tmp_path / "journal.json")
    github = FakeGitHub(closes=False)
    with pytest.raises(close_prs.StalePRSnapshotError):
        close_prs.close_pr(
            _row(), journal=journal, call=github, result_path=tmp_path / "result.json"
        )
    assert journal.state(101) == "CLOSED", "the resume point stays where the work stopped"
    assert not (tmp_path / "result.json").exists()


def test_a_legacy_closure_with_the_marker_and_a_closed_pr_is_adopted(
    close_prs: ModuleType, tmp_path: Path
) -> None:
    """The 43 journal-less closures of the first run must not block a re-run."""
    journal = close_prs.ClosureJournal(tmp_path / "journal.json")
    github = FakeGitHub(state="closed", comments=[{"body": f"done {close_prs.CLOSURE_MARKER}"}])
    result = tmp_path / "result.json"
    outcome = close_prs.close_pr(_row(), journal=journal, call=github, result_path=result)
    assert outcome == "closed"
    assert github.mutations() == []
    assert journal.state(101) == "RECORDED"
    assert json.loads(result.read_text(encoding="utf-8"))[0]["number"] == 101


def test_a_closed_pr_without_the_marker_is_still_refused(
    close_prs: ModuleType, tmp_path: Path
) -> None:
    """Closed by someone else, with no comment of ours: refuse rather than adopt."""
    journal = close_prs.ClosureJournal(tmp_path / "journal.json")
    github = FakeGitHub(state="closed", comments=[{"body": "closed by hand"}])
    with pytest.raises(close_prs.StalePRSnapshotError):
        close_prs.close_pr(
            _row(), journal=journal, call=github, result_path=tmp_path / "result.json"
        )


def test_previous_results_are_adopted_without_reposting(
    close_prs: ModuleType, tmp_path: Path
) -> None:
    results = tmp_path / "result.json"
    results.write_text(
        json.dumps([{"number": 101, "disposition": "x", "comment_url": "u"}], indent=2),
        encoding="utf-8",
    )
    journal = close_prs.ClosureJournal(tmp_path / "journal.json")
    adopted = close_prs._adopt_previous_closures(journal, results, [_row()])
    assert adopted == 1
    assert journal.state(101) == "RECORDED"
    assert journal.comment_url(101) == "u"
    assert close_prs._adopt_previous_closures(journal, results, [_row()]) == 0


# ---------------------------------------------------------------------------
# board_reconcile.py — complete, non-blocking path metadata
# ---------------------------------------------------------------------------
def test_zone_prefixes_keeps_every_specific_directory(
    board_reconcile: ModuleType,
) -> None:
    paths = [f"src/nexus_ai_agent/pkg{i}/mod.py" for i in range(40)]
    prefixes = board_reconcile.zone_prefixes(paths)
    assert len(prefixes) == 40, "40 distinct directories must survive; no [:24] truncation"
    assert prefixes == sorted(prefixes)


def test_zone_prefixes_never_emits_a_broad_root(board_reconcile: ModuleType) -> None:
    paths = [
        "src/nexus_ai_agent/api/studio_ibpc.py",
        "src/nexus_ai_agent/web/studio_surface/studio.js",
        "src/nexus_ai_agent/cli.py",
    ]
    prefixes = board_reconcile.zone_prefixes(paths)
    assert "src/nexus_ai_agent/" not in prefixes
    assert "src/" not in prefixes
    assert "src/nexus_ai_agent/api/" in prefixes
    assert "src/nexus_ai_agent/cli.py" in prefixes, "a root-level file is named exactly"


def test_zone_prefixes_is_empty_for_no_paths(board_reconcile: ModuleType) -> None:
    assert board_reconcile.zone_prefixes([]) == []


def test_zone_prefixes_output_does_not_overlap_another_zone(
    board_reconcile: ModuleType,
) -> None:
    """The referee's overlap primitive must not see two families collide."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import agent_board

    left = board_reconcile.zone_prefixes(["src/nexus_ai_agent/api/x.py"])
    right = board_reconcile.zone_prefixes(["tests/unit/test_x.py"])
    for a in left:
        for b in right:
            assert not agent_board._paths_overlap(a, b)


# ---------------------------------------------------------------------------
# analyze.py — the parity label says what it measures
# ---------------------------------------------------------------------------
def test_parity_is_labelled_structural_signature_parity(analyze: ModuleType) -> None:
    assert analyze.PARITY_METHOD == "structural_signature_parity"
    assert analyze.PARITY_METHOD_LABEL == "Structural API Signature Parity"
    record = {"number": 4, "added_contracts": [["m.py", "f", ["a"]]]}
    result = analyze.verify_superseded_contract_parity(record, {("m.py", "f", ("a",))})
    assert result["method"] == "structural_signature_parity"
    assert result["structural_signature_parity"] is True
    assert result["parity"] is True, "the older key stays as a mirror"


def test_structural_parity_is_false_for_a_missing_contract(analyze: ModuleType) -> None:
    record = {"number": 4, "added_contracts": [["m.py", "f", ["a"]]]}
    result = analyze.verify_superseded_contract_parity(record, set())
    assert result["structural_signature_parity"] is False
    assert result["parity"] is False


def test_a_prepared_pr_that_already_carries_the_closure_comment_is_not_commented_again(
    close_prs: ModuleType, tmp_path: Path
) -> None:
    """A run that crashed *between* posting and journaling must not double-post.

    State PREPARED means "nothing durable happened yet" — but the comment on
    GitHub is the durable part, and a crash in the gap is exactly the window the
    journal cannot see. The live comment list is therefore consulted before any
    POST, and finding the marker is proof, not a guess.
    """
    journal = close_prs.ClosureJournal(tmp_path / "journal.json")
    journal.transition(101, close_prs.PREPARED)

    github = FakeGitHub(
        state="open",
        comments=[{"body": f"… {close_prs.CLOSURE_MARKER} …", "html_url": "https://x/old"}],
    )
    result = tmp_path / "result.json"
    outcome = close_prs.close_pr(_row(), journal=journal, call=github, result_path=result)

    assert outcome == "closed"
    assert github.posts() == [], "the marker proves a comment already exists"
    assert len(github.patches()) == 1
    assert journal.state(101) == "RECORDED"
