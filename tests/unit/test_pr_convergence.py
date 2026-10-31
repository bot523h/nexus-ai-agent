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
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "pr_convergence.py"

_spec = importlib.util.spec_from_file_location("pr_convergence", SCRIPT)
assert _spec and _spec.loader
registry = importlib.util.module_from_spec(_spec)
sys.modules["pr_convergence"] = registry
_spec.loader.exec_module(registry)


#: The real required contexts of this repository's ``main`` protection, read
#: live from the branch-protection API.  Tests pin the guard against *these*
#: names so a silent rename of a required check cannot make the guard pass.
REQUIRED_CONTEXTS = [
    "lint (ruff + mypy + version lockstep)",
    'test (pytest -m "not slow")',
    "merge-base-guard (base == main)",
]

#: A protection payload with three required contexts and **no** required review
#: — the actual gate on this repository.  A guard that demanded a review would
#: wrongly report mergeable PRs as BLOCKED.
PROTECTION = {
    "required_status_checks": {"strict": True, "contexts": list(REQUIRED_CONTEXTS)},
    "required_pull_request_reviews": None,
}

#: A protection payload that *does* require one approval (for the review tests).
PROTECTION_WITH_REVIEW = {
    "required_status_checks": {"strict": True, "contexts": list(REQUIRED_CONTEXTS)},
    "required_pull_request_reviews": {"required_approving_review_count": 1},
}


def _green_contexts() -> list[dict[str, str]]:
    return [{"name": name, "conclusion": "SUCCESS"} for name in REQUIRED_CONTEXTS]


def _pr(**overrides: Any) -> dict[str, Any]:
    """A PR that is green, main-bound, up to date — i.e. mergeable."""
    record: dict[str, Any] = {
        "baseRefName": "main",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "",
        "headRefOid": "a" * 40,
        "statusCheckRollup": {"state": "SUCCESS", "contexts": _green_contexts()},
    }
    record.update(overrides)
    return record


def _guard(pr: dict[str, Any], **kwargs: Any) -> tuple[int, list[str]]:
    kwargs.setdefault("protection", PROTECTION)
    return registry.check_guard(pr, **kwargs)


# ---------------------------------------------------------------------------
# the deadlock this tool exists to make visible
# ---------------------------------------------------------------------------
def test_green_but_unapproved_is_not_ready() -> None:
    """When a repo *does* require approval, a green PR without one is blocked."""
    pr = _pr(reviewDecision="REVIEW_REQUIRED", mergeStateStatus="BLOCKED")
    assert registry.blockers(pr, required_approvals=1) == ["NEEDS_APPROVAL"]
    assert registry.classify(pr, required_approvals=1) == "NEEDS_APPROVAL"


def test_a_repo_requiring_no_approval_does_not_invent_one() -> None:
    """This repository requires 0 approvals; a green PR must be READY, not blocked.

    The mirror of the original bug: a gate that does not exist reported as if it
    did.  Classification must follow the repository's live requirement.
    """
    pr = _pr(reviewDecision="REVIEW_REQUIRED")
    assert registry.blockers(pr, required_approvals=0) == []
    assert registry.classify(pr, required_approvals=0) == "READY"


def test_needs_approval_is_distinct_from_changes_requested() -> None:
    """Opposite remedies: one needs a reviewer, the other needs the author.

    Collapsing them is what produced a merge plan with no executable step, so
    the two codes must never be the same string.
    """
    assert "NEEDS_APPROVAL" in registry.BLOCKERS
    assert "CHANGES_REQUESTED" in registry.BLOCKERS
    assert registry.BLOCKERS.index("NEEDS_APPROVAL") != registry.BLOCKERS.index("CHANGES_REQUESTED")
    assert registry.classify(_pr(reviewDecision="CHANGES_REQUESTED")) == "CHANGES_REQUESTED"


def test_changes_requested_blocks_even_when_no_approval_is_required() -> None:
    """A requested change is always a blocker — it is work for the author."""
    pr = _pr(reviewDecision="CHANGES_REQUESTED")
    assert registry.classify(pr, required_approvals=0) == "CHANGES_REQUESTED"


def test_fully_green_pr_is_ready() -> None:
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
    assert registry.blockers(pr, required_approvals=1) == ["CONFLICT", "CI_RED", "NEEDS_APPROVAL"]


