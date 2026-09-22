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
mutated by a test run.

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


@pytest.fixture()
def board_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = _load_board_cli()
    board_copy = tmp_path / "board.json"
    board_copy.write_text(BOARD_PATH.read_text(encoding="utf-8"), encoding="utf-8")
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


def test_check_detects_overlap_and_allows_disjoint_work(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = json.loads(board_module.BOARD.read_text(encoding="utf-8"))
    active = _first_active_claim(board)
    _pin_clock_inside_lease(monkeypatch, board_module, active)
    mine = active["exclusive_paths"][0].rstrip("/") + "/intruder.py"
    assert (
        board_module.cmd_check(type("A", (), {"files": mine, "branch": "arena/999-other-agent"})())
        == 1
    )
    assert (
        board_module.cmd_check(
            type("A", (), {"files": "docs/not-owned-by-anyone.md", "branch": "arena/999-other"})()
        )
        == 0
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
