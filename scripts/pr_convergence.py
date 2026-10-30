#!/usr/bin/env python3
"""Live PR convergence registry for `bot523h/nexus-ai-agent`.

The repository carried a recurring failure mode: a session reported PR state
from memory (or from a private JSON that was never committed) and the next
session could not reproduce a single number.  This script is the mechanical
answer — one command, one live observation, one committed artifact.

    python scripts/pr_convergence.py collect --out .agents/evidence/PR_REGISTRY_2026-10-25.json
    python scripts/pr_convergence.py report --in .agents/evidence/PR_REGISTRY_2026-10-25.json
    python scripts/pr_convergence.py guard 125

Design rules (all four exist because each one was violated at least once):

* **Live only.**  Every number comes from one GraphQL read against GitHub in
  the same process run.  Nothing is read from a cache, a prior report, or an
  agent's memory.
* **Deterministic classification.**  :func:`classify` is a pure function of the
  PR record, so the tier of a PR is testable without the network
  (``tests/unit/test_pr_convergence.py``).
* **Merge-readiness is never inferred from "it exists".**  Being open is not
  being ready; the tiers below are ordered from *hard blocker* to *merge now*.
* **Unreadable is not a pass.**  Exit ``2`` means a source was unreadable
  (mirrors ``scripts/merge_base_guard.py``); only ``0`` means verified.

Exit codes: ``0`` ok, ``1`` blocked/violation, ``2`` unreadable.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "nexus-pr-registry/v1"
DEFAULT_REPO = "bot523h/nexus-ai-agent"
EXPECTED_BASE = "main"

#: Blockers, ordered from "cannot be fixed by any credential" to "mechanical".
#: A PR's tier is its *first* blocker, so the list doubles as the priority order.
BLOCKERS: tuple[str, ...] = (
    "WRONG_BASE_BRANCH",
    "DRAFT",
    "CONFLICT",
    "MERGEABILITY_UNKNOWN",
    "CI_RED",
    "CI_PENDING",
    "CHANGES_REQUESTED",
    "NEEDS_APPROVAL",
    "REBASE_FIRST",
    # Lowest-priority blocker: the merge gate itself could not be read, so no PR
    # may be certified READY.  It sits last so a *known* blocker (a conflict, a
    # draft) still wins the tier — an unreadable gate never hides a real defect.
    "PROTECTION_UNREADABLE",
)

TIERS: tuple[str, ...] = (*BLOCKERS, "READY")

#: Tier meaning "act now": green, approved, up to date, no conflict.
ACTIONABLE = ("READY",)

_PR_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 50, after: $cursor,
                  orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        isDraft
        headRefName
        headRefOid
        baseRefName
        mergeable
        mergeStateStatus
        reviewDecision
        createdAt
        updatedAt
        additions
        deletions
        changedFiles
        author { login }
        commits(last: 1) { totalCount }
        statusCheckRollup {
          state
          contexts(first: 100) {
            totalCount
            nodes {
              __typename
              ... on CheckRun { name conclusion completedAt }
              ... on StatusContext { context state }
            }
          }
        }
      }
    }
  }
}
"""


# --------------------------------------------------------------------------
# pure decision logic
# --------------------------------------------------------------------------
def _rollup_state(pr: dict[str, Any]) -> str:
    rollup = pr.get("statusCheckRollup") or {}
    return str(rollup.get("state") or "NONE").upper()


#: Terminal conclusions that are *not* a green pass but still describe a
#: finished run.  A context whose latest run is one of these is red, not pending.
_RED_CONCLUSIONS = {
    "FAILURE",
    "ERROR",
    "TIMED_OUT",
    "CANCELLED",
    "ACTION_REQUIRED",
    "STARTUP_FAILURE",
    "STALE",
}
#: Non-gating terminal states: a green pass, or a context that deliberately
#: opted out (a skipped job is not a failure — mirrors ``_NON_GATING_OK``).
_GREEN_CONCLUSIONS = {"SUCCESS", "PASS", "SKIPPED", "NEUTRAL"}


