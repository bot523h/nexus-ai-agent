"""Wave 2 — the PR convergence graph is deterministic, evidence-backed, read-only.

Fifty open pull requests and sixty ``arena/*`` branches are a queue nobody can
read.  ``scripts/pr_convergence.py`` turns them into a deterministic
convergence graph: classify, correlate, detect duplicates, rank, recommend.

Two properties are the point of this file:

1. **Read-only.** The tool must never merge, close, delete or retarget anything.
   That is asserted structurally (no HTTP write verb, no mutating ``gh``
   subcommand in the source) *and* behaviourally (the board it is handed is
   byte-identical afterwards).
2. **Deterministic and non-vacuous.** The same input always yields the same
   bytes, and each analysis step is attacked: disable the duplicate detector,
   drop the supersession edge, feed an unknown PR, invent a dependency, remove
   one — a surviving mutation means the invariant was never proven.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import urllib.error
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).parents[2]
SCRIPT = REPO_ROOT / "scripts" / "pr_convergence.py"

NOW = datetime(2026, 10, 7, 21, 30, tzinfo=timezone.utc)
MAIN_SHA = "a" * 40
OTHER_SHA = "b" * 40


@pytest.fixture()
def conv() -> Iterator[ModuleType]:
    spec = importlib.util.spec_from_file_location("pr_convergence_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    yield module
    sys.modules.pop(spec.name, None)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _pr(
    number: int,
    *,
    branch: str,
    files: list[str],
    head_sha: str | None = None,
    base_sha: str = MAIN_SHA,
    updated: datetime = NOW,
    created: datetime | None = None,
    complete: bool = True,
    title: str = "",
) -> dict:
    return {
        "number": number,
        "title": title or f"task {number}",
        "head_branch": branch,
        # a distinct head per PR by default: sharing one would make every pair
        # look like "the same commits pushed twice" (signal S1)
        "head_sha": f"{number:040x}" if head_sha is None else head_sha,
        "base_ref": "main",
        "base_sha": base_sha,
        "created_at": _iso(created or (updated - timedelta(days=1))),
        "updated_at": _iso(updated),
        "files": files,
        "files_complete": complete,
    }


TEMPORAL = [
    "src/nexus_ai_agent/creative/temporal/__init__.py",
    "src/nexus_ai_agent/creative/temporal/adapters.py",
    "src/nexus_ai_agent/creative/temporal/core.py",
    "src/nexus_ai_agent/creative/packs/delivery/operations.py",
    "tests/unit/test_temporal_algebra.py",
]
STORAGE = [
    "src/nexus_ai_agent/storage/db.py",
    "src/nexus_ai_agent/storage/models.py",
    "src/nexus_ai_agent/storage/migrations.py",
    "src/nexus_ai_agent/storage/resilience.py",
    "tests/unit/test_storage_resilience.py",
]
CLI = ["src/nexus_ai_agent/cli.py", "tests/unit/test_packs_cli.py"]


def _board() -> dict:
    """A board with one live claim, one prerequisite chain and one supersession."""
    claimed = _iso(NOW - timedelta(hours=1))
    return {
        "schema": 2,
        "updated_at": claimed,
        "zones": [{"id": "temporal", "paths": ["src/nexus_ai_agent/creative/temporal/"]}],
        "claims": [
            {
                "task": "task-1",
                "status": "active",
                "zone": "temporal",
                "agent_branch": "arena/owner-1",
                "claimed_at": claimed,
                "ttl_hours": 24,
                "gates_owner": False,
                "exclusive_paths": ["src/nexus_ai_agent/creative/temporal/"],
                "prerequisites": ["task-0"],
            },
            {
                "task": "task-0",
                "status": "active",
                "zone": "temporal",
                "agent_branch": "arena/owner-0",
                "claimed_at": claimed,
                "ttl_hours": 24,
                "gates_owner": False,
                "exclusive_paths": [],
                "prerequisites": [],
            },
            {
                "task": "task-9",
                "status": "superseded_by_PR33",
                "zone": "temporal",
                "agent_branch": "",
                "claimed_at": None,
                "gates_owner": False,
                "exclusive_paths": [],
            },
        ],
        "deferred_log": [],
        "next_work": [],
        "history": [],
    }


# --------------------------------------------------------------------------- #
# read-only is a structural property, not a promise
# --------------------------------------------------------------------------- #
def test_the_tool_never_mutates_github() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    forbidden = (
        r"method\s*=\s*[\"'](PUT|PATCH|POST|DELETE)",
        r"/merge\b",
        r"state=closed",
        r"gh\s+pr\s+(merge|close|edit|ready|review|comment)",
        r"save_board",
        r"write_text",
    )
    for pattern in forbidden:
        assert not re.search(pattern, source), (
            f"pr_convergence.py must be read-only but matched {pattern!r}"
        )


def test_the_board_it_is_handed_is_not_mutated(conv: ModuleType) -> None:
    board = _board()
    before = json.dumps(board, sort_keys=True)
    conv.analyze(
        [conv.pr_from_record(_pr(1, branch="arena/owner-1", files=TEMPORAL))],
        board,
        live_main_sha=MAIN_SHA,
        now=NOW,
    )
    assert json.dumps(board, sort_keys=True) == before


def test_the_report_declares_its_read_only_policy(conv: ModuleType) -> None:
    report = conv.analyze(
        [conv.pr_from_record(_pr(1, branch="arena/owner-1", files=TEMPORAL))],
        _board(),
        live_main_sha=MAIN_SHA,
        now=NOW,
    )
    assert "never merges" in report["mutation_policy"]


# --------------------------------------------------------------------------- #
# classification — one positive test per class, evidence-backed
# --------------------------------------------------------------------------- #
def _report(conv: ModuleType, records: list[dict], board: dict | None = None, **kw):
    return conv.analyze(
        [conv.pr_from_record(r) for r in records],
        board if board is not None else _board(),
        live_main_sha=kw.pop("live_main_sha", MAIN_SHA),
        now=kw.pop("now", NOW),
        **kw,
    )


def _class_of(report: dict, number: int) -> str:
    return next(r["class"] for r in report["prs"] if r["number"] == number)


def test_a_fresh_claimed_pr_with_no_pressure_is_active(conv: ModuleType) -> None:
    report = _report(conv, [_pr(1, branch="arena/owner-1", files=TEMPORAL)])
    assert _class_of(report, 1) == "ACTIVE"


def test_an_unowned_pr_is_orphaned(conv: ModuleType) -> None:
    report = _report(conv, [_pr(5, branch="arena/nobody-claimed-this", files=CLI)])
    assert _class_of(report, 5) == "ORPHANED"
    row = next(r for r in report["prs"] if r["number"] == 5)
    assert any("no live board claim" in s for s in row["signals"])


def test_incomplete_evidence_is_blocked_never_guessed(conv: ModuleType) -> None:
    """A truncated changed-file list cannot support a duplicate verdict."""
    report = _report(conv, [_pr(7, branch="arena/truncated", files=TEMPORAL, complete=False)])
    assert _class_of(report, 7) == "BLOCKED"
    row = next(r for r in report["prs"] if r["number"] == 7)
    assert row["evidence"]["verdict"] == "UNVERIFIABLE"
    assert any("truncated" in s for s in row["signals"])


def test_a_missing_head_sha_is_blocked(conv: ModuleType) -> None:
    report = _report(conv, [_pr(8, branch="arena/no-sha", files=TEMPORAL, head_sha="")])
    assert _class_of(report, 8) == "BLOCKED"


def test_two_prs_on_the_same_commits_are_duplicates(conv: ModuleType) -> None:
    same = "c" * 40
    report = _report(
        conv,
        [
            _pr(11, branch="arena/dup-a", files=TEMPORAL, head_sha=same),
            _pr(12, branch="arena/dup-b", files=TEMPORAL, head_sha=same),
        ],
        board={"schema": 2, "claims": [], "zones": [], "deferred_log": [], "next_work": []},
    )
    assert report["duplicate_clusters"] == [[11, 12]]
    classes = {_class_of(report, 11), _class_of(report, 12)}
    assert classes == {"CANONICAL", "DUPLICATE"}
    evidence = report["duplicate_evidence"][0]
    assert evidence["confidence"] == 1.0
    assert any("identical head_sha" in w for w in evidence["witnesses"])


def test_the_duplicate_survivor_is_chosen_by_a_documented_rank(conv: ModuleType) -> None:
    """The board-claimed, fresher PR wins the cluster; the rank is reproducible."""
    board = _board()
    report = _report(
        conv,
        [
            _pr(21, branch="arena/unclaimed", files=TEMPORAL),
            _pr(22, branch="arena/owner-1", files=TEMPORAL),
        ],
        board=board,
    )
    assert _class_of(report, 22) == "CANONICAL", report["duplicate_evidence"]
    assert _class_of(report, 21) == "DUPLICATE"


def test_a_near_identical_change_set_is_a_duplicate_without_board_help(
    conv: ModuleType,
) -> None:
    """File evidence alone can carry the verdict (the real PR#140/#144 shape)."""
    empty = {"schema": 2, "claims": [], "zones": [], "deferred_log": [], "next_work": []}
    report = _report(
        conv,
        [
            _pr(31, branch="arena/a", files=TEMPORAL),
            _pr(32, branch="arena/b", files=[*TEMPORAL, "src/nexus_ai_agent/continuum/x.py"]),
        ],
        board=empty,
    )
    assert report["duplicate_clusters"] == [[31, 32]]
    witnesses = " ".join(report["duplicate_evidence"][0]["witnesses"])
    assert "strong file overlap" in witnesses


def test_incidental_coordination_overlap_is_not_a_duplicate(conv: ModuleType) -> None:
    """Two PRs touching only board.json + docs are correlated, not duplicates."""
    empty = {"schema": 2, "claims": [], "zones": [], "deferred_log": [], "next_work": []}
    noise = [".agents/board.json", "docs/README.md", "docs/DECISION_LOG.md"]
    report = _report(
        conv,
        [
            _pr(41, branch="arena/a", files=[*noise, *STORAGE]),
            _pr(42, branch="arena/b", files=[*noise, *CLI]),
        ],
        board=empty,
    )
    assert report["duplicate_clusters"] == []
    assert _class_of(report, 41) != "DUPLICATE"
    assert any(
        r["relation"] == "conflicts_with" and {r["from"], r["to"]} == {41, 42}
        for r in report["relations"]
    )


def test_a_containing_newer_pr_supersedes_the_older_one(conv: ModuleType) -> None:
    empty = {"schema": 2, "claims": [], "zones": [], "deferred_log": [], "next_work": []}
    report = _report(
        conv,
        [
            _pr(51, branch="arena/old", files=TEMPORAL[:3], updated=NOW - timedelta(days=9)),
            _pr(52, branch="arena/new", files=TEMPORAL, updated=NOW),
        ],
        board=empty,
    )
    sup = [r for r in report["relations"] if r["relation"] == "supersedes"]
    assert sup and sup[0]["from"] == 52 and sup[0]["to"] == 51
    assert _class_of(report, 52) == "SUPERSEDING"
    row = next(r for r in report["prs"] if r["number"] == 51)
    assert row["superseded_by"] == 52
    assert _class_of(report, 51) == "STALE", "an explicitly superseded PR is stale"


def test_the_board_can_declare_supersession_explicitly(conv: ModuleType) -> None:
    report = _report(conv, [_pr(33, branch="arena/none", files=CLI)])
    sup = [r for r in report["relations"] if r["relation"] == "supersedes"]
    assert any(e["source"] == "board" for e in sup)
    assert any("task-9" in w for e in sup for w in e["witnesses"])


def test_staleness_needs_inactivity_plus_corroboration(conv: ModuleType) -> None:
    """Age alone never retires a PR; a still-updated old PR stays alive."""
    empty = {"schema": 2, "claims": [], "zones": [], "deferred_log": [], "next_work": []}
    old_but_active = _pr(
        61, branch="arena/old-active", files=STORAGE, created=NOW - timedelta(days=40), updated=NOW
    )
    dormant = _pr(
        62,
        branch="arena/dormant",
        files=CLI,
        base_sha=OTHER_SHA,
        created=NOW - timedelta(days=40),
        updated=NOW - timedelta(days=20),
    )
    report = _report(conv, [old_but_active, dormant], board=empty)
    assert _class_of(report, 61) != "STALE"
    assert _class_of(report, 62) == "STALE"


def test_a_pr_sharing_files_with_a_live_claim_is_dependent(conv: ModuleType) -> None:
    board = _board()
    board["claims"].append(
        {
            "task": "task-2",
            "status": "active",
            "zone": "temporal",
            "agent_branch": "arena/owner-2",
            "claimed_at": _iso(NOW - timedelta(hours=2)),
            "ttl_hours": 24,
            "gates_owner": False,
            "exclusive_paths": [],
            "prerequisites": [],
        }
    )
    report = _report(
        conv,
        [
            _pr(71, branch="arena/owner-1", files=TEMPORAL),
            _pr(72, branch="arena/owner-2", files=[TEMPORAL[0], "src/nexus_ai_agent/other.py"]),
        ],
        board=board,
    )
    assert _class_of(report, 72) == "DEPENDENT"
    row = next(r for r in report["prs"] if r["number"] == 72)
    assert row["conflicts_with"] == [71]


# --------------------------------------------------------------------------- #
# the dependency graph
# --------------------------------------------------------------------------- #
def test_a_real_prerequisite_becomes_depends_on_blocks_and_unlocks(conv: ModuleType) -> None:
    report = _report(
        conv,
        [
            _pr(80, branch="arena/owner-0", files=CLI),
            _pr(81, branch="arena/owner-1", files=TEMPORAL),
        ],
    )
    kinds = {(r["relation"], r["from"], r["to"]) for r in report["relations"]}
    assert ("depends_on", 81, 80) in kinds
    assert ("blocks", 80, 81) in kinds
    assert ("unlocks", 80, 81) in kinds
    order = report["recommended_merge_order"]
    assert order.index(80) < order.index(81), order


def test_an_invented_prerequisite_creates_no_edge(conv: ModuleType) -> None:
    """A fake dependency must not fabricate a merge order."""
    board = _board()
    board["claims"][0]["prerequisites"] = ["task-does-not-exist"]
    report = _report(conv, [_pr(81, branch="arena/owner-1", files=TEMPORAL)], board=board)
    assert [r for r in report["relations"] if r["relation"] == "depends_on"] == []


def test_a_removed_prerequisite_removes_the_edge(conv: ModuleType) -> None:
    board = _board()
    assert _report(
        conv,
        [
            _pr(80, branch="arena/owner-0", files=CLI),
            _pr(81, branch="arena/owner-1", files=TEMPORAL),
        ],
        board=board,
    )["relations"]
    board["claims"][0]["prerequisites"] = []
    report = _report(
        conv,
        [
            _pr(80, branch="arena/owner-0", files=CLI),
            _pr(81, branch="arena/owner-1", files=TEMPORAL),
        ],
        board=board,
    )
    assert [r for r in report["relations"] if r["relation"] in ("depends_on", "blocks")] == []


def test_a_dependency_cycle_is_reported_not_silently_ordered(conv: ModuleType) -> None:
    board = _board()
    board["claims"][0]["prerequisites"] = ["task-0"]
    board["claims"][1]["prerequisites"] = ["task-1"]
    report = _report(
        conv,
        [
            _pr(90, branch="arena/owner-0", files=CLI),
            _pr(91, branch="arena/owner-1", files=TEMPORAL),
        ],
        board=board,
    )
    assert report["dependency_cycles"], "a cycle must be named, never hidden by a fake order"
    assert sorted(report["recommended_merge_order"]) == [90, 91]


# --------------------------------------------------------------------------- #
# evidence freshness
# --------------------------------------------------------------------------- #
def test_base_drift_is_measured_against_the_live_head(conv: ModuleType) -> None:
    report = _report(conv, [_pr(100, branch="arena/owner-1", files=TEMPORAL, base_sha=OTHER_SHA)])
    row = next(r for r in report["prs"] if r["number"] == 100)
    assert row["evidence"]["base_drift"] is True
    assert any("base drift" in s for s in row["evidence"]["reasons"])


def test_without_a_live_head_drift_is_unknown_not_absent(conv: ModuleType) -> None:
    report = _report(conv, [_pr(101, branch="arena/owner-1", files=TEMPORAL)], live_main_sha=None)
    row = next(r for r in report["prs"] if r["number"] == 101)
    assert row["evidence"]["base_drift"] is False
    assert any("live main head was not supplied" in s for s in row["evidence"]["reasons"])
    assert row["evidence"]["verdict"] == "PARTIAL"


# --------------------------------------------------------------------------- #
# determinism, completeness, and the mandated mutations
# --------------------------------------------------------------------------- #
def test_the_report_is_deterministic(conv: ModuleType) -> None:
    records = [
        _pr(110, branch="arena/owner-1", files=TEMPORAL),
        _pr(111, branch="arena/other", files=TEMPORAL[:2] + CLI),
        _pr(
            112,
            branch="arena/third",
            files=STORAGE,
            base_sha=OTHER_SHA,
            updated=NOW - timedelta(days=30),
        ),
    ]
    a = json.dumps(_report(conv, records), sort_keys=True)
    b = json.dumps(_report(conv, list(reversed(records))), sort_keys=True)
    assert a == b, "input order must not change the report"


def test_no_input_pr_is_silently_ignored(conv: ModuleType) -> None:
    records = [
        _pr(120, branch="arena/a", files=TEMPORAL),
        _pr(121, branch="arena/b", files=CLI),
        _pr(122, branch="arena/c", files=[], complete=False),
    ]
    report = _report(conv, records)
    assert report["pr_count"] == 3
    assert sorted(r["number"] for r in report["prs"]) == [120, 121, 122]
    assert sum(report["class_summary"].values()) == 3
    for name in conv.CLASSES:
        assert name in report["class_summary"]


def test_duplicate_input_is_refused_not_merged(conv: ModuleType) -> None:
    with pytest.raises(ValueError):
        _report(conv, [_pr(130, branch="a", files=TEMPORAL), _pr(130, branch="b", files=CLI)])


def test_disabling_the_duplicate_detector_is_observable(
    conv: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: a disabled detector must change the report, or the tests are blind."""
    records = [
        _pr(140, branch="arena/dup-a", files=TEMPORAL),
        _pr(141, branch="arena/dup-b", files=TEMPORAL),
    ]
    empty = {"schema": 2, "claims": [], "zones": [], "deferred_log": [], "next_work": []}
    assert _report(conv, records, board=empty)["duplicate_clusters"] == [[140, 141]]
    monkeypatch.setattr(conv, "duplicate_evidence", lambda *a, **k: None)
    mutated = _report(conv, records, board=empty)
    assert mutated["duplicate_clusters"] == []
    assert _class_of(mutated, 140) != "DUPLICATE"


def test_removing_the_supersession_edge_is_observable(
    conv: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = {"schema": 2, "claims": [], "zones": [], "deferred_log": [], "next_work": []}
    records = [
        _pr(150, branch="arena/old", files=TEMPORAL[:3], updated=NOW - timedelta(days=9)),
        _pr(151, branch="arena/new", files=TEMPORAL, updated=NOW),
    ]
    assert _report(conv, records, board=empty)["relations"]
    monkeypatch.setattr(conv, "supersession_edges", lambda *a, **k: [])
    mutated = _report(conv, records, board=empty)
    assert [r for r in mutated["relations"] if r["relation"] == "supersedes"] == []
    assert _class_of(mutated, 151) != "SUPERSEDING"


def test_accepting_stale_evidence_is_observable(
    conv: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: treating a truncated file list as complete must be caught."""
    record = _pr(160, branch="arena/truncated", files=TEMPORAL, complete=False)
    assert _class_of(_report(conv, [record]), 160) == "BLOCKED"
    # BLOCKED is defended twice — by the freshness verdict *and* by the raw
    # evidence gaps — so both gates must be removed before stale evidence can
    # slip through.  That redundancy is the point; this proves it is real.
    monkeypatch.setattr(
        conv, "assess_freshness", lambda pr, sha, now, th: conv.Freshness("FRESH", False, ())
    )
    assert _class_of(_report(conv, [record]), 160) == "BLOCKED", (
        "the freshness verdict alone must not be the only gate"
    )
    monkeypatch.setattr(conv.PR, "missing_evidence", lambda self: [])
    assert _class_of(_report(conv, [record]), 160) != "BLOCKED"


def test_ignoring_an_unknown_pr_is_observable(
    conv: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: an input PR that vanishes from the report must be detectable.

    The completeness assertion in ``test_no_input_pr_is_silently_ignored``
    compares against the *input* numbers, so corrupting one PR's identity here
    makes that comparison fail — which is the point: a dropped PR is loud.
    """
    records = [
        _pr(170, branch="arena/a", files=TEMPORAL),
        _pr(171, branch="arena/b", files=CLI),
    ]
    original = conv.pr_from_record

    def corrupted(record: dict):
        mutated = dict(record)
        if mutated["number"] == 171:
            mutated["number"] = 172
        return original(mutated)

    monkeypatch.setattr(conv, "pr_from_record", corrupted)
    numbers = sorted(r["number"] for r in _report(conv, records)["prs"])
    assert 171 not in numbers, "the mutated PR disappeared from the report"
    assert numbers == [170, 172]
    assert sorted(r["number"] for r in records) == [170, 171], (
        "the report no longer covers every input PR — this is exactly what the "
        "completeness assertion catches"
    )


def test_thresholds_are_parameters_not_hard_coded(conv: ModuleType) -> None:
    """Raising the duplicate floor must dissolve a borderline cluster."""
    empty = {"schema": 2, "claims": [], "zones": [], "deferred_log": [], "next_work": []}
    records = [
        _pr(180, branch="arena/a", files=TEMPORAL),
        _pr(181, branch="arena/b", files=[*TEMPORAL, "src/nexus_ai_agent/continuum/x.py"]),
    ]
    assert _report(conv, records, board=empty)["duplicate_clusters"] == [[180, 181]]
    strict = conv.Thresholds(**{**conv.Thresholds().to_dict(), "duplicate_confidence": 0.99})
    assert _report(conv, records, board=empty, thresholds=strict)["duplicate_clusters"] == []


def test_the_report_carries_the_thresholds_it_used(conv: ModuleType) -> None:
    report = _report(conv, [_pr(190, branch="arena/owner-1", files=TEMPORAL)])
    assert report["thresholds"]["inactive_days"] == 7
    assert report["thresholds"]["duplicate_confidence"] == pytest.approx(0.60)
    assert report["generated_at_utc"] == _iso(NOW)


# --------------------------------------------------------------------------- #
# live_main_sha — the live CLI entry point must never traceback
# --------------------------------------------------------------------------- #
# Reproduced: ``agent_board._gh_get`` returns the decoded JSON object, not a
# ``(value, error)`` pair, so ``ref, _err = _gh_get(...)`` raised
# ``ValueError: too many values to unpack`` on every *successful* response.  It
# is called from ``main`` outside any ``try``, so the documented command
# ``pr_convergence.py --repo owner/name`` died with a traceback instead of the
# contracted EXIT_BLOCKED.
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"ref": "refs/heads/main", "object": {"sha": "abc123", "type": "commit"}}, "abc123"),
        ({"ref": "refs/heads/main"}, None),  # missing object
        ({"ref": "r", "object": {"type": "commit"}}, None),  # object without sha
        ({"ref": "r", "object": "not-a-dict"}, None),  # wrong shape
        ({"ref": "r", "object": None}, None),
        ([1, 2, 3], None),  # a list where an object belongs
        ("nope", None),
        (None, None),
    ],
)
def test_live_main_sha_reads_the_response_contract(
    conv: ModuleType, monkeypatch: pytest.MonkeyPatch, response: object, expected: str | None
) -> None:
    monkeypatch.setattr(conv, "_gh_get", lambda url, token: response)
    assert conv.live_main_sha("o/r", None) == expected


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.HTTPError("u", 404, "Not Found", {}, None),
        urllib.error.URLError("dns failure"),
        OSError("connection reset"),
        TimeoutError("timed out"),
        ValueError("Expecting value: line 1 column 1"),
    ],
)
def test_live_main_sha_converts_transport_failure_to_none(
    conv: ModuleType, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    def boom(url: str, token: str | None) -> object:
        raise exc

    monkeypatch.setattr(conv, "_gh_get", boom)
    assert conv.live_main_sha("o/r", None) is None


def test_live_main_sha_does_not_unpack_a_tuple(conv: ModuleType) -> None:
    """Pin the contract itself, so the original defect cannot come back.

    The bug was reading ``_gh_get`` as ``(value, error)``.  A tuple response is
    not something GitHub returns, so it must be treated as malformed rather than
    destructured.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    assert "ref, _err = _gh_get" not in source, "reverted to the tuple-unpacking contract"


def test_the_cli_blocks_rather_than_crashing_when_main_is_unreadable(
    conv: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The whole documented live path, with the network failing.

    Acceptance: no traceback, no false VERIFIED — the contracted EXIT_BLOCKED.
    """

    def boom(url: str, token: str | None) -> object:
        raise urllib.error.URLError("network unreachable")

    monkeypatch.setattr(conv, "_gh_get", boom)
    board = tmp_path / "board.json"
    board.write_text(json.dumps({"schema": 2, "claims": []}), encoding="utf-8")
    code = conv.main(["--repo", "o/r", "--board", str(board)])
    assert code == conv.EXIT_BLOCKED
    assert "Traceback" not in capsys.readouterr().err


def test_the_cli_reports_blocked_when_the_ref_payload_is_malformed(
    conv: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP 200 with a body that is not a ref object must not invent a SHA."""

    def fake(url: str, token: str | None) -> object:
        if "/git/ref/" in url:
            return {"unexpected": "shape"}
        return []  # the open-PR list

    monkeypatch.setattr(conv, "_gh_get", fake)
    board = tmp_path / "board.json"
    board.write_text(json.dumps({"schema": 2, "claims": []}), encoding="utf-8")
    assert conv.main(["--repo", "o/r", "--board", str(board)]) == conv.EXIT_BLOCKED
