"""The PR convergence registry is infrastructure for a merge campaign — so it is tested.

The 2026-10-25 incident this file guards against: a prior session reported PR
state from memory ("2 verified ready", "30/30 patch sets merge-ready", "merge is
impossible: 0 write credentials").  A live read showed 69 open PRs, 46 with real
conflicts, and a merge that worked on the first attempt.  Both halves of that
failure are pinned here:

* :func:`pr_convergence.blockers` must never report "ready" for a PR that
  GitHub will refuse to merge (the classification bug that turns into a plan
  that cannot execute), and
* the tier listing must be **total** — every PR lands in a bucket, including the
  zero-count ones, because a category that disappears from the report is how
  "30/30 ready" survived three rounds of review.

The module is loaded by path (it lives in ``scripts/``, is stdlib-only, and is
executed directly by CI), the same pattern as
``tests/unit/test_board_integrity_gate.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "pr_convergence.py"

_spec = importlib.util.spec_from_file_location("pr_convergence", SCRIPT)
assert _spec and _spec.loader
registry = importlib.util.module_from_spec(_spec)
sys.modules["pr_convergence"] = registry
_spec.loader.exec_module(registry)


def _pr(**overrides: Any) -> dict[str, Any]:
    """A PR that is green, approved, main-bound, up to date — i.e. mergeable."""
    record: dict[str, Any] = {
        "baseRefName": "main",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "APPROVED",
        "headRefOid": "a" * 40,
        "statusCheckRollup": {"state": "SUCCESS"},
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# the deadlock this tool exists to make visible
# ---------------------------------------------------------------------------
def test_green_but_unapproved_is_not_ready() -> None:
    """A green PR without an approval is blocked, however perfect its checks are."""
    pr = _pr(reviewDecision="REVIEW_REQUIRED", mergeStateStatus="BLOCKED")
    assert registry.blockers(pr) == ["NEEDS_APPROVAL"]
    assert registry.classify(pr) == "NEEDS_APPROVAL"


def test_needs_approval_is_distinct_from_changes_requested() -> None:
    """Opposite remedies: one needs a reviewer, the other needs the author.

    Collapsing them is what produced a merge plan with no executable step, so
    the two codes must never be the same string.
    """
    assert "NEEDS_APPROVAL" in registry.BLOCKERS
    assert "CHANGES_REQUESTED" in registry.BLOCKERS
    assert registry.BLOCKERS.index("NEEDS_APPROVAL") != registry.BLOCKERS.index("CHANGES_REQUESTED")
    assert registry.classify(_pr(reviewDecision="CHANGES_REQUESTED")) == "CHANGES_REQUESTED"


def test_fully_green_approved_pr_is_ready() -> None:
    assert registry.blockers(_pr()) == []
    assert registry.classify(_pr()) == "READY"


# ---------------------------------------------------------------------------
# hard blockers outrank soft ones
# ---------------------------------------------------------------------------
def test_wrong_base_outranks_every_other_blocker() -> None:
    pr = _pr(baseRefName="arena/other", mergeable="CONFLICTING", isDraft=True)
    assert registry.classify(pr) == "WRONG_BASE_BRANCH"


def test_draft_outranks_conflict() -> None:
    pr = _pr(isDraft=True, mergeable="CONFLICTING")
    assert registry.classify(pr) == "DRAFT"


def test_conflict_outranks_red_ci() -> None:
    pr = _pr(mergeable="CONFLICTING", statusCheckRollup={"state": "FAILURE"})
    assert registry.classify(pr) == "CONFLICT"


def test_red_ci_is_detected_from_the_rollup_state() -> None:
    for state in ("FAILURE", "ERROR"):
        pr = _pr(statusCheckRollup={"state": state})
        assert registry.classify(pr) == "CI_RED", state


def test_pending_ci_is_not_treated_as_a_pass() -> None:
    for state in ("PENDING", "EXPECTED", "NONE"):
        pr = _pr(statusCheckRollup={"state": state})
        assert registry.classify(pr) == "CI_PENDING", state


def test_behind_head_requires_a_rebase_even_when_everything_else_is_green() -> None:
    """`BEHIND` means CI never ran on the current base — merge on this is a guess."""
    pr = _pr(mergeStateStatus="BEHIND")
    assert registry.blockers(pr) == ["REBASE_FIRST"]
    assert registry.classify(pr) == "REBASE_FIRST"


def test_blockers_accumulate_in_priority_order() -> None:
    pr = _pr(mergeable="CONFLICTING", statusCheckRollup={"state": "FAILURE"}, reviewDecision="")
    assert registry.blockers(pr) == ["CONFLICT", "CI_RED", "NEEDS_APPROVAL"]


# ---------------------------------------------------------------------------
# the pre-merge guard must fail closed
# ---------------------------------------------------------------------------
def test_guard_passes_a_green_approved_pr_with_the_expected_sha() -> None:
    code, reasons = registry.check_guard(_pr(), expected_sha="a" * 12)
    assert (code, reasons) == (0, [])


def test_guard_rejects_a_moved_head() -> None:
    """Pinning the reviewed SHA is the whole point: a moved head is not a pass."""
    code, reasons = registry.check_guard(_pr(), expected_sha="b" * 12)
    assert code == 1
    assert any("head moved" in reason for reason in reasons)


def test_guard_explains_the_self_approval_deadlock() -> None:
    code, reasons = registry.check_guard(_pr(reviewDecision="REVIEW_REQUIRED"))
    assert code == 1
    assert any("never be approved by its own author" in reason for reason in reasons)


def test_guard_rejects_a_stale_base() -> None:
    code, reasons = registry.check_guard(_pr(mergeStateStatus="BEHIND"))
    assert code == 1
    assert any("BEHIND" in reason for reason in reasons)


# ---------------------------------------------------------------------------
# totals must be total
# ---------------------------------------------------------------------------
def test_every_tier_is_counted_including_the_zeroes() -> None:
    counts = registry.summarise([_pr(), _pr(mergeable="CONFLICTING")])
    assert set(counts) == set(registry.TIERS)
    assert counts["READY"] == 1
    assert counts["CONFLICT"] == 1
    assert counts["DRAFT"] == 0


def test_tier_list_is_blockers_plus_ready_in_one_place() -> None:
    assert registry.TIERS == (*registry.BLOCKERS, "READY")


def test_report_renders_every_tier_and_every_pr() -> None:
    built = {
        "schema": registry.SCHEMA,
        "repo": "bot523h/nexus-ai-agent",
        "generated_at": "2026-10-25T00:00:00Z",
        "main_sha": "c" * 40,
        "open_total": 2,
        "totals": registry.summarise([_pr(), _pr(isDraft=True)]),
        "blockers": {code: 0 for code in registry.BLOCKERS},
        "prs": [
            {
                "number": 1,
                "title": "green",
                "base": "main",
                "ci_state": "SUCCESS",
                "review_decision": "APPROVED",
                "changed_files": 1,
                "additions": 1,
                "deletions": 0,
                "tier": "READY",
            },
            {
                "number": 2,
                "title": "draft",
                "base": "main",
                "ci_state": "SUCCESS",
                "review_decision": "",
                "changed_files": 1,
                "additions": 1,
                "deletions": 0,
                "tier": "DRAFT",
            },
        ],
    }
    text = registry.render_report(built)
    for tier in registry.TIERS:
        assert f"`{tier}`" in text, tier
    assert "#1" in text and "#2" in text


def test_registry_schema_is_versioned() -> None:
    """A committed artifact must announce the shape a reader can parse."""
    assert registry.SCHEMA.startswith("nexus-pr-registry/v")