# ---------------------------------------------------------------------------
# the pre-merge guard must fail closed
# ---------------------------------------------------------------------------
def test_guard_passes_a_green_pr_with_the_expected_sha() -> None:
    code, reasons = _guard(_pr(), expected_sha="a" * 12)
    assert (code, reasons) == (0, [])


def test_guard_rejects_a_moved_head() -> None:
    """Pinning the reviewed SHA is the whole point: a moved head is not a pass."""
    code, reasons = _guard(_pr(), expected_sha="b" * 12)
    assert code == 1
    assert any("head moved" in reason for reason in reasons)


def test_guard_blocks_on_a_missing_required_context() -> None:
    """A required check absent from the head is not a pass, however green the rest."""
    contexts = [c for c in _green_contexts() if c["name"] != "merge-base-guard (base == main)"]
    pr = _pr(statusCheckRollup={"state": "SUCCESS", "contexts": contexts})
    code, reasons = _guard(pr)
    assert code == 1
    assert any("required checks missing" in reason for reason in reasons)


def test_guard_blocks_on_a_red_required_context() -> None:
    contexts = _green_contexts()
    contexts[0] = {"name": contexts[0]["name"], "conclusion": "FAILURE"}
    pr = _pr(statusCheckRollup={"state": "FAILURE", "contexts": contexts})
    code, reasons = _guard(pr)
    assert code == 1
    assert any("not green" in reason for reason in reasons)


def test_guard_ignores_a_non_required_context_that_is_skipped() -> None:
    """A non-required job that is SKIPPED (e.g. merge-base-guard on a push) is fine."""
    contexts = _green_contexts() + [{"name": "extras-matrix (speech)", "conclusion": "SKIPPED"}]
    pr = _pr(statusCheckRollup={"state": "SUCCESS", "contexts": contexts})
    code, reasons = _guard(pr)
    assert (code, reasons) == (0, [])


def test_guard_uses_the_latest_run_of_a_required_context_not_a_superseded_one() -> None:
    """A re-run cancels the old attempt; the newest run decides the gate.

    GitHub's required-check evaluation looks at the *most recent* run of each
    context.  A guard that let the earlier ``CANCELLED`` veto the later
    ``SUCCESS`` reported a genuinely mergeable head as BLOCKED (observed live on
    PR #263, whose lint/test were re-run and cancelled the first attempt).
    """
    contexts = _green_contexts() + [
        {
            "name": REQUIRED_CONTEXTS[0],
            "conclusion": "CANCELLED",
            "completedAt": "2026-10-28T05:41:39Z",
        },
        {
            "name": REQUIRED_CONTEXTS[0],
            "conclusion": "SUCCESS",
            "completedAt": "2026-10-28T05:48:52Z",
        },
    ]
    pr = _pr(statusCheckRollup={"state": "SUCCESS", "contexts": contexts})
    code, reasons = _guard(pr)
    assert (code, reasons) == (0, [])


def test_guard_still_fails_when_the_latest_run_of_a_required_context_is_red() -> None:
    """Latest-wins must not become last-listed-wins in the unsafe direction."""
    contexts = _green_contexts() + [
        {
            "name": REQUIRED_CONTEXTS[0],
            "conclusion": "SUCCESS",
            "completedAt": "2026-10-28T05:41:39Z",
        },
        {
            "name": REQUIRED_CONTEXTS[0],
            "conclusion": "FAILURE",
            "completedAt": "2026-10-28T05:48:52Z",
        },
    ]
    pr = _pr(statusCheckRollup={"state": "FAILURE", "contexts": contexts})
    code, reasons = _guard(pr)
    assert code == 1
    assert any("not green" in reason for reason in reasons)


def test_guard_latest_run_falls_back_to_order_without_timestamps() -> None:
    """Without timestamps, the REST reader's oldest→newest order decides."""
    contexts = _green_contexts() + [
        {"name": REQUIRED_CONTEXTS[0], "conclusion": "CANCELLED"},
        {"name": REQUIRED_CONTEXTS[0], "conclusion": "SUCCESS"},
    ]
    pr = _pr(statusCheckRollup={"state": "SUCCESS", "contexts": contexts})
    assert _guard(pr) == (0, [])


