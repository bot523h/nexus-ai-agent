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
    data = json.loads(board_path.read_text(encoding="utf-8"))
    return next(
        claim["task"]
        for claim in data["claims"]
        if claim["status"] in {"queued", "available", "expired", "deferred"}
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
            {"task": active["task"], "branch": active["agent_branch"], "ttl": 10, "gates": False},
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


def test_claim_takes_a_free_task_and_check_enforces_the_new_lease(board_module: ModuleType) -> None:
    task = _first_claimable(board_module.BOARD)
    args = type("A", (), {"task": task, "branch": "arena/999-new", "ttl": 24, "gates": False})()
    assert board_module.cmd_claim(args) == 0
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    claimed = next(c for c in board["claims"] if c["task"] == task)
    assert claimed["status"] == "active" and claimed["agent_branch"] == "arena/999-new"
    assert claimed["exclusive_paths"], "a claimed task must own at least one path"
    # and it can be released again by the same branch
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
            },
        )()
    )
    assert code == 0
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    entry = board["deferred_log"][-1]
    assert entry["task"] == task and entry["reason_fa"].startswith("چون عامل دیگری")
    claim = next(c for c in board["claims"] if c["task"] == task)
    assert claim["status"] == "deferred" and claim["claimed_at"] is None


def test_show_garbage_collects_an_expired_lease(board_module: ModuleType) -> None:
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    stale = next(c for c in board["claims"] if c["status"] == "active")
    stale["claimed_at"] = _iso(datetime.now(timezone.utc) - timedelta(hours=48))
    stale["ttl_hours"] = 1
    board_module.BOARD.write_text(json.dumps(board, indent=2, ensure_ascii=False), encoding="utf-8")
    assert board_module.cmd_show(type("A", (), {})()) == 0
    reloaded = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    freed = next(c for c in reloaded["claims"] if c["task"] == stale["task"])
    assert freed["status"] == "expired"