def _latest_run_state(pr: dict[str, Any], name: str) -> str | None:
    """The conclusion of the **most recent** run of context *name*, or ``None``.

    A re-run *cancels* the superseded attempt, so the same context appears twice
    on one head — an old ``CANCELLED`` next to a new ``SUCCESS``.  GitHub's own
    required-check evaluation uses the latest run; so must the registry, or a
    green head reads red (the observed #287 / #289 misclassification).

    Ordering mirrors :func:`_contexts_of`: an explicit completion timestamp
    wins, otherwise the list position does (the REST reader is oldest→newest).
    ``None`` means the context never ran on this head — which is *not* a pass.
    """
    rollup = pr.get("statusCheckRollup")
    if isinstance(rollup, dict) and isinstance(rollup.get("contexts"), list):
        nodes = rollup["contexts"]
    elif isinstance(rollup, list):
        nodes = rollup
    else:
        return None
    best_key: tuple[int, Any] | None = None
    best_state: str | None = None
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        if (node.get("name") or node.get("context") or "") != name:
            continue
        state = str(node.get("conclusion") or node.get("state") or "").upper()
        stamp = str(
            node.get("completedAt")
            or node.get("completed_at")
            or node.get("updatedAt")
            or node.get("updated_at")
            or ""
        )
        key: tuple[int, Any] = (1, stamp) if stamp else (0, index)
        if best_key is None or key >= best_key:
            best_key, best_state = key, state
    return best_state


def _has_context_data(pr: dict[str, Any]) -> bool:
    """True when the record carries a per-context check list (a *live* read).

    A live GraphQL/REST read always includes the per-context list, so a required
    context that is absent from it genuinely did **not** run on this head.  Only
    a genuinely old serialized snapshot (no list at all) may use the coarse
    rollup, via the explicit legacy contract below — a current live response can
    never take that path to bypass a missing required check.
    """
    rollup = pr.get("statusCheckRollup")
    if isinstance(rollup, dict):
        return isinstance(rollup.get("contexts"), list)
    return isinstance(rollup, list)


def _latest_ci_verdict(
    pr: dict[str, Any],
    required_contexts: Iterable[str],
    *,
    require_all_present: bool = True,
) -> str:
    """``"SUCCESS"`` | ``"FAILURE"`` | ``"PENDING"`` for the merge gate.

    **Strict (live) mode** — when the record carries per-context data, *every*
    required context must be present **and** its latest run must be green:

    * a required context that is missing, running, or has an unknown conclusion
      is never a pass (``PENDING``) — the earlier "only judge the contexts that
      happen to be present" logic certified a head ``READY`` while a required
      check had never reported;
    * any latest run that is a failure is ``FAILURE``;
    * only "every required context present and green" is ``SUCCESS``;
    * zero required contexts is ``SUCCESS`` (a real, readable "no CI gate"
      configuration — the readability of the gate is tracked separately by
      ``PROTECTION_UNREADABLE``).

    ``require_all_present`` is the caller's assertion that *required_contexts* is
    the **authoritative** gate — true only when branch protection was read.  When
    protection is unreadable the context list is a best-effort hint, so a context
    missing from it is not a missing gate (``PROTECTION_UNREADABLE`` fail-closes
    separately) and the present contexts (or the coarse rollup) decide.

    **Legacy mode** — a genuinely old snapshot with no per-context list falls
    back to the coarse rollup.  This is the explicit legacy-data contract; a
    live response always has the list, so it cannot reach here.
    """
    contexts = list(required_contexts)
    if _has_context_data(pr):
        states = {name: _latest_run_state(pr, name) for name in contexts}
        if require_all_present and any(state is None for state in states.values()):
            # A required context that never ran on this head is NOT a pass.
            return "PENDING"
        values = [state for state in states.values() if state is not None]
        if not values:
            # Nothing authoritative is present; fall back to the coarse rollup.
            state = _rollup_state(pr)
            if state in {"FAILURE", "ERROR"}:
                return "FAILURE"
            return "SUCCESS" if state == "SUCCESS" else "PENDING"
        if any(state in _RED_CONCLUSIONS for state in values):
            return "FAILURE"
        if all(state in _GREEN_CONCLUSIONS for state in values):
            return "SUCCESS"
        # A non-terminal or unknown conclusion is never a pass (pending is safe).
        return "PENDING"
    # Legacy snapshot: no per-context data.  Trust the coarse rollup, but an
    # unreadable/missing rollup must never read as SUCCESS.
    state = _rollup_state(pr)
    if state in {"FAILURE", "ERROR"}:
        return "FAILURE"
    return "SUCCESS" if state == "SUCCESS" else "PENDING"


