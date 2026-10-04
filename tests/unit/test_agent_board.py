"""The coordination board and its CLI are infrastructure — so they are tested.

Board schema 2 (``docs/architecture/adr/0004-board-schema-2.md``) added structure
to ``.agents/board.json`` while keeping the field names the zero-dependency CLI
reads. These tests pin both halves:

* **schema integrity** — unique task ids, legal statuses, leases that carry an
  owner and a timestamp, exclusive paths that belong to a declared zone, no two
  active claims owning the same path, prerequisites that exist, and queued work
  that carries acceptance criteria (a task without them cannot be verified);
* **CLI behaviour** — ``claim`` refuses a foreign active lease and renews its own,
  ``check`` detects overlap and allows disjoint work, ``release`` frees the zone,
  ``defer`` records the bilingual note, and ``show`` garbage-collects an expired
  lease.

The CLI is loaded from ``scripts/agent_board.py`` by path (stdlib only) and
pointed at a temporary board copy, so the repository's real board is never
mutated by a test run.  The copy always carries one synthetic active
directory-scoped lease (``board_module``), so these tests prove lease *semantics*
without depending on whether the live board currently holds any active lease —
leases come and go as work is claimed and released, and a test that needs
"somewhere there is an active lease" on the real board is a time bomb (the
same fragility class as the 2026-09-22 clock-bomb incident).

Because lease liveness is wall-clock arithmetic, every test that acts on a real
lease pins ``agent_board._now`` inside that lease
(:func:`_pin_clock_inside_lease`) instead of trusting the day the suite runs.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).parents[2]
BOARD_PATH = REPO_ROOT / ".agents" / "board.json"
SCRIPT = REPO_ROOT / "scripts" / "agent_board.py"


@pytest.fixture(autouse=True)
def _hermetic_pr_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub CI's GitHub env so no unit test can reach the network.

    GitHub Actions exports ``GITHUB_REPOSITORY``/``GITHUB_TOKEN`` on every
    runner; without scrubbing them, ``cmd_check`` would try to enumerate open-PR
    branches over the network inside a unit test (the same leak class that once
    turned ``praudit`` red). The explicit ``--repo``/``--token`` arguments stay
    available for the tests that exercise the new source directly.
    """
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)


LEGAL_STATUSES = {
    "queued",
    "active",
    "done",
    "expired",
    "deferred",
    "available",
    "completed_released",
    "completed_merged",
    "completed_delivered_via_PR34",
    "active_in_review",
    "superseded_by_PR33",
    "assigned_to_B",
    "assigned_to_B_next",
    "assigned_to_E_pr33",
    "available_sequenced_post_33",
    "available_sequenced_post_32",
    "available_sequenced_post_32_33",
}


def _load_board_cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agent_board_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _synthetic_active_lease() -> dict:
    """An active, directory-scoped lease owned by a branch no real session uses."""
    return {
        "task": "synthetic-dir-claim",
        "status": "active",
        "zone": "synthetic",
        "agent_branch": "arena/111-someone-else",
        "claimed_at": "2026-01-01T00:00:00Z",
        "ttl_hours": 24,
        "gates_owner": False,
        "exclusive_paths": ["src/synthetic/"],
    }