def test_guard_ignores_a_missing_review_when_none_is_required() -> None:
    """This repo requires 0 approvals; a green PR must PASS without a review."""
    code, reasons = _guard(_pr(reviewDecision="REVIEW_REQUIRED"))
    assert (code, reasons) == (0, [])


def test_guard_explains_the_self_approval_deadlock_when_a_review_is_required() -> None:
    code, reasons = _guard(_pr(reviewDecision="REVIEW_REQUIRED"), protection=PROTECTION_WITH_REVIEW)
    assert code == 1
    assert any("never be approved by its own author" in reason for reason in reasons)


def test_required_contexts_refuses_an_unreadable_protection() -> None:
    """Unreadable protection is not 'no gate' — it must raise, and guard exits 2."""
    with pytest.raises(RuntimeError, match="unreadable"):
        registry.required_contexts(None)


def test_required_contexts_reads_names_and_approvals_from_the_payload() -> None:
    contexts, approvals = registry.required_contexts(PROTECTION_WITH_REVIEW)
    assert contexts == REQUIRED_CONTEXTS
    assert approvals == 1
    assert registry.required_contexts(PROTECTION) == (REQUIRED_CONTEXTS, 0)


def test_guard_rejects_a_stale_base() -> None:
    code, reasons = _guard(_pr(mergeStateStatus="BEHIND"))
    assert code == 1
    assert any("BEHIND" in reason for reason in reasons)


def test_guard_rejects_unknown_merge_state() -> None:
    code, reasons = _guard(_pr(mergeStateStatus="UNKNOWN"))
    assert code == 1
    assert any("expected 'CLEAN'" in reason for reason in reasons)


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
    # A committed 13:33 v1 snapshot predates MERGEABILITY_UNKNOWN. Reporting
    # remains backward-compatible; the absent category is rendered as zero.
    built["totals"].pop("MERGEABILITY_UNKNOWN", None)
    built["blockers"].pop("MERGEABILITY_UNKNOWN", None)
    text = registry.render_report(built)
    for tier in registry.TIERS:
        assert f"`{tier}`" in text, tier
    assert "#1" in text and "#2" in text


def test_registry_schema_is_versioned() -> None:
    """A committed artifact must announce the shape a reader can parse."""
    assert registry.SCHEMA.startswith("nexus-pr-registry/v")


# ---------------------------------------------------------------------------
# mergeability is computed asynchronously — "not computed yet" is not "clean"
# ---------------------------------------------------------------------------
def test_unknown_mergeability_is_its_own_blocker_not_a_pass() -> None:
    """The 2026-10-25 regression: after main moved twice, 66 of 68 PRs reported
    ``mergeable: UNKNOWN`` and the registry first printed ``CONFLICT: 0``.

    A snapshot of an unmeasured backlog is not an empty backlog, so UNKNOWN must
    be a blocker in its own right, ranked with the hard structural ones.
    """
    pr = _pr(mergeable="UNKNOWN", mergeStateStatus="UNKNOWN")
    assert "MERGEABILITY_UNKNOWN" in registry.blockers(pr)
    assert registry.classify(pr) == "MERGEABILITY_UNKNOWN"
    assert registry.BLOCKERS.index("MERGEABILITY_UNKNOWN") < registry.BLOCKERS.index("CI_RED")


def test_unknown_mergeability_outranks_a_missing_approval() -> None:
    """A PR we cannot even measure must not be filed as merely 'needs a review'."""
    pr = _pr(mergeable="UNKNOWN", reviewDecision="REVIEW_REQUIRED")
    assert registry.classify(pr) == "MERGEABILITY_UNKNOWN"


def test_unknown_mergeability_does_not_hide_a_real_conflict() -> None:
    pr = _pr(mergeable="CONFLICTING")
    assert registry.classify(pr) == "CONFLICT"


def test_unknown_merge_state_is_not_treated_as_behind() -> None:
    pr = _pr(mergeStateStatus="UNKNOWN", mergeable="MERGEABLE")
    assert "REBASE_FIRST" not in registry.blockers(pr)


def test_guard_refuses_to_merge_while_mergeability_is_unknown() -> None:
    code, reasons = _guard(_pr(mergeable="UNKNOWN"))
    assert code == 1
    assert any("not computed" in reason for reason in reasons)


