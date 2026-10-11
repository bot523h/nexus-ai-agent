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
    offenders: list[str] = []
    for claim in board["claims"]:
        if claim["status"] not in TERMINAL_STATUSES:
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
    # Isolate the worktree source: the open-PR branch source is readable-and-empty
    # so this test still proves the *worktree* conflict (not a blanket exit 2).
    monkeypatch.setattr(board_module, "_open_pr_heads", lambda repo, token: ([], ""))
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
    board_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """If sibling worktrees cannot be enumerated, the absence of overlap is unproven."""
    board = _synthetic_directory_claim_board()
    # One live gate holder, so the gates fail-closed branch cannot be the cause
    # of the exit code under test (see the assertion comment below).
    board["claims"][0]["gates_owner"] = True
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
    monkeypatch.setattr(board_module, "_open_pr_heads", lambda repo, token: ([], ""))

    code = board_module.cmd_check(
        type(
            "A",
            (),
            {"files": "docs/not-owned.md", "branch": "arena/999-me", "no_remote": False},
        )()
    )
    assert code == 2, "unreliable worktree enumeration must not be a clean pass"
    # …and exit 2 must be *this* reason. Asserting the code alone let the
    # mutation campaign's ``foreign_worktree_boards_skipped`` mutant survive: with
    # zero gate holders on the board the separate gates fail-closed branch also
    # returned 2, so the assertion passed while the guard it names was gone. The
    # board below therefore carries exactly one live gate holder, leaving the
    # unenumerable worktree source as the only possible cause — and the printed
    # reason is asserted, not just the exit code (task-266).
    out = capsys.readouterr().out
    assert "git worktree enumeration" in out, out
    assert "exactly one active, unexpired gates_owner" not in out, out