def _latest_ci_state(
    pr: dict[str, Any],
    required_contexts: Iterable[str],
    *,
    require_all_present: bool = True,
) -> str:
    """One-word description of the latest per-context CI outcome (for the report).

    Mirrors :func:`_latest_ci_verdict`: strict when per-context data is present,
    coarse rollup only for a legacy snapshot.
    """
    if _has_context_data(pr):
        return _latest_ci_verdict(
            pr, required_contexts, require_all_present=require_all_present
        )
    return _rollup_state(pr)


def blockers(
    pr: dict[str, Any],
    expected_base: str = EXPECTED_BASE,
    required_approvals: int = 0,
    required_contexts: Iterable[str] = (),
    protection_readable: bool = True,
) -> list[str]:
    """Every reason this PR cannot be merged right now, hardest first.

    ``NEEDS_APPROVAL`` is listed separately from ``CHANGES_REQUESTED`` because
    the two have *opposite* remedies: a requested change is work for the author,
    while a missing approval on a **self-authored** PR cannot be produced by any
    credential the author holds — GitHub forbids approving your own pull
    request.  Lumping them together produced a plan that could never execute.

    ``required_approvals`` is the repository's **live** review requirement.  A
    repository that requires zero approvals (this one does) must not classify a
    green, conflict-free PR as ``NEEDS_APPROVAL`` — that is the mirror-image of
    the original bug: a gate that does not exist reported as if it did.
    """
    found: list[str] = []
    base = str(pr.get("baseRefName") or "")
    if base.strip() != expected_base:
        found.append("WRONG_BASE_BRANCH")
    if pr.get("isDraft"):
        found.append("DRAFT")
    mergeable = str(pr.get("mergeable") or "").upper()
    if mergeable == "CONFLICTING":
        found.append("CONFLICT")
    elif mergeable != "MERGEABLE":
        # GitHub recomputes mergeability asynchronously whenever the base moves;
        # "not computed yet" is NOT "no conflict".  Reporting it as clean is how
        # a 46-conflict backlog turns into "0 conflicts" the moment main moves.
        found.append("MERGEABILITY_UNKNOWN")

    state = _latest_ci_verdict(
        pr, required_contexts, require_all_present=protection_readable
    )
    if state == "FAILURE":
        found.append("CI_RED")
    elif state == "PENDING":
        found.append("CI_PENDING")

    decision = str(pr.get("reviewDecision") or "").upper()
    if decision == "CHANGES_REQUESTED":
        found.append("CHANGES_REQUESTED")
    elif required_approvals > 0 and decision != "APPROVED":
        found.append("NEEDS_APPROVAL")

    if str(pr.get("mergeStateStatus") or "").upper() in {"BEHIND", "DIRTY"}:
        found.append("REBASE_FIRST")
    if not protection_readable:
        found.append("PROTECTION_UNREADABLE")
    return found


def classify(
    pr: dict[str, Any],
    expected_base: str = EXPECTED_BASE,
    required_approvals: int = 0,
    required_contexts: Iterable[str] = (),
    protection_readable: bool = True,
) -> str:
    """The single tier this PR belongs to — its hardest blocker, or ``READY``."""
    found = blockers(pr, expected_base, required_approvals, required_contexts, protection_readable)
    return found[0] if found else "READY"


def summarise(
    prs: Iterable[dict[str, Any]],
    expected_base: str = EXPECTED_BASE,
    required_approvals: int = 0,
    required_contexts: Iterable[str] = (),
    protection_readable: bool = True,
) -> dict[str, int]:
    """Count PRs per tier, including the zeroes (a missing key is a lie)."""
    counts = dict.fromkeys(TIERS, 0)
    for pr in prs:
        counts[
            classify(pr, expected_base, required_approvals, required_contexts, protection_readable)
        ] += 1
    return counts


#: Contexts whose presence/absence in the rollup is informational only.  The
#: rollup is computed from *all* check runs, but only a repository's required
#: contexts are an actual merge gate; a non-required job that is skipped (e.g.
#: ``merge-base-guard`` on a push to ``main``) must not read as a failure.
_NON_GATING_OK = {"SUCCESS", "SKIPPED", "NEUTRAL"}