def test_aggregate_fails_closed_on_an_unclassified_check() -> None:
    assert registry._aggregate([{"conclusion": "SUCCESS"}, {}]) == "PENDING"


def test_aggregate_fails_closed_on_an_empty_check_list() -> None:
    assert registry._aggregate([]) == "NONE"


def test_guard_accepts_case_insensitive_sha_prefix() -> None:
    code, reasons = _guard(_pr(), expected_sha="A" * 12)
    assert (code, reasons) == (0, [])


@pytest.mark.parametrize("expected_sha", ["", "a" * 6, "z" * 12, "a" * 41])
def test_guard_rejects_invalid_expected_sha_pins(expected_sha: str) -> None:
    code, reasons = _guard(_pr(), expected_sha=expected_sha)
    assert code == 1
    assert any("7–40 hexadecimal" in reason for reason in reasons)


def test_aggregate_treats_startup_failure_as_failure() -> None:
    assert registry._aggregate([{"conclusion": "STARTUP_FAILURE"}]) == "FAILURE"


def test_aggregate_treats_stale_as_failure() -> None:
    assert registry._aggregate([{"conclusion": "STALE"}]) == "FAILURE"


@pytest.mark.parametrize("unknown", ["SOME_NEW_STATE", "STALE_UNKNOWN", "ACTION_REQUIRED_NEW"])
def test_aggregate_never_turns_unknown_conclusions_into_success(unknown: str) -> None:
    assert registry._aggregate([{"conclusion": "SUCCESS"}, {"conclusion": unknown}]) == "PENDING"


def test_aggregate_returns_success_only_when_every_conclusion_is_recognized() -> None:
    assert (
        registry._aggregate(
            [{"conclusion": "SUCCESS"}, {"conclusion": "SKIPPED"}, {"state": "PASS"}]
        )
        == "SUCCESS"
    )


def test_graphql_rejects_a_non_object_response() -> None:
    with patch.object(registry, "_gh", return_value=json.dumps(["not", "an object"])):
        with pytest.raises(RuntimeError, match="non-object"):
            registry._graphql("query { viewer { login } }", {})


def test_graphql_rejects_top_level_errors() -> None:
    response = {"errors": [{"message": "rate limit"}], "data": None}
    with patch.object(registry, "_gh", return_value=json.dumps(response)):
        with pytest.raises(RuntimeError, match="GraphQL returned errors"):
            registry._graphql("query { viewer { login } }", {})


def test_graphql_returns_a_valid_payload_unchanged() -> None:
    response = {"data": {"viewer": {"login": "bot523h"}}}
    with patch.object(registry, "_gh", return_value=json.dumps(response)):
        assert registry._graphql("query { viewer { login } }", {}) == response


# ---------------------------------------------------------------------------
# M14 defect A — the registry must judge the LATEST run of each required
# context, not the coarse rollup, which an older superseded run poisons.
# Reproduced live on #287 and #289 (2026-10-30): every required context had an
# older CANCELLED next to a newer SUCCESS, yet both were classified CI_RED.
# ---------------------------------------------------------------------------
def _required(name: str, conclusion: str, completed: str) -> dict[str, str]:
    return {"name": name, "conclusion": conclusion, "completedAt": completed}


def test_classify_uses_latest_required_context_not_coarse_rollup() -> None:
    """The exact #287/#289 shape: coarse rollup FAILURE, every latest run green."""
    contexts = [
        _required(REQUIRED_CONTEXTS[0], "CANCELLED", "2026-10-30T01:40:12Z"),
        _required(REQUIRED_CONTEXTS[0], "SUCCESS", "2026-10-30T01:49:43Z"),
        _required(REQUIRED_CONTEXTS[1], "CANCELLED", "2026-10-30T01:40:27Z"),
        _required(REQUIRED_CONTEXTS[1], "SUCCESS", "2026-10-30T02:01:44Z"),
        _required(REQUIRED_CONTEXTS[2], "CANCELLED", "2026-10-30T01:40:12Z"),
        _required(REQUIRED_CONTEXTS[2], "SUCCESS", "2026-10-30T01:41:50Z"),
    ]
    pr = _pr(statusCheckRollup={"state": "FAILURE", "contexts": contexts})
    assert registry.classify(pr, required_contexts=REQUIRED_CONTEXTS) == "READY"
    assert registry.blockers(pr, required_contexts=REQUIRED_CONTEXTS) == []