@pytest.fixture()
def board_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """CLI module pointed at a tmp board copy that always carries a known lease.

    The copy mirrors the real board, but one synthetic active directory-scoped
    lease is always inserted first: the CLI-behaviour tests exercise *lease
    semantics* (foreign refusal, own-branch renewal, overlap, GC), and those
    must not depend on whether the live board happens to hold any active lease
    — leases come and go as work is claimed and released, so a test that
    requires "some active lease exists" on the real board is a time bomb (the
    same fragility class as the 2026-09-22 clock-bomb incident).
    """
    module = _load_board_cli()
    board = json.loads(BOARD_PATH.read_text(encoding="utf-8"))
    board["claims"].insert(0, _synthetic_active_lease())
    if not any(zone["id"] == "synthetic" for zone in board["zones"]):
        board["zones"].append({"id": "synthetic", "paths": ["src/synthetic/"]})
    board_copy = tmp_path / "board.json"
    board_copy.write_text(json.dumps(board, indent=2, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(module, "BOARD", board_copy)
    return module


@pytest.fixture()
def board() -> dict:
    return json.loads(BOARD_PATH.read_text(encoding="utf-8"))


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _first_active_claim(board: dict) -> dict:
    """The lease the CLI tests exercise: the board's first *path-fencing* active claim."""
    return next(
        claim
        for claim in board["claims"]
        if claim["status"] == "active" and claim.get("exclusive_paths")
    )


def _pin_clock_inside_lease(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, claim: dict
) -> None:
    """Freeze ``agent_board._now`` inside *claim*'s lease window.

    Lease liveness is wall-clock arithmetic (``claimed_at + ttl_hours``), so a
    test that reads the repository's real board is a time bomb: 24 h after that
    board was last rehearsed every ``active`` lease is expired and three
    assertions below invert (overlap detected → none; foreign claim refused →
    accepted; heartbeat keeps its TTL → the ``--ttl`` flag wins).  That is not a
    hypothetical — it is the red CI of 2026-09-22, where the first red run was
    simply the first push after the clock crossed the fence.

    Pinning the clock one second after the claim's own ``claimed_at`` makes the
    lease live *by construction*, whatever today's date is, so these tests
    report on the CLI instead of on the calendar.  Same hermetic pattern as
    ``test_agent_board_pr_visibility.py`` and
    ``test_agent_board_active_in_review.py`` (task-151).
    """
    claimed_at = datetime.strptime(claim["claimed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    monkeypatch.setattr(module, "_now", lambda: claimed_at + timedelta(seconds=1))


# --------------------------------------------------------------------------- #
# schema integrity
# --------------------------------------------------------------------------- #
def test_top_level_shape_is_schema_2(board: dict) -> None:
    assert board["schema"] == 2
    assert board["protocol"]["protocol_version"] == 2
    for key in ("claims", "zones", "deferred_log", "next_work", "history", "protocol"):
        assert key in board, f"schema 2 requires a top-level {key!r} block"


def test_task_ids_are_unique(board: dict) -> None:
    ids = [claim["task"] for claim in board["claims"]]
    duplicates = sorted({task for task in ids if ids.count(task) > 1})
    assert not duplicates, f"duplicate task ids on the board: {duplicates}"


def test_statuses_are_legal(board: dict) -> None:
    unknown = sorted(
        {claim["status"] for claim in board["claims"] if claim["status"] not in LEGAL_STATUSES}
    )
    assert not unknown, f"unknown claim statuses: {unknown} (add them to LEGAL_STATUSES)"


def test_active_claims_carry_owner_timestamp_and_zone(board: dict) -> None:
    zone_ids = {zone["id"] for zone in board["zones"]}
    for claim in board["claims"]:
        assert claim["zone"] in zone_ids, f"{claim['task']} references unknown zone {claim['zone']}"
        if claim["status"] != "active":
            continue
        assert claim.get("agent_branch"), f"active claim {claim['task']} has no owner branch"
        assert claim.get("claimed_at"), f"active claim {claim['task']} has no lease timestamp"
        assert int(claim.get("ttl_hours", 0)) > 0, f"active claim {claim['task']} has no TTL"
        assert claim.get("exclusive_paths") is not None
        datetime.strptime(claim["claimed_at"], "%Y-%m-%dT%H:%M:%SZ")


def test_exclusive_paths_belong_to_a_declared_zone(board: dict) -> None:
    zones = {zone["id"]: zone.get("paths", []) for zone in board["zones"]}
    problems: list[str] = []
    for claim in board["claims"]:
        zone_paths = zones[claim["zone"]]
        for path in claim.get("exclusive_paths") or []:
            if not any(
                path == zpath or path.startswith(zpath) or zpath.startswith(path)
                for zpath in zone_paths
            ):
                problems.append(f"{claim['task']}: {path} is outside zone {claim['zone']}")
    assert not problems, "exclusive paths outside their zone:\n" + "\n".join(problems)


def test_no_two_active_claims_own_the_same_path(board: dict) -> None:
    owned: dict[str, str] = {}
    clashes: list[str] = []
    for claim in board["claims"]:
        if claim["status"] != "active":
            continue
        for path in claim.get("exclusive_paths") or []:
            for existing, owner in owned.items():
                if path == existing or path.startswith(existing) or existing.startswith(path):
                    clashes.append(f"{owner} and {claim['task']} both own {path}")
            owned[path] = claim["task"]
    assert not clashes, "overlapping active leases:\n" + "\n".join(clashes)


TERMINAL_STATUSES = frozenset(
    {
        "done",
        "completed_released",
        "completed_merged",
        "completed_delivered_via_PR34",
        "superseded_by_PR33",
    }
)


def test_terminal_claims_release_owner_and_gate(board: dict) -> None:
    """A finished claim must not keep a live-looking owner or the single gate.

    ``done`` + ``agent_branch`` set (or ``done`` + ``gates_owner: true``) is a
    governance bug, not cosmetics: the gate is supposed to have exactly one
    holder, so a finished claim silently holding it misleads every reader. The
    evidence (which branch did the work, when) lives in history, not in the live
    owner field.
    """
    grandfathered_terminal = {
        "task-131",
        "task-111-version-lockstep-ci",
        "ci-gates-steward",
        "task-132",
        "pr34-merged",
        "task-110-duplicate-F",
        "nagar-wave3-timeline-edit-delivery",
        "board-gc-engineered-handoff",
        "task-101",
        "task-104",
        "task-105",
        "task-113-ci-lint-rail",
        "pr39-in-review-docs",
        "task-142-delivery-signing-coverage-95",
        "task-151-board-test-clock-time-bomb",
        "task-157-pr51-ci-repair",
        "task-165-legacy-creative-hardening",
        "task-166-creative-surface-wiring",
        "task-167-backup-verifiability",
        "task-179-gate2-command-reconciliation",
        "task-178-job-lifecycle",
        "task-180-verification-gap-closure",
        "task-195-llm-queue-lifecycle",
    }
    offenders: list[str] = []
    for claim in board["claims"]:
        if claim["status"] not in TERMINAL_STATUSES or claim["task"] in grandfathered_terminal:
            continue
        if claim.get("agent_branch") or claim.get("claimed_at") or claim.get("gates_owner"):
            offenders.append(
                f"{claim['task']}: branch={claim.get('agent_branch')!r} "
                f"claimed_at={claim.get('claimed_at')!r} gates_owner={claim.get('gates_owner')}"
            )
    assert not offenders, "terminal claims still hold owner/gate state:\n" + "\n".join(offenders)


def test_prerequisites_reference_existing_tasks(board: dict) -> None:
    ids = {claim["task"] for claim in board["claims"]}
    referenced = {
        prereq
        for claim in board["claims"]
        for prereq in (claim.get("prerequisites") or [])
        if prereq and prereq != "PR#33"
    }
    missing = sorted(referenced - ids)
    assert not missing, f"prerequisites reference unknown tasks: {missing}"


def test_open_work_declares_acceptance_criteria(board: dict) -> None:
    """Anything claimable must say how a reviewer decides it is done."""
    open_statuses = {
        "queued",
        "available",
        "available_sequenced_post_33",
        "available_sequenced_post_32",
        "available_sequenced_post_32_33",
        "expired",
        "deferred",
    }
    offenders = [
        claim["task"]
        for claim in board["claims"]
        if claim["status"] in open_statuses and not claim.get("acceptance_criteria")
    ]
    assert not offenders, f"claimable tasks without acceptance criteria: {offenders}"


def test_deferred_log_references_real_tasks(board: dict) -> None:
    ids = {claim["task"] for claim in board["claims"]}
    unknown = sorted({entry["task"] for entry in board["deferred_log"] if entry["task"] not in ids})
    assert not unknown, f"deferred_log references unknown tasks: {unknown}"


def test_next_work_network_is_structured(board: dict) -> None:
    entries = board["next_work"]
    assert len(entries) == 10, "the protocol requires exactly ten forward tasks"
    for entry in entries:
        assert entry.get("priority"), f"{entry.get('id')} has no priority"
        assert entry.get("zone"), f"{entry.get('id')} has no zone"
        assert entry.get("acceptance_criteria"), f"{entry.get('id')} has no acceptance criteria"
        assert entry.get("title_fa") or entry.get("title"), f"{entry.get('id')} has no title"


def test_history_records_closed_work_without_claim_semantics(board: dict) -> None:
    assert board["history"]["waves_closed"], "closed waves must stay recorded"
    assert board["history"]["pull_requests"], "merged PRs must stay recorded"
    for entry in board["history"]["pull_requests"]:
        assert entry.get("pr"), "a history PR entry without a number"
        assert entry.get("state") in {"MERGED", "CLOSED", "OPEN"}


# --------------------------------------------------------------------------- #
# CLI behaviour
# --------------------------------------------------------------------------- #
def _first_claimable(board_path: Path) -> str:
    """A claimable task the CLI will actually accept.

    ``claim`` now refuses a task without ``evidence_required`` (AGENTS.md §2), so
    the helper selects one that satisfies that precondition — otherwise the
    claim-semantics tests would fail for a governance reason unrelated to the
    behaviour they pin.
    """
    data = json.loads(board_path.read_text(encoding="utf-8"))
    return next(
        claim["task"]
        for claim in data["claims"]
        if claim["status"] in {"queued", "available", "expired", "deferred"}
        and claim.get("evidence_required")
        and claim.get("exclusive_paths")
    )


def _synthetic_directory_claim_board() -> dict:
    """A minimal board with one active, directory-scoped lease.

    The overlap check must be proven against *known* fence shape, not against
    whatever the first live claim on the real board happens to be: when that
    claim fences a single file (e.g. ``Dockerfile``), ``path + "/intruder.py"``
    is legitimately outside the fence and the check must return 0.  The
    fixture writes into the ``board_module.BOARD`` tmp copy, so the real board
    is never touched (same hermeticity as ``_pin_clock_inside_lease``).
    """
    return {
        "schema": 2,
        "protocol": {"protocol_version": 2},
        "claims": [
            {
                "task": "synthetic-dir-claim",
                "status": "active",
                "zone": "synthetic",
                "agent_branch": "arena/111-someone-else",
                "claimed_at": "2026-01-01T00:00:00Z",
                "ttl_hours": 24,
                "gates_owner": False,
                "exclusive_paths": ["src/synthetic/"],
            }
        ],
        "zones": [{"id": "synthetic", "paths": ["src/synthetic/"]}],
        "deferred_log": [],
        "next_work": [],
        "history": {"waves_closed": [], "pull_requests": [], "incidents": []},
    }


def test_check_detects_overlap_and_allows_disjoint_work(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _synthetic_directory_claim_board()
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")
    _pin_clock_inside_lease(monkeypatch, board_module, board["claims"][0])
    # Local-only mode: the overlap semantics are exercised without touching the
    # network or other worktrees (multi-source behaviour is covered separately).
    assert (
        board_module.cmd_check(
            type(
                "A",
                (),
                {
                    "files": "src/synthetic/intruder.py",
                    "branch": "arena/999-other-agent",
                    "no_remote": True,
                },
            )()
        )
        == 1
    )
    assert (
        board_module.cmd_check(
            type(
                "A",
                (),
                {
                    "files": "docs/not-owned-by-anyone.md",
                    "branch": "arena/999-other",
                    "no_remote": True,
                },
            )()
        )
        == 0
    )


def test_check_unreadable_source_is_exit_2_never_a_pass(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source that cannot be read must be a loud exit 2, never a silent 0."""
    board = _synthetic_directory_claim_board()
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")
    _pin_clock_inside_lease(monkeypatch, board_module, board["claims"][0])

    def _no_git(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(board_module, "_git", _no_git)
    code = board_module.cmd_check(
        type(
            "A",
            (),
            {"files": "docs/not-owned.md", "branch": "arena/999-other", "no_remote": False},
        )()
    )
    assert code == 2, "an unreadable remote/worktree must not be reported as 'no overlap'"


def test_check_no_remote_narrows_the_claim_loudly(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """``--no-remote`` is a local verdict and must say so out loud."""
    board = _synthetic_directory_claim_board()
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")
    _pin_clock_inside_lease(monkeypatch, board_module, board["claims"][0])

    def _explode(*_a: object, **_k: object) -> None:
        raise AssertionError("--no-remote must not consult git")

    monkeypatch.setattr(board_module, "_git", _explode)
    code = board_module.cmd_check(
        type(
            "A",
            (),
            {"files": "docs/not-owned.md", "branch": "arena/999-other", "no_remote": True},
        )()
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "NOT a global pass" in out


def test_check_consults_sibling_worktree_boards(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A foreign lease that exists only in another worktree must still be seen."""
    board = _synthetic_directory_claim_board()
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")
    _pin_clock_inside_lease(monkeypatch, board_module, board["claims"][0])

    # Another worktree holds a lease on docs/other-agent.md.
    other_root = tmp_path / "other-worktree"
    (other_root / ".agents").mkdir(parents=True)
    other_board = _synthetic_directory_claim_board()
    other_board["claims"][0]["exclusive_paths"] = ["docs/other-agent.md"]
    other_board["claims"][0]["agent_branch"] = "arena/222-far-agent"
    (other_root / ".agents" / "board.json").write_text(json.dumps(other_board), encoding="utf-8")

    def _fake_git(args: list[str], cwd: Path) -> object:
        if args[:2] == ["worktree", "list"]:
            listing = f"worktree {board_module.BOARD.parents[1]}\nworktree {other_root}\n"
            return subprocess.CompletedProcess(args, 0, stdout=listing, stderr="")
        if args[:2] == ["fetch", "--quiet"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="")

    monkeypatch.setattr(board_module, "_git", _fake_git)
    monkeypatch.setattr(board_module, "_read_remote_board", lambda: (None, "test: remote offline"))
    # Isolate the worktree source: the open-PR branch source is readable-and-empty
    # so this test still proves the *worktree* conflict (not a blanket exit 2).
    monkeypatch.setattr(board_module, "_open_pr_branches", lambda repo, token: ([], ""))
    code = board_module.cmd_check(
        type(
            "A",
            (),
            {"files": "docs/other-agent.md", "branch": "arena/999-me", "no_remote": False},
        )()
    )
    # A proven conflict is exit 1 even though the remote was unreadable.
    assert code == 1


def test_check_unreliable_worktree_enumeration_is_exit_2(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If sibling worktrees cannot be enumerated, the absence of overlap is unproven."""
    board = _synthetic_directory_claim_board()
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")
    _pin_clock_inside_lease(monkeypatch, board_module, board["claims"][0])

    def _git_worktree_fails(args: list[str], cwd: Path) -> object:
        if args[:2] == ["worktree", "list"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="not a repo")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(board_module, "_git", _git_worktree_fails)
    # origin/main is readable and clean, but the worktree enumeration was not.
    monkeypatch.setattr(board_module, "_read_remote_board", lambda: ({"claims": []}, ""))
    # Isolate the worktree source from the open-PR branch source.
    monkeypatch.setattr(board_module, "_open_pr_branches", lambda repo, token: ([], ""))
    code = board_module.cmd_check(
        type(
            "A",
            (),
            {"files": "docs/not-owned.md", "branch": "arena/999-me", "no_remote": False},
        )()
    )
    assert code == 2, "unreliable worktree enumeration must not be a clean pass"


def test_check_proven_conflict_wins_over_unreadable_remote(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local board proves a conflict while origin/main is unreachable → exit 1."""
    board = _synthetic_directory_claim_board()
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")
    _pin_clock_inside_lease(monkeypatch, board_module, board["claims"][0])
    monkeypatch.setattr(board_module, "_git", lambda *a, **k: None)
    code = board_module.cmd_check(
        type(
            "A",
            (),
            {"files": "src/synthetic/intruder.py", "branch": "arena/999-other", "no_remote": False},
        )()
    )
    assert code == 1


def test_claim_refuses_a_foreign_active_lease(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    active = _first_active_claim(board)
    _pin_clock_inside_lease(monkeypatch, board_module, active)
    code = board_module.cmd_claim(
        type(
            "A",
            (),
            {"task": active["task"], "branch": "arena/999-other", "ttl": 24, "gates": False},
        )()
    )
    assert code == 2


def test_check_local_board_unreadable_is_exit_2_not_a_crash(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed local board must be exit 2 — never a traceback, never a pass."""
    board_module.BOARD.write_text("{ this is not json", encoding="utf-8")

    def _explode(*_a: object, **_k: object) -> None:
        raise AssertionError("an unreadable local board must short-circuit before git")

    monkeypatch.setattr(board_module, "_git", _explode)
    code = board_module.cmd_check(
        type(
            "A",
            (),
            {"files": "docs/anything.md", "branch": "arena/999-me", "no_remote": False},
        )()
    )
    assert code == 2, "an unparseable local board must be UNVERIFIABLE (2), not a crash"


def test_check_local_board_missing_is_exit_2(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(board_module, "BOARD", tmp_path / "does-not-exist.json")
    code = board_module.cmd_check(
        type(
            "A",
            (),
            {"files": "docs/anything.md", "branch": "arena/999-me", "no_remote": True},
        )()
    )
    assert code == 2


def test_check_malformed_claim_ttl_does_not_crash_the_referee(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poison claim (non-numeric ttl) must degrade to exit 2, never raise."""
    board = _synthetic_directory_claim_board()
    board["claims"].append(
        {
            "task": "poison-claim",
            "status": "active",
            "zone": "synthetic",
            "agent_branch": "arena/poison",
            "claimed_at": "2026-01-01T00:00:00Z",
            "ttl_hours": "not-a-number",
            "gates_owner": False,
            "exclusive_paths": ["docs/poison.md"],
        }
    )
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")
    _pin_clock_inside_lease(monkeypatch, board_module, board["claims"][0])
    code = board_module.cmd_check(
        type(
            "A",
            (),
            {"files": "docs/poison.md", "branch": "arena/999-me", "no_remote": True},
        )()
    )
    assert code == 2, "a malformed claim must be UNVERIFIABLE, never a silent 0 or a crash"


# --------------------------------------------------------------------------- #
# pushed open-PR branch source (task-207): a lease that lives only on a pushed
# branch — open PR, not in origin/main, not checked out — must still be seen.
# --------------------------------------------------------------------------- #
def _no_git(*_a: object, **_k: object) -> None:
    return None


def test_check_fails_closed_when_the_open_pr_list_is_unavailable(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No repo/token => the pushed-branch source is unreadable => exit 2, never 0.

    Everything else is readable and clean, so exit 2 can *only* come from the
    open-PR branch source: this is the false-clear guard itself.
    """
    board = _synthetic_directory_claim_board()
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")
    _pin_clock_inside_lease(monkeypatch, board_module, board["claims"][0])

    def _git_ok_worktree(args: list[str], cwd: Path) -> object:
        if args[:2] == ["worktree", "list"]:
            return subprocess.CompletedProcess(args, 0, stdout=f"worktree {REPO_ROOT}\n", stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="")

    monkeypatch.setattr(board_module, "_git", _git_ok_worktree)
    monkeypatch.setattr(board_module, "_read_remote_board", lambda: ({"claims": []}, ""))
    code = board_module.cmd_check(
        type(
            "A",
            (),
            {
                "files": "docs/not-owned.md",
                "branch": "arena/999-me",
                "no_remote": False,
                "repo": "",
                "token": "",
            },
        )()
    )
    assert code == 2, "an unreadable open-PR list must never be a clean pass"


def test_check_sees_a_lease_that_lives_only_on_a_pushed_branch(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PR-only lease is the false-clear this source exists to close."""
    board = _synthetic_directory_claim_board()
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")
    _pin_clock_inside_lease(monkeypatch, board_module, board["claims"][0])

    foreign_board = _synthetic_directory_claim_board()
    foreign_board["claims"][0]["exclusive_paths"] = ["src/pr_only_zone/"]
    foreign_board["claims"][0]["agent_branch"] = "arena/999-pr-only"

    def _fake_git(args: list[str], cwd: Path) -> object:
        if args[:2] == ["worktree", "list"]:
            return subprocess.CompletedProcess(args, 0, stdout=f"worktree {REPO_ROOT}\n", stderr="")
        if args[0] == "fetch":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[0] == "show" and "refs/remotes/origin/arena/999-pr-only" in args[1]:
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(foreign_board), stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="")

    monkeypatch.setattr(board_module, "_git", _fake_git)
    monkeypatch.setattr(board_module, "_read_remote_board", lambda: ({"claims": []}, ""))
    monkeypatch.setattr(
        board_module, "_open_pr_branches", lambda repo, token: (["arena/999-pr-only"], "")
    )
    code = board_module.cmd_check(
        type(
            "A",
            (),
            {
                "files": "src/pr_only_zone/engine.py",
                "branch": "arena/999-me",
                "no_remote": False,
                "repo": "o/r",
                "token": "t",
            },
        )()
    )
    # Exit 1 (not 2): every other source is readable, so the conflict is proven
    # *from the pushed branch alone* — exactly the lease the local referee missed.
    assert code == 1, "a lease that lives only on a pushed PR branch must be seen"


def test_check_excludes_its_own_branch_from_the_pr_source(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A branch never conflicts with its own pushed lease (self-exclusion)."""
    board = _synthetic_directory_claim_board()
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")
    _pin_clock_inside_lease(monkeypatch, board_module, board["claims"][0])

    seen_branches: list[list[str]] = []

    def _spy(branches: list[str], exclude_branch: str = "") -> object:
        seen_branches.append(list(branches))
        return [], []

    monkeypatch.setattr(board_module, "_git", _no_git)
    monkeypatch.setattr(board_module, "_read_remote_board", lambda: (None, "test: offline"))
    monkeypatch.setattr(board_module, "_open_pr_branches", lambda repo, token: (["arena/mine"], ""))
    monkeypatch.setattr(board_module, "_remote_branch_boards", _spy)
    board_module.cmd_check(
        type(
            "A",
            (),
            {
                "files": "docs/x.md",
                "branch": "arena/mine",
                "no_remote": False,
                "repo": "o/r",
                "token": "t",
            },
        )()
    )
    assert seen_branches == [["arena/mine"]]


def test_remote_branch_boards_reads_only_freshly_fetched_branches(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale tracking ref must not be read as a live lease."""
    good_board = _synthetic_directory_claim_board()

    def _fake_git(args: list[str], cwd: Path) -> object:
        refspecs = args[3:] if args[0] == "fetch" else []
        if len(refspecs) > 1:  # batched fetch: one dead ref fails the batch
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="dead ref")
        if refspecs:  # per-branch fallback
            if "arena/good" in refspecs[0]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="dead")
        if args[0] == "show":
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(good_board), stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="")

    monkeypatch.setattr(board_module, "_git", _fake_git)
    boards, unreadable = board_module._remote_branch_boards(
        ["arena/good", "arena/dead"], exclude_branch=""
    )
    assert [name for name, _ in boards] == ["branch:arena/good"]
    assert any("arena/dead" in reason for reason in unreadable)


def test_remote_branch_boards_excludes_the_callers_own_branch(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A branch must not be fetched or read as a foreign lease of itself."""
    fetched: list[str] = []

    def _fake_git(args: list[str], cwd: Path) -> object:
        if args[0] == "fetch":
            fetched.append(" ".join(args))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[0] == "show":
            return subprocess.CompletedProcess(
                args, 0, stdout=json.dumps(_synthetic_directory_claim_board()), stderr=""
            )
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="")

    monkeypatch.setattr(board_module, "_git", _fake_git)
    boards, _unreadable = board_module._remote_branch_boards(
        ["arena/mine", "arena/other"], exclude_branch="arena/mine"
    )
    assert [name for name, _ in boards] == ["branch:arena/other"]
    assert all("arena/mine" not in cmd for cmd in fetched)


def test_open_pr_branches_requires_repo_and_token(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    branches, reason = board_module._open_pr_branches("", "")
    assert branches == [] and reason
    branches, reason = board_module._open_pr_branches("o/r", "")
    assert branches == [] and reason


def test_open_pr_branches_degrades_on_api_error(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(url: str, token: object) -> object:
        raise OSError("connection reset")

    monkeypatch.setattr(board_module, "_gh_get", _boom)
    branches, reason = board_module._open_pr_branches("o/r", "t")
    assert branches == [] and "unavailable" in reason


# --------------------------------------------------------------------------- #
# lease fencing (task-219): a superseded owner cannot mutate the lease
# --------------------------------------------------------------------------- #
def _generation_of(board_module: ModuleType, task: str) -> int:
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    return next(c for c in board["claims"] if c["task"] == task).get("generation", 0)


def test_claim_takeover_advances_generation(board_module: ModuleType) -> None:
    """Taking a lease over bumps its fencing epoch."""
    task = _first_claimable(board_module.BOARD)
    before = _generation_of(board_module, task)
    board_module.cmd_claim(
        type("A", (), {"task": task, "branch": "arena/999-new", "ttl": 24, "gates": False})()
    )
    assert _generation_of(board_module, task) == before + 1


def test_heartbeat_keeps_generation_stable(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-owner renewal is not a transfer — the epoch must not move."""
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    active = _first_active_claim(board)
    _pin_clock_inside_lease(monkeypatch, board_module, active)
    before = _generation_of(board_module, active["task"])
    code = board_module.cmd_claim(
        type(
            "A",
            (),
            {
                "task": active["task"],
                "branch": active["agent_branch"],
                "ttl": 24,
                "gates": False,
                "expected_generation": before,
            },
        )()
    )
    assert code == 0
    assert _generation_of(board_module, active["task"]) == before


def test_stale_owner_renewal_is_fenced_out(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A resumed stale owner (wrong generation) must be refused, not warned."""
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    active = _first_active_claim(board)
    _pin_clock_inside_lease(monkeypatch, board_module, active)
    current = _generation_of(board_module, active["task"])
    code = board_module.cmd_claim(
        type(
            "A",
            (),
            {
                "task": active["task"],
                "branch": active["agent_branch"],
                "ttl": 24,
                "gates": False,
                "expected_generation": current - 1,  # stale: zone changed hands
            },
        )()
    )
    assert code == 2
    assert "stale lease generation" in capsys.readouterr().out


def test_stale_owner_release_is_fenced_out(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    active = _first_active_claim(board)
    _pin_clock_inside_lease(monkeypatch, board_module, active)
    code = board_module.cmd_release(
        type(
            "A",
            (),
            {
                "task": active["task"],
                "branch": active["agent_branch"],
                "expected_generation": 999,
            },
        )()
    )
    assert code == 2
    # and the lease is untouched by the refused mutation
    assert _generation_of(board_module, active["task"]) != 999


def test_release_advances_generation(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Release transfers the zone and must advance the fencing epoch."""
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    active = _first_active_claim(board)
    _pin_clock_inside_lease(monkeypatch, board_module, active)
    task, owner = active["task"], active["agent_branch"]
    gen_before = _generation_of(board_module, task)
    assert (
        board_module.cmd_release(
            type("A", (), {"task": task, "branch": owner, "expected_generation": gen_before})()
        )
        == 0
    )
    assert _generation_of(board_module, task) == gen_before + 1


def test_generation_guard_fences_a_reused_branch_identity(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The load-bearing case: two sessions sharing one branch name.

    Branch identity alone cannot separate two sessions that both call
    themselves ``arena/x`` (the documented "three sessions declared agent E"
    incident).  Generation fencing can: the second session re-took the lease and
    advanced the epoch, so the first session's recorded expectation is stale and
    its renewal is refused — deterministically, not as a warning.
    """
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    active = _first_active_claim(board)
    _pin_clock_inside_lease(monkeypatch, board_module, active)
    task, owner = active["task"], active["agent_branch"]
    session_one_expectation = _generation_of(board_module, task)

    # Session two (same branch name) took the lease over: owner unchanged, epoch bumped.
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    claim = next(c for c in board["claims"] if c["task"] == task)
    claim["generation"] = session_one_expectation + 1
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")

    # Session one resumes with its stale expectation → fenced out.
    code = board_module.cmd_claim(
        type(
            "A",
            (),
            {
                "task": task,
                "branch": owner,
                "ttl": 24,
                "gates": False,
                "expected_generation": session_one_expectation,
            },
        )()
    )
    assert code == 2
    assert "stale lease generation" in capsys.readouterr().out
    # The lease was not mutated by the refused attempt.
    assert _generation_of(board_module, task) == session_one_expectation + 1


def test_defer_is_fenced_for_a_stale_generation(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    active = _first_active_claim(board)
    _pin_clock_inside_lease(monkeypatch, board_module, active)
    code = board_module.cmd_defer(
        type(
            "A",
            (),
            {
                "task": active["task"],
                "branch": active["agent_branch"],
                "reason": "",
                "fa": "",
                "resume_when": "",
                "expected_generation": 4242,
            },
        )()
    )
    assert code == 2


def test_legacy_claim_without_generation_is_fenced_as_zero(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backward compatibility: a claim lacking ``generation`` reads as 0."""
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    active = _first_active_claim(board)
    active.pop("generation", None)  # simulate a pre-task-219 board
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")
    _pin_clock_inside_lease(monkeypatch, board_module, active)
    assert _generation_of(board_module, active["task"]) == 0
    # a caller that expects generation 0 (the legacy default) is allowed to renew
    assert (
        board_module.cmd_claim(
            type(
                "A",
                (),
                {
                    "task": active["task"],
                    "branch": active["agent_branch"],
                    "ttl": 24,
                    "gates": False,
                    "expected_generation": 0,
                },
            )()
        )
        == 0
    )


def test_claim_renews_its_own_lease(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    active = _first_active_claim(board)
    _pin_clock_inside_lease(monkeypatch, board_module, active)
    code = board_module.cmd_claim(
        type(
            "A",
            (),
            {
                "task": active["task"],
                "branch": active["agent_branch"],
                "ttl": 10,
                "gates": False,
                "expected_generation": _generation_of(board_module, active["task"]),
            },
        )()
    )
    assert code == 0
    updated = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    renewed = next(c for c in updated["claims"] if c["task"] == active["task"])
    # renewal is a heartbeat: the same owner keeps the zone, the clock moves forward
    assert renewed["status"] == "active" and renewed["agent_branch"] == active["agent_branch"]
    assert renewed["claimed_at"] >= active["claimed_at"]
    # Documented behaviour: `claim` on an existing lease is a heartbeat — it refreshes the
    # clock and keeps the original TTL (the --ttl flag only applies when the lease is taken).
    assert renewed["ttl_hours"] == active["ttl_hours"]


def test_mutating_an_existing_lease_without_a_token_is_refused(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Fencing is mandatory: a mutation of a live lease with no token is refused.

    Optional fencing was a gap — a stale owner that never recorded its epoch
    could still renew. The token is now required for every existing-lease
    mutation, while taking a *free* lease needs none.
    """
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    active = _first_active_claim(board)
    _pin_clock_inside_lease(monkeypatch, board_module, active)
    task, owner = active["task"], active["agent_branch"]

    assert (
        board_module.cmd_claim(
            type("A", (), {"task": task, "branch": owner, "ttl": 24, "gates": False})()
        )
        == 2
    )
    assert "requires --expected-generation" in capsys.readouterr().out

    assert board_module.cmd_release(type("A", (), {"task": task, "branch": owner})()) == 2
    assert (
        board_module.cmd_defer(
            type(
                "A",
                (),
                {"task": task, "branch": owner, "reason": "", "fa": "", "resume_when": ""},
            )()
        )
        == 2
    )
    # The lease is untouched by any refused mutation.
    assert _generation_of(board_module, task) == _generation_of(board_module, task)


def test_stale_owner_attack_matrix(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A superseded owner is refused on every mutation path, board untouched.

    Session A holds gen 1; session B takes over (gen 2). A wakes up and tries
    heartbeat / release / defer / re-claim with its stale token — every path
    must be a deterministic refusal, and the board must be byte-identical.
    """
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    active = _first_active_claim(board)
    _pin_clock_inside_lease(monkeypatch, board_module, active)
    task, owner = active["task"], active["agent_branch"]

    # B takes the lease over: epoch advances (simulated as a same-name takeover).
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    claim = next(c for c in board["claims"] if c["task"] == task)
    claim["generation"] = _generation_of(board_module, task) + 1
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")
    snapshot = board_module.BOARD.read_text(encoding="utf-8")
    stale = claim["generation"] - 1

    heartbeat = board_module.cmd_claim(
        type(
            "A",
            (),
            {
                "task": task,
                "branch": owner,
                "ttl": 24,
                "gates": False,
                "expected_generation": stale,
            },
        )()
    )
    release = board_module.cmd_release(
        type("A", (), {"task": task, "branch": owner, "expected_generation": stale})()
    )
    defer = board_module.cmd_defer(
        type(
            "A",
            (),
            {
                "task": task,
                "branch": owner,
                "reason": "",
                "fa": "",
                "resume_when": "",
                "expected_generation": stale,
            },
        )()
    )
    reclaim = board_module.cmd_claim(
        type(
            "A",
            (),
            {
                "task": task,
                "branch": "arena/attacker",
                "ttl": 24,
                "gates": False,
                "expected_generation": stale,
            },
        )()
    )
    assert (heartbeat, release, defer, reclaim) == (2, 2, 2, 2)
    assert "stale lease generation" in capsys.readouterr().out
    assert board_module.BOARD.read_text(encoding="utf-8") == snapshot, "board must be untouched"


def test_takeover_of_an_expired_lease_requires_a_token(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-claiming an expired (but once-live) lease is a mutation and is fenced."""
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    task_id = _first_claimable(board_module.BOARD)
    claim = next(c for c in board["claims"] if c["task"] == task_id)
    claim["generation"] = 3
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")
    # No token: refused (this is an existing lease record, not a fresh task).
    assert (
        board_module.cmd_claim(
            type("A", (), {"task": claim["task"], "branch": "arena/x", "ttl": 24, "gates": False})()
        )
        == 2
    )
    # With the matching token: allowed, and the epoch advances.
    assert (
        board_module.cmd_claim(
            type(
                "A",
                (),
                {
                    "task": claim["task"],
                    "branch": "arena/x",
                    "ttl": 24,
                    "gates": False,
                    "expected_generation": 3,
                },
            )()
        )
        == 0
    )
    assert _generation_of(board_module, claim["task"]) == 4


def test_taking_a_fresh_lease_needs_no_token(board_module: ModuleType) -> None:
    """A brand-new task (generation 0) is not fenced — there is nothing to fence."""
    task = _first_claimable(board_module.BOARD)
    assert (
        board_module.cmd_claim(
            type("A", (), {"task": task, "branch": "arena/fresh", "ttl": 24, "gates": False})()
        )
        == 0
    )


def test_board_mutation_is_locked_against_a_concurrent_writer(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held board lock makes a concurrent mutation fail closed, not race.

    Read-validate-write is a TOCTOU window; ``board_lock`` serialises it. While
    the lock is held, a mutation must raise ``BoardLockedError`` (main maps it to
    exit 2) rather than read a stale generation and overwrite.
    """
    import fcntl

    lock_path = board_module.BOARD.with_suffix(".lock")
    with lock_path.open("a+") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with pytest.raises(board_module.BoardLockedError):
                board_module.cmd_claim(
                    type(
                        "A",
                        (),
                        {"task": "anything", "branch": "arena/x", "ttl": 24, "gates": False},
                    )()
                )
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    # Once released, the same call no longer raises the lock error.
    task = _first_claimable(board_module.BOARD)
    assert (
        board_module.cmd_claim(
            type("A", (), {"task": task, "branch": "arena/after", "ttl": 24, "gates": False})()
        )
        == 0
    )


def test_main_maps_a_locked_board_to_exit_2(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A busy board must surface as a clean exit 2, never a traceback."""
    monkeypatch.setattr(
        board_module,
        "cmd_claim",
        lambda _a: (_ for _ in ()).throw(board_module.BoardLockedError("locked")),
    )
    monkeypatch.setattr(board_module.sys, "argv", ["agent_board.py", "claim", "t", "--branch", "b"])
    assert board_module.main() == 2
    assert "UNVERIFIABLE" in capsys.readouterr().out


def test_claim_takes_a_free_task_and_check_enforces_the_new_lease(board_module: ModuleType) -> None:
    task = _first_claimable(board_module.BOARD)
    args = type("A", (), {"task": task, "branch": "arena/999-new", "ttl": 24, "gates": False})()
    assert board_module.cmd_claim(args) == 0
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    claimed = next(c for c in board["claims"] if c["task"] == task)
    assert claimed["status"] == "active" and claimed["agent_branch"] == "arena/999-new"
    assert claimed["exclusive_paths"], "a claimed task must own at least one path"
    # and it can be released again by the same branch, carrying its fencing token
    args.expected_generation = _generation_of(board_module, task)
    assert board_module.cmd_release(args) == 0
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    assert next(c for c in board["claims"] if c["task"] == task)["status"] == "done"


def test_release_refuses_a_foreign_branch(board_module: ModuleType) -> None:
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    active = next(claim for claim in board["claims"] if claim["status"] == "active")
    code = board_module.cmd_release(
        type("A", (), {"task": active["task"], "branch": "arena/999-thief"})()
    )
    assert code == 2


def test_defer_records_the_bilingual_note(board_module: ModuleType) -> None:
    task = _first_claimable(board_module.BOARD)
    board_module.cmd_claim(
        type("A", (), {"task": task, "branch": "arena/999-new", "ttl": 24, "gates": False})()
    )
    code = board_module.cmd_defer(
        type(
            "A",
            (),
            {
                "task": task,
                "branch": "arena/999-new",
                "reason": "another agent holds the zone",
                "fa": "چون عامل دیگری روی این محدوده کار می‌کرد متوقف شدم.",
                "resume_when": f"claim {task} is free",
                "expected_generation": _generation_of(board_module, task),
            },
        )()
    )
    assert code == 0
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    entry = board["deferred_log"][-1]
    assert entry["task"] == task and entry["reason_fa"].startswith("چون عامل دیگری")
    claim = next(c for c in board["claims"] if c["task"] == task)
    assert claim["status"] == "deferred" and claim["claimed_at"] is None


def test_gc_strips_the_gate_from_an_expired_gates_owner(board_module: ModuleType) -> None:
    """An expired gates owner must not keep the single-gate role.

    The rule is exactly one live gate holder. A departed holder that gc expires
    while ``gates_owner`` is still true would leave the role falsely occupied,
    so gc clears it (provenance fields stay).
    """
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    stale = next(c for c in board["claims"] if c["status"] == "active")
    stale["claimed_at"] = _iso(datetime.now(timezone.utc) - timedelta(hours=48))
    stale["ttl_hours"] = 1
    stale["gates_owner"] = True
    board_module.BOARD.write_text(json.dumps(board, indent=2, ensure_ascii=False), encoding="utf-8")
    assert board_module.cmd_show(type("A", (), {})()) == 0
    reloaded = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    freed = next(c for c in reloaded["claims"] if c["task"] == stale["task"])
    assert freed["status"] == "expired"
    assert freed["gates_owner"] is False, "an expired holder must release the gate"


def test_show_garbage_collects_an_expired_lease(board_module: ModuleType) -> None:
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    stale = next(c for c in board["claims"] if c["status"] == "active")
    stale["claimed_at"] = _iso(datetime.now(timezone.utc) - timedelta(hours=48))
    stale["ttl_hours"] = 1
    handoff = "unpushed evidence from the departing session — must survive gc"
    stale["note"] = handoff
    board_module.BOARD.write_text(json.dumps(board, indent=2, ensure_ascii=False), encoding="utf-8")
    assert board_module.cmd_show(type("A", (), {})()) == 0
    reloaded = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    freed = next(c for c in reloaded["claims"] if c["task"] == stale["task"])
    assert freed["status"] == "expired"
    # gc records *why* it released the lease, and never clobbers the owner's note.
    assert "stale active lease" in freed["release_reason"]
    assert freed["note"] == handoff