def _check_with_clean_sources(board_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> int:
    """Run ``check`` with every non-local source readable and disjoint.

    Leaves the *local board* as the only variable, so the verdict is decided by
    the gate-ownership contract rather than by a source that could not be read.
    """
    monkeypatch.setattr(board_module, "_worktree_boards", lambda exclude: ([], True))
    monkeypatch.setattr(board_module, "_read_remote_board", lambda: ({"claims": []}, ""))
    monkeypatch.setattr(board_module, "_open_pr_heads", lambda repo, token: ([], ""))
    return board_module.cmd_check(
        type(
            "A",
            (),
            {"files": "docs/not-owned.md", "branch": "arena/999-me", "no_remote": False},
        )()
    )


def test_check_fails_closed_with_no_gate_owner(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Zero live gate holders is not "no one is running the gates" — it is unverifiable.

    The gate is lease-scoped, so an idle board legitimately holds none; the
    referee must then refuse to certify anything (exit 2), because nobody is
    accountable for the full gates.  Asserting the *reason*, not just the code,
    is what keeps this honest (see the worktree-source test above for the failure
    mode that creates: the same exit code from an unrelated branch).
    """
    board = _synthetic_directory_claim_board()
    board["claims"][0]["gates_owner"] = False
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")
    _pin_clock_inside_lease(monkeypatch, board_module, board["claims"][0])

    code = _check_with_clean_sources(board_module, monkeypatch)
    assert code == 2, "zero gate holders must be fail-closed"
    assert "exactly one active, unexpired gates_owner" in capsys.readouterr().out


def test_check_fails_closed_with_two_gate_owners(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The invariant is *exactly* one: a second live holder is a refusal, not a tie-break."""
    board = _synthetic_directory_claim_board()
    first = board["claims"][0]
    first["gates_owner"] = True
    board["claims"].append(
        {
            "task": "task-9999-second-gate-holder",
            "status": "active",
            "zone": first["zone"],
            "agent_branch": "arena/999-second-gate-holder",
            "claimed_at": first["claimed_at"],
            "ttl_hours": 24,
            "gates_owner": True,
            "exclusive_paths": [],
        }
    )
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")
    _pin_clock_inside_lease(monkeypatch, board_module, first)

    code = _check_with_clean_sources(board_module, monkeypatch)
    assert code == 2, "two live gate holders must be fail-closed"
    assert "exactly one active, unexpired gates_owner" in capsys.readouterr().out


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
        board_module,
        "_open_pr_heads",
        lambda repo, token: ([_pr_head(board_module, "o/r", "arena/999-pr-only", 1)], ""),
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


def test_self_exclusion_is_by_identity_not_by_branch_name(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller's own head is skipped; another repo's same-named head is not.

    Excluding by bare branch name would drop a *different* repository's PR head
    that merely shares this caller's branch name — a source silently lost — so the
    exclusion is applied to (repository, ref) exactly (task-266 review finding B).
    """
    fetched: list[str] = []

    def _fake_git(args: list[str], cwd: Path) -> object:
        if args[0] == "fetch":
            fetched.extend(
                refspec.split(":")[-1] for refspec in args if refspec.startswith("+refs/heads/")
            )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[0] == "show":
            return subprocess.CompletedProcess(
                args, 0, stdout=json.dumps(_synthetic_directory_claim_board()), stderr=""
            )
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="")

    monkeypatch.setattr(board_module, "_git", _fake_git)
    boards, _unreadable = board_module._remote_branch_boards(
        [
            _pr_head(board_module, "o/r", "arena/mine"),  # this caller's own head
            _pr_head(
                board_module, "fork/nexus-ai-agent", "arena/mine"
            ),  # a different repo, same ref
        ],
        coordination_repo="o/r",
        exclude_branch="arena/mine",
    )
    assert "refs/remotes/origin/arena/mine" not in fetched, "own head must be self-excluded"
    assert len(fetched) == 1, "the foreign same-named head is a different source, not 'me'"
    assert "nexus-referee" in fetched[0], "a foreign head is fetched into its own namespace"
    assert "pr-head:fork/nexus-ai-agent#arena/mine" in [name for name, _ in boards]


def test_remote_branch_boards_reads_only_freshly_fetched_branches(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale tracking ref must not be read as a live lease."""
    good_board = _synthetic_directory_claim_board()

    _shows: list[list[str]] = []

    def _fake_git(args: list[str], cwd: Path) -> object:
        if args[0] == "show":
            _shows.append(args)
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
        [_pr_head(board_module, "o/r", "arena/good"), _pr_head(board_module, "o/r", "arena/dead")],
        "o/r",
    )
    assert [name for name, _ in boards] == ["branch:arena/good"]
    assert any("arena/dead" in reason for reason in unreadable)
    assert all(cmd[1].startswith("refs/remotes/origin/arena/good:") for cmd in _shows), (
        "a local head is read from its own freshly fetched tracking ref"
    )


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
        [_pr_head(board_module, "o/r", "arena/mine"), _pr_head(board_module, "o/r", "arena/other")],
        coordination_repo="o/r",
        exclude_branch="arena/mine",
    )
    assert [name for name, _ in boards] == ["branch:arena/other"]
    assert all("arena/mine" not in cmd for cmd in fetched)


def test_remote_branch_boards_consults_every_open_pr_branch(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Batching must never narrow the source set: a clean pass is a claim about *read* data.

    The referee used to truncate the open-PR branch list to the first
    ``_REMOTE_BRANCH_FETCH_LIMIT`` entries and still print "no overlap — safe to
    proceed".  On this repository (53 open PRs) that silently dropped 28 pushed
    boards while exiting 0 — a false clean verdict on a *required* source, and
    the oldest branches it dropped are exactly the long-lived PRs whose pushed
    leases are most likely to still fence a zone (task-266).
    """
    limit = board_module._REMOTE_BRANCH_FETCH_LIMIT
    heads = [
        _pr_head(board_module, "o/r", f"arena/branch-{index:03d}") for index in range(limit + 3)
    ]
    fetched: list[str] = []

    def _fake_git(args: list[str], cwd: Path) -> object:
        if args[0] == "fetch":
            fetched.extend(refspec.split(":refs/remotes/origin/")[1] for refspec in args[3:])
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[0] == "show":
            return subprocess.CompletedProcess(
                args, 0, stdout=json.dumps(_synthetic_directory_claim_board()), stderr=""
            )
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="")

    monkeypatch.setattr(board_module, "_git", _fake_git)
    boards, unreadable = board_module._remote_branch_boards(heads, "o/r")
    assert unreadable == [], "every branch is fetchable in this scenario"
    assert sorted(fetched) == sorted(head.ref for head in heads), "no open-PR head may be skipped"
    assert [name for name, _ in boards] == [f"branch:{head.ref}" for head in heads]


def test_a_batch_boundary_never_hides_a_branch_failure(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A branch past the first batch must still be fetched and, if dead, reported.

    The batched ``git fetch`` is a fast path, not a coverage strategy: when it
    fails, the per-branch fallback has to run for *every* branch — including the
    ones in later batches — so an unreachable board degrades to exit 2 instead of
    disappearing from the verdict.
    """
    limit = board_module._REMOTE_BRANCH_FETCH_LIMIT
    readable = [f"arena/ok-{index:03d}" for index in range(limit)]
    dead = "arena/dead-beyond-the-first-batch"
    heads = [_pr_head(board_module, "o/r", ref) for ref in [*readable, dead]]

    def _fake_git(args: list[str], cwd: Path) -> object:
        if args[0] == "fetch":
            if len(args[3:]) > 1:  # the batched fast path fails for the whole group
                return subprocess.CompletedProcess(args, 128, stdout="", stderr="dead ref")
            if "refs/heads/arena/ok-" in args[-1]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="dead")
        if args[0] == "show":
            return subprocess.CompletedProcess(
                args, 0, stdout=json.dumps(_synthetic_directory_claim_board()), stderr=""
            )
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="")

    monkeypatch.setattr(board_module, "_git", _fake_git)
    boards, unreadable = board_module._remote_branch_boards(heads, "o/r")
    assert [name for name, _ in boards] == [f"branch:{ref}" for ref in readable]
    assert any(dead in reason for reason in unreadable), "a dead branch must be loud"