def test_classify_latest_failure_after_older_success_is_red() -> None:
    """Latest-wins must not become last-listed-wins in the unsafe direction."""
    contexts = _green_contexts() + [
        _required(REQUIRED_CONTEXTS[1], "SUCCESS", "2026-10-28T05:41:39Z"),
        _required(REQUIRED_CONTEXTS[1], "FAILURE", "2026-10-28T05:48:52Z"),
    ]
    pr = _pr(statusCheckRollup={"state": "SUCCESS", "contexts": contexts})
    assert registry.classify(pr, required_contexts=REQUIRED_CONTEXTS) == "CI_RED"


def test_classify_old_failure_then_new_success_is_ready() -> None:
    """PR #113's shape: an older FAILURE superseded by a newer SUCCESS."""
    contexts = _green_contexts() + [
        _required(REQUIRED_CONTEXTS[1], "FAILURE", "2026-09-27T21:06:42Z"),
        _required(REQUIRED_CONTEXTS[1], "SUCCESS", "2026-09-27T21:10:41Z"),
    ]
    pr = _pr(statusCheckRollup={"state": "FAILURE", "contexts": contexts})
    assert registry.classify(pr, required_contexts=REQUIRED_CONTEXTS) == "READY"


def test_classify_old_cancelled_then_new_success_is_ready() -> None:
    contexts = _green_contexts() + [
        _required(REQUIRED_CONTEXTS[0], "CANCELLED", "2026-10-30T01:40:12Z"),
        _required(REQUIRED_CONTEXTS[0], "SUCCESS", "2026-10-30T01:49:43Z"),
    ]
    pr = _pr(statusCheckRollup={"state": "FAILURE", "contexts": contexts})
    assert registry.classify(pr, required_contexts=REQUIRED_CONTEXTS) == "READY"


def test_classify_is_pending_while_a_required_run_is_in_flight() -> None:
    contexts = _green_contexts() + [
        _required(REQUIRED_CONTEXTS[0], "SUCCESS", "2026-10-30T01:40:12Z"),
        _required(REQUIRED_CONTEXTS[0], "IN_PROGRESS", "2026-10-30T01:49:43Z"),
    ]
    pr = _pr(statusCheckRollup={"state": "PENDING", "contexts": contexts})
    assert registry.classify(pr, required_contexts=REQUIRED_CONTEXTS) == "CI_PENDING"


def test_classify_duplicate_context_names_use_the_latest() -> None:
    contexts = [
        _required(REQUIRED_CONTEXTS[0], "FAILURE", "2026-10-30T01:00:00Z"),
        _required(REQUIRED_CONTEXTS[0], "FAILURE", "2026-10-30T02:00:00Z"),
        _required(REQUIRED_CONTEXTS[0], "SUCCESS", "2026-10-30T03:00:00Z"),
    ] + _green_contexts()[1:]
    pr = _pr(statusCheckRollup={"state": "FAILURE", "contexts": contexts})
    assert registry.classify(pr, required_contexts=REQUIRED_CONTEXTS) == "READY"


def test_classify_latest_without_timestamps_falls_back_to_order() -> None:
    contexts = _green_contexts() + [
        {"name": REQUIRED_CONTEXTS[0], "conclusion": "CANCELLED"},
        {"name": REQUIRED_CONTEXTS[0], "conclusion": "SUCCESS"},
    ]
    pr = _pr(statusCheckRollup={"state": "FAILURE", "contexts": contexts})
    assert registry.classify(pr, required_contexts=REQUIRED_CONTEXTS) == "READY"


def test_classify_missing_required_context_is_not_a_pass() -> None:
    """A required context that never ran on this head can NEVER be inferred green.

    This is the Sourcery-flagged defect: judging only the contexts that happen to
    be present certified a head ``READY`` while a required check had not reported.
    With live per-context data present, a missing required context is PENDING.
    """
    contexts = [_required(REQUIRED_CONTEXTS[0], "SUCCESS", "2026-10-30T03:00:00Z")]
    pr = _pr(statusCheckRollup={"state": "SUCCESS", "contexts": contexts})
    assert registry._latest_ci_verdict(pr, REQUIRED_CONTEXTS) == "PENDING"
    assert registry.classify(pr, required_contexts=REQUIRED_CONTEXTS) == "CI_PENDING"