def required_contexts(protection: dict[str, Any] | None) -> tuple[list[str], int]:
    """Read the *actual* merge gate from a branch-protection payload.

    Returns ``(required_contexts, required_approvals)``.  This is deliberately
    a pure function of the API payload so it is testable without the network.

    A ``403``/absent payload is **not** "no gate": it raises, and the caller
    fails closed (exit 2).  Assuming a gate is off because it could not be read
    is exactly how an unguarded merge happens.
    """
    if protection is None:
        raise RuntimeError(
            "branch protection is unreadable — refusing to assume the merge gate is off"
        )
    checks = protection.get("required_status_checks") or {}
    contexts = list(checks.get("contexts") or [])
    reviews = protection.get("required_pull_request_reviews") or {}
    approvals = int(reviews.get("required_approving_review_count") or 0)
    return contexts, approvals


def _contexts_of(pr: dict[str, Any]) -> dict[str, str]:
    """Map each check context name to its **latest** conclusion/state.

    Handles both the GraphQL ``statusCheckRollup.contexts`` shape (used by
    ``collect``/``guard``) and the flat REST check-run list, so the guard does
    not depend on which reader produced the record.

    A context can appear more than once on one head: re-running a workflow
    *cancels* the superseded run, so the same name shows up as an old
    ``CANCELLED`` next to a new ``SUCCESS``.  GitHub's own required-check
    evaluation uses the **most recent** run, so the guard must too — otherwise a
    green head reads as red and a genuinely mergeable PR is refused.
    """
    rollup = pr.get("statusCheckRollup")
    if isinstance(rollup, dict) and isinstance(rollup.get("contexts"), list):
        nodes = rollup["contexts"]
    elif isinstance(rollup, list):
        nodes = rollup
    else:
        return {}
    newest: dict[str, tuple[tuple[int, Any], str]] = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        name = node.get("name") or node.get("context") or ""
        if not name:
            continue
        state = str(node.get("conclusion") or node.get("state") or "").upper()
        stamp = str(
            node.get("completedAt")
            or node.get("completed_at")
            or node.get("updatedAt")
            or node.get("updated_at")
            or ""
        )
        # Prefer an explicit completion timestamp; fall back to list position
        # (the REST reader returns oldest→newest).  The leading flag keeps the
        # two key kinds comparable without mixing str/int.
        key: tuple[int, Any] = (1, stamp) if stamp else (0, index)
        if name not in newest or key >= newest[name][0]:
            newest[name] = (key, state)
    return {name: state for name, (_key, state) in newest.items()}