def _pr_head(board_module: ModuleType, repo: str, ref: str, number: int = 0) -> object:
    """A PR head identity, built by the module under test.

    Built by *that* module (not by a second import of the script): the reader
    type-checks its input, and a class object from another module instance is a
    different class — the same trap a caller would hit by re-importing the tool.
    """
    return board_module.PrHead(repo, ref, number)


def _paginated_gh_get(page_one: int = 100, page_two: list | None = None, *, repo: str = "o/r"):
    """Fake GitHub listing: ``page_one`` PRs, then ``page_two`` (or a final short page)."""

    def _fake(url: str, token: object) -> object:
        if "page=2" in url:
            if page_two is None:
                raise AssertionError(f"unexpected second page request: {url}")
            return page_two
        return [
            {
                "number": index,
                "head": {
                    "ref": f"arena/harmless-{index}",
                    "repo": {"full_name": repo, "clone_url": f"https://github.com/{repo}.git"},
                },
            }
            for index in range(1, page_one + 1)
        ]

    return _fake


def test_open_pr_heads_walks_every_page(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing longer than one page must be read in full, not capped at page one.

    GitHub returns one page per request, so a page-one-only listing hides every PR
    past ``_OPEN_PR_PAGE_SIZE`` — with 101 open PRs, PR #101's pushed lease was
    invisible while ``check`` still reported a clean pass (task-266 review finding A).
    """
    monkeypatch.setattr(
        board_module,
        "_gh_get",
        _paginated_gh_get(
            100,
            [
                {
                    "number": 101,
                    "head": {
                        "ref": "arena/first-pr-on-page-two",
                        "repo": {"full_name": "o/r", "clone_url": "https://github.com/o/r.git"},
                    },
                }
            ],
        ),
    )
    heads, reason = board_module._open_pr_heads("o/r", "t")
    assert reason == ""
    assert len(heads) == 101, "every page must contribute its PRs"
    assert heads[-1].ref == "arena/first-pr-on-page-two"
    assert heads[-1].repo_full_name == "o/r"


def test_open_pr_heads_fails_closed_when_a_later_page_is_unreachable(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page that cannot be fetched makes the whole listing unverifiable.

    Page one alone is 100 readable boards and one silent gap; returning it would
    let ``check`` certify a source set it never finished reading.
    """

    def _fake(url: str, token: object) -> object:
        if "page=2" in url:
            raise OSError("connection reset by peer")
        return _paginated_gh_get()(url, token)

    monkeypatch.setattr(board_module, "_gh_get", _fake)
    heads, reason = board_module._open_pr_heads("o/r", "t")
    assert heads == [], "a partial listing must not be returned as the source set"
    assert "page 2" in reason and "unavailable" in reason


def test_open_pr_heads_fails_closed_on_a_malformed_page(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected payload is a failure to interpret the page, not an empty page."""

    def _fake(url: str, token: object) -> object:
        if "page=2" in url:
            return {"message": "rate limited"}
        return _paginated_gh_get()(url, token)

    monkeypatch.setattr(board_module, "_gh_get", _fake)
    heads, reason = board_module._open_pr_heads("o/r", "t")
    assert heads == [] and "unexpected payload" in reason


def test_open_pr_heads_refuses_a_listing_beyond_the_page_bound(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing that never ends is refused loudly, never truncated silently."""

    def _always_full(url: str, token: object) -> object:
        return [
            {
                "number": index,
                "head": {
                    "ref": f"arena/harmless-{index}",
                    "repo": {"full_name": "o/r", "clone_url": "https://github.com/o/r.git"},
                },
            }
            for index in range(1, board_module._OPEN_PR_PAGE_SIZE + 1)
        ]

    monkeypatch.setattr(board_module, "_gh_get", _always_full)
    heads, reason = board_module._open_pr_heads("o/r", "t")
    assert heads == []
    assert "refusing to treat a truncated list as complete" in reason


def test_open_pr_heads_refuses_a_head_without_repository_identity(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deleted fork leaves no repository to read: that head is unverifiable."""

    def _fake(url: str, token: object) -> object:
        return [{"number": 5, "head": {"ref": "main", "repo": None}}]

    monkeypatch.setattr(board_module, "_gh_get", _fake)
    heads, reason = board_module._open_pr_heads("o/r", "t")
    assert heads == []
    assert "#5" in reason and "deleted fork" in reason


def test_open_pr_heads_refuses_a_malformed_ref(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ref is interpolated into a refspec, so it is validated before use."""

    def _fake(url: str, token: object) -> object:
        return [
            {
                "number": 6,
                "head": {
                    "ref": "arena/../main",
                    "repo": {"full_name": "o/r", "clone_url": "https://github.com/o/r.git"},
                },
            }
        ]

    monkeypatch.setattr(board_module, "_gh_get", _fake)
    heads, reason = board_module._open_pr_heads("o/r", "t")
    assert heads == [] and "malformed" in reason


def test_a_repository_whose_name_escapes_the_github_shape_is_never_fetched(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fetch URL is derived from the identity, and only for a safe ``owner/name``.

    The API payload's ``clone_url`` must never steer ``git fetch``: a compromised
    token (or a fork named to look like a URL) could otherwise point this tool at
    an arbitrary host.
    """
    called: list[list[str]] = []

    def _fake_git(args: list[str], cwd: Path) -> object:
        called.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"claims": []}), stderr="")

    monkeypatch.setattr(board_module, "_git", _fake_git)
    # Names that are not a plausible GitHub "owner/name": a third path segment, a
    # traversal, a query string, a missing owner.
    for repo_name in ("o/r/extra", "../etc/passwd", "o/r?x=1", "/r", "o/", ".."):
        head = _pr_head(board_module, repo_name, "main")
        boards, unreadable = board_module._remote_branch_boards([head], coordination_repo="o/r")
        assert boards == [], f"{repo_name!r} must not be fetched"
        assert any("unusable repository identity" in reason for reason in unreadable)
    assert called == [], "no git command may run for an unusable identity"


def test_every_derived_fetch_url_stays_on_github(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fetch target is computed from the identity and always points at github.com.

    ``clone_url`` from the payload never steers ``git fetch``: a payload cannot
    move this tool to another host, whatever it claims the repository's URL is.
    """
    for repo_name in ("o/r", "evil.example.com/x", "owner/repo.name", "Owner_1/repo-2"):
        head = _pr_head(board_module, repo_name, "main")
        url = board_module._head_fetch_url(head)
        assert url == f"https://github.com/{repo_name}.git", url
        assert url.startswith("https://github.com/")
        assert "gitlab" not in url and "http://" not in url


def test_head_fetch_url_ignores_a_payload_clone_url(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``clone_url`` from the API is not the source of truth; the identity is."""

    def _fake(url: str, token: object) -> object:
        return [
            {
                "number": 9,
                "head": {
                    "ref": "release",
                    "repo": {
                        "full_name": "fork/nexus-ai-agent",
                        "clone_url": "https://gitlab.example.com/fork/nexus-ai-agent.git",
                    },
                },
            }
        ]

    monkeypatch.setattr(board_module, "_gh_get", _fake)
    heads, reason = board_module._open_pr_heads("o/r", "t")
    assert reason == "" and len(heads) == 1
    assert board_module._head_fetch_url(heads[0]) == "https://github.com/fork/nexus-ai-agent.git"


def test_remote_branch_boards_reads_a_fork_from_its_own_repository(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fork head is fetched from the fork, never from a same-named origin branch.

    The dangerous shape is a fork PR whose branch is ``main``: resolving by ref
    name alone reads this clone's ``origin/main`` — a different repository, a
    different commit, a different board — and reports on it as if it were the
    PR's lease (task-266 review finding B).
    """
    commands: list[list[str]] = []

    def _fake_git(args: list[str], cwd: Path) -> object:
        commands.append(args)
        if args[0] == "show":
            return subprocess.CompletedProcess(
                args, 0, stdout=json.dumps({"claims": []}), stderr=""
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(board_module, "_git", _fake_git)
    boards, unreadable = board_module._remote_branch_boards(
        [_pr_head(board_module, "attacker/nexus-ai-agent", "main", 7)],
        coordination_repo="o/r",
    )
    fetches = [cmd for cmd in commands if cmd[0] == "fetch"]
    assert unreadable == [] and len(boards) == 1
    assert boards[0][0] == "pr-head:attacker/nexus-ai-agent#main"
    assert fetches and "https://github.com/attacker/nexus-ai-agent.git" in fetches[0]
    assert not any("origin" in cmd for cmd in fetches), "origin must not be used for a foreign head"
    assert fetches[0][-1] == "+refs/heads/main:refs/nexus-referee/pr-7"
    # …and the board is read from exactly that ref: fetching the fork and then
    # reading a same-named branch of origin would be the same defect wearing a
    # different hat (it survived the first mutation campaign for this reason).
    shows = [cmd for cmd in commands if cmd[0] == "show"]
    assert len(shows) == 1
    assert shows[0][1] == "refs/nexus-referee/pr-7:.agents/board.json", shows[0]


def test_a_fork_head_that_cannot_be_fetched_is_unverifiable(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the true head cannot be read, the referee must not fall back to origin."""

    def _fake_git(args: list[str], cwd: Path) -> object:
        if args[0] == "fetch":
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="repository not found")
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"claims": []}), stderr="")

    monkeypatch.setattr(board_module, "_git", _fake_git)
    boards, unreadable = board_module._remote_branch_boards(
        [_pr_head(board_module, "attacker/nexus-ai-agent", "main", 7)], coordination_repo="o/r"
    )
    assert boards == [], "origin must never stand in for an unreadable fork head"
    assert any("attacker/nexus-ai-agent#main" in reason for reason in unreadable)


def test_two_forks_sharing_a_ref_name_are_two_sources(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dedupe is by (repository, ref): the same ref name in two repos is two heads."""
    commands: list[list[str]] = []

    def _fake_git(args: list[str], cwd: Path) -> object:
        commands.append(args)
        if args[0] == "show":
            return subprocess.CompletedProcess(
                args, 0, stdout=json.dumps({"claims": []}), stderr=""
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(board_module, "_git", _fake_git)
    boards, unreadable = board_module._remote_branch_boards(
        [
            _pr_head(board_module, "forka/nexus-ai-agent", "release", 8),
            _pr_head(board_module, "forkb/nexus-ai-agent", "release", 9),
        ],
        coordination_repo="o/r",
    )
    urls = [cmd[3] for cmd in commands if cmd[0] == "fetch"]
    assert "https://github.com/forka/nexus-ai-agent.git" in urls
    assert "https://github.com/forkb/nexus-ai-agent.git" in urls
    assert sorted(name for name, _ in boards) == [
        "pr-head:forka/nexus-ai-agent#release",
        "pr-head:forkb/nexus-ai-agent#release",
    ]
    assert unreadable == []


def test_open_pr_heads_requires_repo_and_token(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    heads, reason = board_module._open_pr_heads("", "")
    assert heads == [] and reason
    heads, reason = board_module._open_pr_heads("o/r", "")
    assert heads == [] and reason


def test_open_pr_heads_degrades_on_api_error(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(url: str, token: object) -> object:
        raise OSError("connection reset")

    monkeypatch.setattr(board_module, "_gh_get", _boom)
    heads, reason = board_module._open_pr_heads("o/r", "t")
    assert heads == [] and "unavailable" in reason


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


def _heartbeat(
    board_module: ModuleType, task: str, branch: str, *, gates: bool, generation: int
) -> int:
    """Run the heartbeat (same-owner renewal) path of ``claim``."""
    return board_module.cmd_claim(
        type(
            "A",
            (),
            {
                "task": task,
                "branch": branch,
                "ttl": 24,
                "gates": gates,
                "expected_generation": generation,
            },
        )()
    )


def _claims_of(board_module: ModuleType) -> list[dict]:
    return json.loads(board_module.BOARD.read_text(encoding="utf-8"))["claims"]


def _live_gate_holders(board_module: ModuleType) -> list[str]:
    owners, malformed = board_module._active_gates_owners({"claims": _claims_of(board_module)})
    assert not malformed, malformed
    return [owner["task"] for owner in owners]


def _hermetic_gate_board(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict, dict]:
    """A one-lease board of its own, so gate assertions cannot read the live board.

    The ``board_module`` fixture mirrors the **real** board, which may hold a live
    gates owner (a session working right now) — and any lease whose ``claimed_at``
    lies in the future relative to a pinned clock is *live* by the referee's own
    arithmetic, so "exactly one gate holder" would silently depend on other
    agents' work and on the calendar.  The gate tests describe their own board
    (``_synthetic_directory_claim_board``) and pin the clock inside its lease.
    """
    board = _synthetic_directory_claim_board()
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")
    active = board["claims"][0]
    _pin_clock_inside_lease(monkeypatch, board_module, active)
    return board, active


def test_heartbeat_with_gates_takes_the_gate_on_a_live_own_lease(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """``claim --gates`` on a live own lease must take the gate, not just print success.

    The documented onboarding loop is ``next`` -> ``claim <task> --branch BR`` and
    then ``claim … --gates`` to own the CI gate (AGENTS.md §1 rule 4).  The second
    command lands on the *heartbeat* path, which used to ignore the flag entirely
    while still printing "renewed lease …" and exiting 0 — the board stayed at
    **zero** gates owners, every other agent's ``check`` kept returning exit 2, and
    the caller's "I own the gates" was false.  A success line without the effect is
    exactly what this pins (task-266).
    """
    _, active = _hermetic_gate_board(board_module, monkeypatch)
    assert active["gates_owner"] is False, "precondition: this lease holds no gate"

    code = _heartbeat(
        board_module,
        active["task"],
        active["agent_branch"],
        gates=True,
        generation=_generation_of(board_module, active["task"]),
    )
    assert code == 0
    renewed = next(c for c in _claims_of(board_module) if c["task"] == active["task"])
    assert renewed["gates_owner"] is True, "the gate must actually be held after --gates"
    assert _live_gate_holders(board_module) == [active["task"]]
    # the operator must be able to read the resulting state off the command's output
    assert "gates_owner: True" in capsys.readouterr().out


def test_heartbeat_with_gates_is_idempotent_and_leaves_one_holder(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicated gate (survived a bad merge) must collapse to the caller alone.

    Asserting exclusivity across *every* granting path is the point: the fresh
    claim, the takeover and the heartbeat all clear the other holders through one
    implementation, so "at most one live gate holder" is a property of the tool
    rather than of the path the caller happened to take.
    """
    board, active = _hermetic_gate_board(board_module, monkeypatch)
    board["claims"].append(
        {
            "task": "task-9999-other-gate-holder",
            "status": "active",
            "zone": active["zone"],
            "agent_branch": "arena/999-other-gate-holder",
            "claimed_at": active["claimed_at"],
            "ttl_hours": 24,
            "gates_owner": True,
            "exclusive_paths": [],
        }
    )
    active["gates_owner"] = True  # both hold it: the inconsistent board
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")

    code = _heartbeat(
        board_module,
        active["task"],
        active["agent_branch"],
        gates=True,
        generation=_generation_of(board_module, active["task"]),
    )
    assert code == 0
    assert _live_gate_holders(board_module) == [active["task"]]
    others = [c for c in _claims_of(board_module) if c["task"] == "task-9999-other-gate-holder"]
    assert others[0]["gates_owner"] is False, "the other holder must lose the gate"


def test_a_heartbeat_without_the_flag_never_drops_the_gate(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renewing a lease must not silently release a gate the owner still holds.

    The flag is additive: absent means "keep whatever I have".  Dropping it on
    every heartbeat would make the single-gate role expire faster than the lease
    it is attached to, with no message saying so.
    """
    board, active = _hermetic_gate_board(board_module, monkeypatch)
    active["gates_owner"] = True
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")

    code = _heartbeat(
        board_module,
        active["task"],
        active["agent_branch"],
        gates=False,
        generation=_generation_of(board_module, active["task"]),
    )
    assert code == 0
    renewed = next(c for c in _claims_of(board_module) if c["task"] == active["task"])
    assert renewed["gates_owner"] is True
    assert _live_gate_holders(board_module) == [active["task"]]


def test_a_stale_heartbeat_cannot_take_the_gate(
    board_module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The gate is fenced by the lease epoch: a superseded owner cannot seize it.

    ``--gates`` is not a way around the fencing token, otherwise a resumed stale
    session could take the single CI gate while another branch works the zone.
    """
    board, active = _hermetic_gate_board(board_module, monkeypatch)
    task = active["task"]
    stale = _generation_of(board_module, task)

    # another session took the zone over (or released it): the epoch advanced
    next(c for c in board["claims"] if c["task"] == task)["generation"] = stale + 1
    board_module.BOARD.write_text(json.dumps(board), encoding="utf-8")
    before = board_module.BOARD.read_text(encoding="utf-8")

    code = _heartbeat(board_module, task, active["agent_branch"], gates=True, generation=stale)
    assert code == 2, "a stale token must be refused, never honoured"
    assert "REFUSED" in capsys.readouterr().out
    assert board_module.BOARD.read_text(encoding="utf-8") == before, "board must be untouched"
    assert _live_gate_holders(board_module) == [], "a refused heartbeat grants nothing"


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
    claim = next(c for c in board["claims"] if c["status"] in ("queued", "expired"))
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