def test_legacy_snapshot_without_context_data_uses_the_coarse_rollup() -> None:
    """The explicit legacy contract: no per-context list at all -> rollup decides."""
    pr_no = _pr(statusCheckRollup={"state": "FAILURE"})
    assert registry._latest_ci_verdict(pr_no, REQUIRED_CONTEXTS) == "FAILURE"
    pr_ok = _pr(statusCheckRollup={"state": "SUCCESS"})
    assert registry._latest_ci_verdict(pr_ok, REQUIRED_CONTEXTS) == "SUCCESS"


# --- M15 Phase 2B: the ten mandatory missing-context / latest-run cases ---------
def test_m15_one_of_three_required_contexts_missing_is_pending() -> None:
    pr = _pr(
        statusCheckRollup={
            "state": "SUCCESS",
            "contexts": [_required(REQUIRED_CONTEXTS[0], "SUCCESS", "2026-10-30T10:00:00Z")],
        }
    )
    assert registry.classify(pr, required_contexts=REQUIRED_CONTEXTS) == "CI_PENDING"


def test_m15_two_successful_third_missing_is_pending() -> None:
    pr = _pr(
        statusCheckRollup={
            "state": "SUCCESS",
            "contexts": [
                _required(REQUIRED_CONTEXTS[0], "SUCCESS", "2026-10-30T10:00:00Z"),
                _required(REQUIRED_CONTEXTS[1], "SUCCESS", "2026-10-30T10:00:00Z"),
            ],
        }
    )
    assert registry.classify(pr, required_contexts=REQUIRED_CONTEXTS) == "CI_PENDING"


def test_m15_all_required_missing_but_rollup_success_is_not_ready() -> None:
    pr = _pr(statusCheckRollup={"state": "SUCCESS", "contexts": []})
    assert registry.classify(pr, required_contexts=REQUIRED_CONTEXTS) == "CI_PENDING"


def test_m15_older_failure_newer_success_is_ready() -> None:
    contexts = _green_contexts()[:2] + [
        _required(REQUIRED_CONTEXTS[2], "FAILURE", "2026-10-30T09:00:00Z"),
        _required(REQUIRED_CONTEXTS[2], "SUCCESS", "2026-10-30T10:00:00Z"),
    ]
    pr = _pr(statusCheckRollup={"state": "FAILURE", "contexts": contexts})
    assert registry.classify(pr, required_contexts=REQUIRED_CONTEXTS) == "READY"


def test_m15_older_success_newer_failure_is_ci_red() -> None:
    contexts = _green_contexts()[:2] + [
        _required(REQUIRED_CONTEXTS[2], "SUCCESS", "2026-10-30T09:00:00Z"),
        _required(REQUIRED_CONTEXTS[2], "FAILURE", "2026-10-30T10:00:00Z"),
    ]
    pr = _pr(statusCheckRollup={"state": "SUCCESS", "contexts": contexts})
    assert registry.classify(pr, required_contexts=REQUIRED_CONTEXTS) == "CI_RED"


def test_m15_required_context_still_running_is_pending() -> None:
    contexts = _green_contexts()[:2] + [
        _required(REQUIRED_CONTEXTS[2], "IN_PROGRESS", "2026-10-30T10:00:00Z"),
    ]
    pr = _pr(statusCheckRollup={"state": "PENDING", "contexts": contexts})
    assert registry.classify(pr, required_contexts=REQUIRED_CONTEXTS) == "CI_PENDING"


def test_m15_duplicate_context_with_ambiguous_timestamps_is_deterministic() -> None:
    """Equal stamps: the later list position wins, and the result is not READY
    unless that winner is green."""
    contexts = _green_contexts()[:2] + [
        _required(REQUIRED_CONTEXTS[2], "FAILURE", "2026-10-30T10:00:00Z"),
        _required(REQUIRED_CONTEXTS[2], "SUCCESS", "2026-10-30T10:00:00Z"),
    ]
    pr = _pr(statusCheckRollup={"state": "FAILURE", "contexts": contexts})
    assert registry.classify(pr, required_contexts=REQUIRED_CONTEXTS) == "READY"
    contexts_rev = _green_contexts()[:2] + [
        _required(REQUIRED_CONTEXTS[2], "SUCCESS", "2026-10-30T10:00:00Z"),
        _required(REQUIRED_CONTEXTS[2], "FAILURE", "2026-10-30T10:00:00Z"),
    ]
    pr_rev = _pr(statusCheckRollup={"state": "SUCCESS", "contexts": contexts_rev})
    assert registry.classify(pr_rev, required_contexts=REQUIRED_CONTEXTS) == "CI_RED"