def check_guard(
    pr: dict[str, Any],
    expected_sha: str | None = None,
    expected_base: str = EXPECTED_BASE,
    protection: dict[str, Any] | None = None,
) -> tuple[int, list[str]]:
    """Pre-merge guard.  Returns ``(exit_code, reasons)``.

    ``exit_code`` is ``0`` only when the PR is genuinely mergeable right now:
    main-bound, not a draft, no conflict, up to date with the base, every
    **required** status context green, and no changes requested.

    The gate is read from the repository's *actual* branch protection
    (``protection``), never hard-coded: this repository requires three status
    contexts and **zero** approvals, so treating a missing review as a blocker
    (as an earlier revision did) wrongly reported mergeable PRs as BLOCKED.

    ``--expect-sha`` pins the exact head that was reviewed; a moved head is a
    violation, never a silent pass.
    """
    reasons: list[str] = []
    contexts, approvals = required_contexts(protection)

    base = str(pr.get("baseRefName") or "")
    if base.strip() != expected_base:
        reasons.append(f"base is {base!r}, expected {expected_base!r}")
    if pr.get("isDraft"):
        reasons.append("pull request is a draft")
    if str(pr.get("mergeable") or "").upper() != "MERGEABLE":
        reasons.append(f"mergeable={pr.get('mergeable')!r} (conflict or not computed)")

    # Required-context check: each named context must be present and green.
    live = _contexts_of(pr)
    missing = [c for c in contexts if c not in live]
    failed = [c for c in contexts if live.get(c) not in _NON_GATING_OK and c in live]
    if missing:
        reasons.append(f"required checks missing on this head: {missing}")
    if failed:
        reasons.append(f"required checks not green: {[(c, live[c]) for c in failed]}")
    # Only when no contexts could be read at all do we fall back to the coarse
    # rollup state — an unreadable rollup must never read as SUCCESS.
    if not contexts and _rollup_state(pr) not in {"SUCCESS"}:
        reasons.append(f"CI rollup state is {_rollup_state(pr)!r}, expected 'SUCCESS'")

    decision = str(pr.get("reviewDecision") or "").upper()
    if decision == "CHANGES_REQUESTED":
        reasons.append("review decision is CHANGES_REQUESTED")
    elif approvals > 0 and decision != "APPROVED":
        reasons.append(
            f"review decision is not APPROVED — this branch requires {approvals} "
            "approving review(s) (a pull request can never be approved by its own author)"
        )

    merge_state = str(pr.get("mergeStateStatus") or "").upper()
    if merge_state == "BEHIND":
        reasons.append("head is BEHIND main — CI did not run on the current base")
    elif merge_state not in {"CLEAN", "UNSTABLE", "HAS_HOOKS"}:
        reasons.append(f"mergeStateStatus is {merge_state!r}, expected 'CLEAN'")

    head = str(pr.get("headRefOid") or "")
    if expected_sha is not None:
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", expected_sha):
            reasons.append(
                f"expected SHA must contain 7–40 hexadecimal characters (received {expected_sha!r})"
            )
        elif not head.lower().startswith(expected_sha.lower()):
            reasons.append(f"head moved: expected {expected_sha}, live {head}")
    return (0 if not reasons else 1), reasons


# --------------------------------------------------------------------------
# live read
# --------------------------------------------------------------------------
def _gh(args: list[str]) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout


def _graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is None:
            continue
        args += ["-f", f"{key}={value}"]
    decoded = json.loads(_gh(args))
    if not isinstance(decoded, dict):
        raise RuntimeError("GitHub GraphQL returned a non-object response")
    errors = decoded.get("errors")
    if errors:
        detail = json.dumps(errors, ensure_ascii=False)[:800]
        raise RuntimeError(f"GitHub GraphQL returned errors: {detail}")
    return decoded


def fetch_open_prs(repo: str = DEFAULT_REPO) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        payload = _graphql(_PR_QUERY, {"owner": owner, "name": name, "cursor": cursor})
        page = payload["data"]["repository"]["pullRequests"]
        for node in page["nodes"]:
            rollup = node.get("statusCheckRollup") or {}
            contexts = (rollup.get("contexts") or {}) if rollup else {}
            context_nodes = [
                {
                    "name": (c.get("name") or c.get("context") or ""),
                    "conclusion": (c.get("conclusion") or c.get("state") or "").upper(),
                    "completedAt": c.get("completedAt") or "",
                }
                for c in (contexts.get("nodes") or [])
            ]
            out.append(
                {
                    "number": node["number"],
                    "title": node["title"],
                    "head": node["headRefName"],
                    "head_sha": node["headRefOid"],
                    "base": node["baseRefName"],
                    "is_draft": node["isDraft"],
                    "mergeable": node["mergeable"],
                    "merge_state": node["mergeStateStatus"],
                    "review_decision": node["reviewDecision"] or "",
                    "ci_state": rollup.get("state") or "NONE",
                    "ci_contexts": (contexts or {}).get("totalCount") or 0,
                    "status_contexts": context_nodes,
                    "commits": (node.get("commits") or {}).get("totalCount") or 0,
                    "additions": node["additions"],
                    "deletions": node["deletions"],
                    "changed_files": node["changedFiles"],
                    "author": ((node.get("author") or {}).get("login")) or "",
                    "created_at": node["createdAt"],
                    "updated_at": node["updatedAt"],
                    "tier": "",
                }
            )
        info = page["pageInfo"]
        if not info["hasNextPage"]:
            break
        cursor = info["endCursor"]
        if cursor is None:  # defensive: never loop forever on a null cursor
            break
    return out


def fetch_main_sha(repo: str = DEFAULT_REPO) -> str:
    payload = json.loads(_gh(["api", f"repos/{repo}/commits/main", "--jq", "{sha: .sha}"]))
    return str(payload["sha"])


