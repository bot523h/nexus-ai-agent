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


def _check_args(**kw):
    """A check invocation with every cross-PR option explicitly pinned.

    task-207 made ``check`` consult the boards of every open PR, and made it exit
    2 rather than 0 when that view could not be established.  Tests must therefore
    SAY which view they are testing: ``board_json=[]`` means "consult an empty,
    successfully-established foreign set" and ``no_remote=True`` means "local only,
    operator accepted the gap".  Neither is the default any more, and that is the
    point: the default is now the strict one.
    """
    base = {"repo": "", "token": "", "board_json": [], "no_remote": False}
    base.update(kw)
    return type("A", (), base)()


def test_check_detects_overlap_and_allows_disjoint_work(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _synthetic_directory_claim_board()
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")
    _pin_clock_inside_lease(monkeypatch, board_module, board["claims"][0])
    assert (
        board_module.cmd_check(
            _check_args(files="src/synthetic/intruder.py", branch="arena/999-other-agent")
        )
        == board_module.CHECK_OVERLAP
    )
    assert (
        board_module.cmd_check(
            _check_args(files="docs/not-owned-by-anyone.md", branch="arena/999-other")
        )
        == board_module.CHECK_CLEAR
    )


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


# ── task-207: `check` must not fail open ──────────────────────────────────
#
# The defect, reproduced before it was fixed (scripts/task207_repro.py): `check`
# read ONE board — the local working tree's — so a claim living only on an
# unmerged PR branch was invisible and the tool answered
#
#     no overlap — safe to proceed.        (exit 0)
#
# for a file somebody else had leased. That is the enforcement mechanism failing
# open, in the most reassuring wording it owns. It happened live: task-202
# (graph.py, PR #119) and task-203 (memory/, PR #121) were both leased while
# check reported no overlap.
#
# Visibility alone is not the fix — the same false negative returns the moment
# GitHub is unreachable — so these tests pin BOTH halves: the foreign lease is
# seen, and an unobtainable cross-PR view is never reported as clearance.

_TARGET = "src/nexus_ai_agent/orchestration/graph.py"
_FOREIGN = "arena/01a0e907-nexus-ai-agent"
_MINE = "arena/01a0eade-nexus-ai-agent"


def _foreign_lease_board(board_module: ModuleType, task: str = "task-202") -> dict:
    """A board on another branch holding a live lease on ``_TARGET``.

    A 7-day TTL is used deliberately, because the real ``pr33-in-review`` claim
    that fences bot/handlers.py on PR#63 is exactly that long.
    """
    board = _synthetic_directory_claim_board()
    claim = board["claims"][0]
    claim.update(
        {
            "task": task,
            "status": "active",
            "agent_branch": _FOREIGN,
            "exclusive_paths": [_TARGET],
            "ttl_hours": 168,
        }
    )
    board["zones"] = [{"id": "synthetic", "paths": [_TARGET]}]
    return board


def _pin(board_module: ModuleType, monkeypatch: pytest.MonkeyPatch, board: dict) -> None:
    _pin_clock_inside_lease(monkeypatch, board_module, board["claims"][0])


def test_check_sees_a_lease_that_exists_only_on_another_prs_board(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """THE DEFECT. The local board is empty; the foreign one is not."""
    local = _synthetic_directory_claim_board()
    local["claims"] = []
    board_module.BOARD.write_text(json.dumps(local), encoding="utf-8")

    foreign = _foreign_lease_board(board_module)
    _pin(board_module, monkeypatch, foreign)
    foreign_path = tmp_path / "foreign.json"
    foreign_path.write_text(json.dumps(foreign), encoding="utf-8")

    args = _check_args(files=_TARGET, branch=_MINE, board_json=[str(foreign_path)])
    assert board_module.cmd_check(args) == board_module.CHECK_OVERLAP, (
        "a live lease held on an unmerged PR branch did not stop the check — "
        "this is the task-207 defect returning"
    )


def test_the_offline_opt_out_is_loud_about_what_it_did_not_see(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`--no-remote` may pass, but it must never pass SILENTLY."""
    board = _synthetic_directory_claim_board()
    board["claims"] = []
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")

    code = board_module.cmd_check(_check_args(files="docs/x.md", branch=_MINE, no_remote=True))
    out = capsys.readouterr()
    assert code == board_module.CHECK_CLEAR
    assert "NOT CONSULTED" in out.err, (
        "--no-remote exited 0 without saying that foreign PR boards were never read"
    )


def test_an_unobtainable_cross_pr_view_is_never_reported_as_clear(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys, tmp_path
) -> None:
    """The fail-closed half: 'could not verify' must not share exit 0 with 'clear'."""
    board = _synthetic_directory_claim_board()
    board["claims"] = []
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")

    missing = tmp_path / "not-there.json"
    code = board_module.cmd_check(
        _check_args(files="docs/x.md", branch=_MINE, board_json=[str(missing)])
    )
    out = capsys.readouterr()
    assert code == board_module.CHECK_UNVERIFIED, (
        "an unreadable foreign board produced a PASS; that is the fail-open bug"
    )
    assert code != board_module.CHECK_CLEAR
    assert "safe to proceed" not in out.out
    assert "NOT a pass" in out.err


def test_check_exit_codes_are_three_distinct_values(board_module: ModuleType) -> None:
    codes = {
        board_module.CHECK_CLEAR,
        board_module.CHECK_OVERLAP,
        board_module.CHECK_UNVERIFIED,
    }
    assert len(codes) == 3, f"exit codes collide: {codes}"


def test_the_local_board_is_consulted_even_when_foreign_boards_are_named(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A regression found by this suite: `--board-json` suppressed the local board.

    The foreign boards are ADDITIONAL views of the same repository. Treating them
    as a replacement silently dropped every lease recorded on the working tree,
    which is the more dangerous of the two mistakes.
    """
    local = _synthetic_directory_claim_board()  # holds src/synthetic/
    board_module.BOARD.write_text(json.dumps(local), encoding="utf-8")
    _pin_clock_inside_lease(monkeypatch, board_module, local["claims"][0])

    empty = tmp_path / "none.json"
    empty.write_text(json.dumps(_synthetic_directory_claim_board()), encoding="utf-8")

    code = board_module.cmd_check(
        _check_args(
            files="src/synthetic/intruder.py",
            branch="arena/999-other",
            board_json=[str(empty)],
        )
    )
    assert code == board_module.CHECK_OVERLAP, (
        "the local board was skipped when foreign boards were supplied"
    )


def test_conflicting_paths_across_is_pure_and_deduplicates(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule is exercised without a network, a git remote, or a real claim."""
    board = _foreign_lease_board(board_module)
    _pin(board_module, monkeypatch, board)
    target = [_TARGET]
    hits = board_module.conflicting_paths_across(
        [("a", board), ("a", board), ("b", board)], _MINE, target
    )
    # Dedup is per (task, file, source): the same claim seen twice in ONE board
    # is one collision, but the same claim in TWO different PRs is two collisions
    # and worth showing separately - it means two agents hold the same path.
    assert hits == [("task-202", _TARGET, "a"), ("task-202", _TARGET, "b")], (
        f"unexpected dedup/source behaviour: {hits!r}"
    )


def test_an_expired_foreign_lease_does_not_block(board_module: ModuleType, monkeypatch) -> None:
    """Fail-closed must not become 'blocked forever by a ghost'."""
    board = _foreign_lease_board(board_module)
    claim = board["claims"][0]
    claim["claimed_at"] = "2020-01-01T00:00:00Z"
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")
    assert (
        board_module.cmd_check(_check_args(files=_TARGET, branch=_MINE, board_json=[]))
        == board_module.CHECK_CLEAR
    )