def test_m15_completely_unavailable_context_data_is_not_ready() -> None:
    """No rollup at all is not a pass."""
    pr = _pr(statusCheckRollup=None)
    assert registry.classify(pr, required_contexts=REQUIRED_CONTEXTS) == "CI_PENDING"


def test_m15_explicit_zero_required_contexts_is_ready() -> None:
    """A readable gate that requires nothing is a real READY."""
    pr = _pr(statusCheckRollup={"state": "SUCCESS", "contexts": _green_contexts()})
    assert registry.classify(pr, required_contexts=[]) == "READY"


def test_m15_unreadable_hint_with_no_matching_context_falls_back_to_rollup() -> None:
    """When protection is unreadable the context list is a hint: if none of the
    hinted names ran, the coarse rollup decides — and a red rollup is CI_RED."""
    pr = _pr(
        statusCheckRollup={
            "state": "FAILURE",
            "contexts": [_required("some-other-check", "SUCCESS", "2026-10-30T10:00:00Z")],
        }
    )
    assert (
        registry._latest_ci_verdict(pr, REQUIRED_CONTEXTS, require_all_present=False) == "FAILURE"
    )


def test_m15_unreadable_hint_judges_the_present_contexts() -> None:
    """With a hint (unreadable protection) a present green context is a pass and
    a missing hinted name is not a missing gate."""
    pr = _pr(
        statusCheckRollup={
            "state": "FAILURE",
            "contexts": [_required(REQUIRED_CONTEXTS[0], "SUCCESS", "2026-10-30T10:00:00Z")],
        }
    )
    assert (
        registry._latest_ci_verdict(pr, REQUIRED_CONTEXTS, require_all_present=False) == "SUCCESS"
    )


def test_m15_unreadable_protection_is_never_ready() -> None:
    pr = _pr(statusCheckRollup={"state": "SUCCESS", "contexts": _green_contexts()})
    assert (
        registry.classify(pr, required_contexts=REQUIRED_CONTEXTS, protection_readable=False)
        == "PROTECTION_UNREADABLE"
    )


def test_latest_ci_state_reports_the_latest_per_context_outcome() -> None:
    # Every required context is present (strict mode) and the newest run of the
    # first one is the green re-run, so the head reads SUCCESS.
    contexts = [
        _required(REQUIRED_CONTEXTS[0], "CANCELLED", "2026-10-30T01:40:12Z"),
        _required(REQUIRED_CONTEXTS[0], "SUCCESS", "2026-10-30T01:49:43Z"),
        _required(REQUIRED_CONTEXTS[1], "SUCCESS", "2026-10-30T01:49:43Z"),
        _required(REQUIRED_CONTEXTS[2], "SUCCESS", "2026-10-30T01:49:43Z"),
    ]
    pr = _pr(statusCheckRollup={"state": "FAILURE", "contexts": contexts})
    assert registry._latest_ci_state(pr, REQUIRED_CONTEXTS) == "SUCCESS"


def test_latest_run_state_returns_none_for_an_absent_context() -> None:
    pr = _pr(statusCheckRollup={"state": "SUCCESS", "contexts": _green_contexts()})
    assert registry._latest_run_state(pr, "no-such-context") is None