def as_pr_record(pr: dict[str, Any]) -> dict[str, Any]:
    """Map a registry row back onto the GraphQL shape :func:`classify` expects.

    ``status_contexts`` (the per-context check list with completion stamps) is
    carried through when present so latest-run classification works on a
    registry read back from disk, not only on a live GraphQL read.
    """
    record = {
        "baseRefName": pr["base"],
        "isDraft": pr["is_draft"],
        "mergeable": pr["mergeable"],
        "mergeStateStatus": pr["merge_state"],
        "reviewDecision": pr["review_decision"],
        "headRefOid": pr["head_sha"],
        "statusCheckRollup": {"state": pr["ci_state"]},
    }
    contexts = pr.get("status_contexts")
    if contexts is not None:
        # Carry the list even when empty: its *presence* is what tells
        # ``_has_context_data`` this is a live read (strict mode), so an empty
        # list must not degrade to the legacy rollup path.
        record["statusCheckRollup"]["contexts"] = contexts
    return record


def build_registry(
    repo: str = DEFAULT_REPO, wait_seconds: float = 180.0, poll_seconds: float = 5.0
) -> dict[str, Any]:
    """One settled read.  Waits out GitHub's async mergeability recomputation.

    After the base branch moves, every PR reports ``mergeable: UNKNOWN`` for a
    while.  A snapshot taken in that window is not a *smaller* backlog, it is an
    unmeasured one, so this function re-reads until the set settles (or the
    deadline passes — then the unknowns are reported as their own tier).
    """
    main_sha = fetch_main_sha(repo)
    # The review gate is the repository's live requirement, not a guess.  An
    # unreadable protection is *recorded*, never silently treated as "no gate":
    # the registry stays useful (it still lists every PR and its real blockers)
    # but every PR carries PROTECTION_UNREADABLE, so nothing is certified READY
    # on an unverified gate.
    protection, protection_reason = _read_protection(repo)
    protection_readable = protection is not None
    if protection_readable:
        contexts, required_approvals = required_contexts(protection)
        protection_source = "branch-protection API"
    else:
        # Best-effort: name the candidate gate from the committed workflow so the
        # registry is informative, but keep the read failure loud and visible.
        contexts = _required_contexts_from_workflow()
        required_approvals = 0
        protection_source = "ci.yml workflow (branch-protection API unreadable)"
        print(
            f"pr-registry: branch protection UNREADABLE ({protection_reason}); "
            f"degrading to workflow-derived contexts {contexts!r}. No PR will be "
            f"certified READY.",
            file=sys.stderr,
        )
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    while True:
        prs = fetch_open_prs(repo)
        unknown = sum(1 for pr in prs if pr["mergeable"] == "UNKNOWN")
        remaining = deadline - time.monotonic()
        if unknown == 0 or remaining <= 0:
            break
        if unknown:
            print(
                f"pr-registry: {unknown} of {len(prs)} PRs still report mergeable=UNKNOWN "
                f"(GitHub recomputing) — re-reading in {poll_seconds:.0f}s ({remaining:.0f}s left)",
                file=sys.stderr,
            )
        time.sleep(min(poll_seconds, remaining))
    for pr in prs:
        record = as_pr_record(pr)
        pr["blockers"] = blockers(
            record,
            required_approvals=required_approvals,
            required_contexts=contexts,
            protection_readable=protection_readable,
        )
        pr["tier"] = pr["blockers"][0] if pr["blockers"] else "READY"
        # The registry row reports the *latest run* of each required context, so
        # a superseded CANCELLED next to a green re-run is not read as red.
        pr["ci_state"] = _latest_ci_state(
            record, contexts, require_all_present=protection_readable
        )
    totals = dict.fromkeys(TIERS, 0)
    for pr in prs:
        totals[pr["tier"]] += 1
    blocker_counts = {code: 0 for code in BLOCKERS}
    for pr in prs:
        for code in pr["blockers"]:
            blocker_counts[code] += 1
    return {
        "schema": SCHEMA,
        "repo": repo,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "main_sha": main_sha,
        "required_approvals": required_approvals,
        "required_contexts": contexts,
        "protection_state": "readable" if protection_readable else "unreadable",
        "protection_source": protection_source,
        "protection_error": protection_reason,
        "open_total": len(prs),
        "totals": totals,
        "blockers": blocker_counts,
        "prs": prs,
    }


