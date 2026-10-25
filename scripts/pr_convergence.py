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
import subprocess
import sys
import time
from collections.abc import Iterable
from datetime import datetime, timezone
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
          contexts(first: 100) { totalCount }
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


def blockers(pr: dict[str, Any], expected_base: str = EXPECTED_BASE) -> list[str]:
    """Every reason this PR cannot be merged right now, hardest first.

    ``NEEDS_APPROVAL`` is listed separately from ``CHANGES_REQUESTED`` because
    the two have *opposite* remedies: a requested change is work for the author,
    while a missing approval on a **self-authored** PR cannot be produced by any
    credential the author holds — GitHub forbids approving your own pull
    request.  Lumping them together produced a plan that could never execute.
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

    state = _rollup_state(pr)
    if state in {"FAILURE", "ERROR"}:
        found.append("CI_RED")
    elif state in {"PENDING", "EXPECTED", "NONE"}:
        found.append("CI_PENDING")

    decision = str(pr.get("reviewDecision") or "").upper()
    if decision == "CHANGES_REQUESTED":
        found.append("CHANGES_REQUESTED")
    elif decision != "APPROVED":
        found.append("NEEDS_APPROVAL")

    if str(pr.get("mergeStateStatus") or "").upper() in {"BEHIND", "DIRTY"}:
        found.append("REBASE_FIRST")
    return found


def classify(pr: dict[str, Any], expected_base: str = EXPECTED_BASE) -> str:
    """The single tier this PR belongs to — its hardest blocker, or ``READY``."""
    found = blockers(pr, expected_base)
    return found[0] if found else "READY"


def summarise(prs: Iterable[dict[str, Any]], expected_base: str = EXPECTED_BASE) -> dict[str, int]:
    """Count PRs per tier, including the zeroes (a missing key is a lie)."""
    counts = dict.fromkeys(TIERS, 0)
    for pr in prs:
        counts[classify(pr, expected_base)] += 1
    return counts


def check_guard(
    pr: dict[str, Any], expected_sha: str | None = None, expected_base: str = EXPECTED_BASE
) -> tuple[int, list[str]]:
    """Pre-merge guard.  Returns ``(exit_code, reasons)``.

    ``exit_code`` is ``0`` only when the PR is genuinely mergeable right now:
    main-bound, not a draft, no conflict, CI green, no requested changes.
    ``--expect-sha`` pins the exact head that was reviewed; a moved head is a
    violation, never a silent pass.
    """
    reasons: list[str] = []
    base = str(pr.get("baseRefName") or "")
    if base.strip() != expected_base:
        reasons.append(f"base is {base!r}, expected {expected_base!r}")
    if pr.get("isDraft"):
        reasons.append("pull request is a draft")
    if str(pr.get("mergeable") or "").upper() != "MERGEABLE":
        reasons.append(f"mergeable={pr.get('mergeable')!r} (conflict or not computed)")
    state = _rollup_state(pr)
    if state not in {"SUCCESS"}:
        reasons.append(f"CI rollup state is {state!r}, expected 'SUCCESS'")
    decision = str(pr.get("reviewDecision") or "").upper()
    if decision == "CHANGES_REQUESTED":
        reasons.append("review decision is CHANGES_REQUESTED")
    elif decision != "APPROVED":
        reasons.append(
            "review decision is not APPROVED — required review not satisfied "
            "(a pull request can never be approved by its own author)"
        )
    merge_state = str(pr.get("mergeStateStatus") or "").upper()
    if merge_state == "BEHIND":
        reasons.append("head is BEHIND main — CI did not run on the current base")
    elif merge_state != "CLEAN":
        reasons.append(f"mergeStateStatus is {merge_state!r}, expected 'CLEAN'")
    head = str(pr.get("headRefOid") or "")
    if expected_sha and not head.startswith(expected_sha):
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
    return json.loads(_gh(args))


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
    """Map a registry row back onto the GraphQL shape :func:`classify` expects."""
    return {
        "baseRefName": pr["base"],
        "isDraft": pr["is_draft"],
        "mergeable": pr["mergeable"],
        "mergeStateStatus": pr["merge_state"],
        "reviewDecision": pr["review_decision"],
        "headRefOid": pr["head_sha"],
        "statusCheckRollup": {"state": pr["ci_state"]},
    }


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
        pr["blockers"] = blockers(record)
        pr["tier"] = pr["blockers"][0] if pr["blockers"] else "READY"
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
        "| tier | count |",
        "|---|---|",
    ]
    for tier in TIERS:
        lines.append(f"| `{tier}` | {registry['totals'][tier]} |")
    lines += [
        "",
        "blockers (a PR can carry several; tier = the hardest one)",
        "",
        "| blocker | PRs |",
        "|---|---|",
    ]
    for code in BLOCKERS:
        lines.append(f"| `{code}` | {registry['blockers'][code]} |")
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
    payload["statusCheckRollup"] = {"state": _aggregate(payload.get("statusCheckRollup") or [])}
    return payload


def _aggregate(checks: list[dict[str, Any]]) -> str:
    """Worst-wins aggregation of the REST/CLI check list."""
    states = [(c.get("conclusion") or c.get("state") or "").upper() for c in checks]
    if any(s in {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"} for s in states):
        return "FAILURE"
    if any(s in {"PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "WAITING", ""} for s in states):
        return "PENDING"
    if any(s in {"SUCCESS", "PASS", "SKIPPED", "NEUTRAL"} for s in states):
        return "SUCCESS"
    return "NONE"


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
            code, reasons = check_guard(pr, expected_sha=args.expect_sha)
            if code == 0:
                print(
                    f"guard #{args.number}: PASS — main-bound, CI SUCCESS, "
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