def test_registry_row_round_trips_latest_run_through_as_pr_record() -> None:
    """A registry read back from disk must classify exactly like the live read."""
    row = {
        "base": "main",
        "is_draft": False,
        "mergeable": "MERGEABLE",
        "merge_state": "CLEAN",
        "review_decision": "",
        "head_sha": "a" * 40,
        "ci_state": "FAILURE",
        "status_contexts": [
            _required(REQUIRED_CONTEXTS[0], "CANCELLED", "2026-10-30T01:40:12Z"),
            _required(REQUIRED_CONTEXTS[0], "SUCCESS", "2026-10-30T01:49:43Z"),
            _required(REQUIRED_CONTEXTS[1], "SUCCESS", "2026-10-30T02:01:44Z"),
            _required(REQUIRED_CONTEXTS[2], "SUCCESS", "2026-10-30T01:41:50Z"),
        ],
    }
    record = registry.as_pr_record(row)
    assert registry.classify(record, required_contexts=REQUIRED_CONTEXTS) == "READY"


# ---------------------------------------------------------------------------
# M14 defect B — an unreadable branch protection must not abort collection.
# A 403 must leave a useful, honest registry that certifies NOTHING ready.
# ---------------------------------------------------------------------------
def test_protection_unreadable_is_its_own_lowest_priority_blocker() -> None:
    pr = _pr()  # otherwise fully green
    assert registry.blockers(pr, protection_readable=False) == ["PROTECTION_UNREADABLE"]
    assert registry.classify(pr, protection_readable=False) == "PROTECTION_UNREADABLE"
    # A real defect still outranks it — an unreadable gate never hides a conflict.
    pr_bad = _pr(mergeable="CONFLICTING")
    assert registry.classify(pr_bad, protection_readable=False) == "CONFLICT"


def test_unreadable_protection_certifies_nothing_ready() -> None:
    prs = [_pr(), _pr(), _pr(isDraft=True)]
    counts = registry.summarise(prs, protection_readable=False)
    assert counts["READY"] == 0
    assert counts["PROTECTION_UNREADABLE"] == 2
    assert counts["DRAFT"] == 1


def test_read_protection_degrades_to_reason_on_403() -> None:
    with patch.object(registry, "_gh", side_effect=RuntimeError("gh api ... failed: HTTP 403")):
        payload, reason = registry._read_protection("bot523h/nexus-ai-agent")
    assert payload is None
    assert "403" in reason


def test_read_protection_returns_payload_when_readable() -> None:
    with patch.object(registry, "_gh", return_value=json.dumps(PROTECTION)):
        payload, reason = registry._read_protection("bot523h/nexus-ai-agent")
    assert reason == ""
    assert registry.required_contexts(payload) == (REQUIRED_CONTEXTS, 0)


def test_workflow_context_reader_names_the_ci_jobs() -> None:
    """With protection unreadable, the committed workflow still names the gate."""
    names = registry._required_contexts_from_workflow()
    assert REQUIRED_CONTEXTS[0] in names
    assert REQUIRED_CONTEXTS[1] in names
    assert REQUIRED_CONTEXTS[2] in names


def test_workflow_context_reader_is_empty_for_a_missing_file(tmp_path: Any) -> None:
    assert registry._required_contexts_from_workflow(tmp_path / "nope.yml") == []


def test_build_registry_degrades_without_raising_on_unreadable_protection() -> None:
    """collect must still produce a registry when protection is a 403."""
    sample = [
        {
            "number": 1,
            "title": "green",
            "head": "arena/x",
            "head_sha": "a" * 40,
            "base": "main",
            "is_draft": False,
            "mergeable": "MERGEABLE",
            "merge_state": "CLEAN",
            "review_decision": "",
            "ci_state": "SUCCESS",
            "ci_contexts": 0,
            "status_contexts": [],
            "commits": 1,
            "additions": 1,
            "deletions": 0,
            "changed_files": 1,
            "author": "bot523h",
            "created_at": "2026-10-30T00:00:00Z",
            "updated_at": "2026-10-30T00:00:00Z",
            "tier": "",
        }
    ]
    with (
        patch.object(registry, "fetch_main_sha", return_value="b" * 40),
        patch.object(registry, "fetch_open_prs", return_value=sample),
        patch.object(registry, "_read_protection", return_value=(None, "HTTP 403")),
    ):
        reg = registry.build_registry("bot523h/nexus-ai-agent", wait_seconds=0.0)
    assert reg["protection_state"] == "unreadable"
    assert reg["protection_error"] == "HTTP 403"
    assert reg["totals"]["READY"] == 0
    assert reg["totals"]["PROTECTION_UNREADABLE"] == 1
    text = registry.render_report(reg)
    assert "protection: **unreadable**" in text