def render_report(registry: dict[str, Any]) -> str:
    ordered = sorted(registry["prs"], key=lambda p: (TIERS.index(p["tier"]), p["number"]))
    lines = [
        f"# open PR registry — {registry['repo']}",
        "",
        f"generated `{registry['generated_at']}` · main `{registry['main_sha'][:12]}` · "
        f"open **{registry['open_total']}**",
        "",
    ]
    protection_state = registry.get("protection_state")
    if protection_state:
        # An unreadable gate is a first-class fact in the artifact, not a silent
        # default: a reader must never assume the merge gate was verified.
        detail = registry.get("protection_error") or registry.get("protection_source") or ""
        lines.append(f"protection: **{protection_state}** {detail}".rstrip())
        lines.append("")
    lines += [
        "| tier | count |",
        "|---|---|",
    ]
    recorded_totals = registry.get("totals", {})
    for tier in TIERS:
        # Older v1 snapshots predate MERGEABILITY_UNKNOWN; missing means zero
        # only for report rendering, never for live collection/classification.
        lines.append(f"| `{tier}` | {recorded_totals.get(tier, 0)} |")
    lines += [
        "",
        "blockers (a PR can carry several; tier = the hardest one)",
        "",
        "| blocker | PRs |",
        "|---|---|",
    ]
    recorded_blockers = registry.get("blockers", {})
    for code in BLOCKERS:
        lines.append(f"| `{code}` | {recorded_blockers.get(code, 0)} |")
    lines += [
        "",
        "| PR | tier | base | CI | review | files | +/- | title |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for pr in ordered:
        lines.append(
            f"| #{pr['number']} | `{pr['tier']}` | {pr['base']} | {pr['ci_state']} | "
            f"{pr['review_decision'] or '—'} | {pr['changed_files']} | "
            f"+{pr['additions']}/-{pr['deletions']} | {pr['title'][:64]} |"
        )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _live_pr(number: int, repo: str) -> dict[str, Any]:
    payload = json.loads(
        _gh(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "number,title,baseRefName,isDraft,mergeable,mergeStateStatus,"
                "reviewDecision,headRefOid,statusCheckRollup,headRefName",
            ]
        )
    )
    checks = payload.get("statusCheckRollup") or []
    # Keep both shapes: the coarse rollup ``state`` for classify(), and the
    # per-context list so the guard can check the *required* contexts by name.
    # The completion timestamp is carried through so a superseded (cancelled)
    # re-run cannot outvote the newest run of the same context.
    payload["statusCheckRollup"] = {
        "state": _aggregate(checks),
        "contexts": [
            {
                "name": c.get("name") or c.get("context") or "",
                "conclusion": (c.get("conclusion") or c.get("state") or "").upper(),
                "completedAt": c.get("completedAt") or c.get("completed_at") or "",
            }
            for c in checks
        ],
    }
    return payload


def _live_protection(repo: str, branch: str = EXPECTED_BASE) -> dict[str, Any]:
    """Read the live branch protection for *branch*.  Unreadable is not 'off'.

    A ``403`` (or any non-zero ``gh`` exit) raises, and ``guard`` then exits 2
    rather than assuming no gate exists.  The previous revision could not read
    protection at all and so hard-coded a review requirement that this
    repository does not have.
    """
    raw = _gh(["api", f"repos/{repo}/branches/{branch}/protection"])
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError("branch protection returned a non-object response")
    return payload


#: The workflow that *defines* the required checks.  Used only to *name* the
#: gate when the protection API is unreadable; it never asserts the gate exists.
_CI_WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def _required_contexts_from_workflow(workflow: Path = _CI_WORKFLOW) -> list[str]:
    """The CI job display-names, read from the committed workflow (best effort).

    A protection read can fail (a 403 from a credential that is not an admin).
    That must never be reported as "no gate" — but it also must not leave the
    registry with *zero* information about which contexts matter.  This reads
    the job ``name:`` lines of ``ci.yml`` so the registry can still *name* the
    candidate gate.  It is deliberately conservative: a job with no explicit
    ``name:`` (whose display name is the job id) is skipped, so the list is a
    subset, and every caller treats it as a hint, never as proof the gate is on.
    """
    try:
        text = workflow.read_text(encoding="utf-8")
    except OSError:
        return []
    names: list[str] = []
    for line in text.splitlines():
        # Job display-names sit at exactly four spaces; step names are deeper.
        if line[:4] != "    " or line[4:5] == " ":
            continue
        stripped = line.strip()
        if not stripped.startswith("name:"):
            continue
        value = stripped[len("name:") :].strip().strip("\"'")
        if value and value.lower() != "ci":
            names.append(value)
    return names


def _read_protection(repo: str, branch: str = EXPECTED_BASE) -> tuple[dict[str, Any] | None, str]:
    """Read protection, degrading to ``(None, reason)`` instead of raising.

    Returns ``(payload, "")`` on success, or ``(None, reason)`` when the read
    fails.  The *reason* is preserved so the registry records *why* the gate is
    unknown — an unreadable source is evidence, not silence.
    """
    try:
        return _live_protection(repo, branch), ""
    except (RuntimeError, json.JSONDecodeError, OSError) as exc:
        return None, str(exc)[:400]


def _aggregate(checks: list[dict[str, Any]]) -> str:
    """Worst-wins aggregation of the REST/CLI check list."""
    states = [(c.get("conclusion") or c.get("state") or "").upper() for c in checks]
    if any(
        s
        in {
            "FAILURE",
            "STARTUP_FAILURE",
            "ERROR",
            "TIMED_OUT",
            "CANCELLED",
            "ACTION_REQUIRED",
            "STALE",
        }
        for s in states
    ):
        return "FAILURE"
    if not states:
        return "NONE"
    success_states = {"SUCCESS", "PASS", "SKIPPED", "NEUTRAL"}
    if all(state in success_states for state in states):
        return "SUCCESS"
    # Unknown conclusions are not evidence of success.  Pending is the only
    # safe aggregate until GitHub reports a recognized terminal conclusion.
    return "PENDING"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    sub = parser.add_subparsers(dest="command", required=True)

    p_collect = sub.add_parser("collect", help="live read → JSON registry")
    p_collect.add_argument("--out", default="-")
    p_collect.add_argument(
        "--no-wait",
        action="store_true",
        help="do not wait for GitHub to finish recomputing mergeability (may record UNKNOWN)",
    )

    p_report = sub.add_parser("report", help="JSON registry → markdown table")
    p_report.add_argument("--in", dest="infile", default="-")

    p_guard = sub.add_parser("guard", help="pre-merge guard for one PR")
    p_guard.add_argument("number", type=int)
    p_guard.add_argument("--expect-sha", default=None)

    args = parser.parse_args(argv)

    try:
        if args.command == "collect":
            registry = build_registry(args.repo, wait_seconds=0.0 if args.no_wait else 180.0)
            text = json.dumps(registry, indent=2, ensure_ascii=False) + "\n"
            if args.out == "-":
                sys.stdout.write(text)
            else:
                with open(args.out, "w", encoding="utf-8") as handle:
                    handle.write(text)
                print(
                    f"pr-registry: {registry['open_total']} open PRs → {args.out} "
                    f"(main {registry['main_sha'][:12]})"
                )
            return 0

        if args.command == "report":
            source = (
                sys.stdin.read()
                if args.infile == "-"
                else open(args.infile, encoding="utf-8").read()
            )
            sys.stdout.write(render_report(json.loads(source)))
            return 0

        if args.command == "guard":
            pr = _live_pr(args.number, args.repo)
            # The gate is the repository's real protection, not an assumption:
            # an unreadable payload raises here and the command exits 2.
            protection = _live_protection(args.repo, str(pr.get("baseRefName") or EXPECTED_BASE))
            code, reasons = check_guard(pr, expected_sha=args.expect_sha, protection=protection)
            if code == 0:
                print(
                    f"guard #{args.number}: PASS — main-bound, required checks green, "
                    f"head {pr['headRefOid'][:12]}, mergeable"
                )
            else:
                print(f"guard #{args.number}: BLOCKED")
                for reason in reasons:
                    print(f"  - {reason}")
            return code
    except (RuntimeError, KeyError, json.JSONDecodeError, OSError) as exc:
        print(f"pr-registry: source unreadable — {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
