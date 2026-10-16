#!/usr/bin/env python3
"""Fresh, reproducible census of every open pull request (GitHub REST, read-only).

For each open PR the script records the exact head/base SHAs, GitHub's mergeability
against *today's* base, the changed paths, the check-run conclusions on the exact head,
the overlap with a reference PR (default #218) and — the A-04 measurement — whether the PR
collides with the base branch **only** through the coordination board
(``.agents/board.json``): the intersection of the PR's changed files with the files the
base branch changed since the merge base, computed with the compare API.

Mechanical classification (``auto_state``):

* ``BLOCKED_BY_GOVERNANCE``  base branch is not the default branch (merge-base-guard);
* ``BLOCKED_BY_FAILURE``     a check run on the exact head concluded failure/timed_out;
* ``REBASE_REQUIRED``        GitHub reports ``dirty`` (conflicting) or ``behind``;
* ``MERGE_ELIGIBLE``         mergeable, no failing check, no overlap with the reference PR;
* ``DEFERRED``               draft PRs;
* ``REVIEW_REQUIRED``        otherwise (clean but waits on the reference PR or a review).

The optional ``--overrides`` JSON (``{"209": {"state": "...", "reason": "..."}}``) records
human dispositions that need a technical argument (e.g. ``SUPERSEDED_BY_VERIFIED_CHANGE``);
they are kept next to the mechanical state, never silently replacing it.

Usage::

    GITHUB_TOKEN=$(gh auth token) python scripts/pr_census.py \
        --repo bot523h/nexus-ai-agent --reference 218 \
        --overrides docs/audits/<pkg>/pr_overrides.json --out docs/audits/<pkg>/pr_census
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

BOARD_FILE = ".agents/board.json"
API = "https://api.github.com"


def _get(url: str, token: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "nexus-pr-census",
        },
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in {403, 429, 502, 503} and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise RuntimeError("unreachable")


def _paginate(url: str, token: str, key: str | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        sep = "&" if "?" in url else "?"
        payload = _get(f"{url}{sep}per_page=100&page={page}", token)
        chunk = payload[key] if key else payload
        if not isinstance(chunk, list):
            break
        items.extend(c for c in chunk if isinstance(c, dict))
        if len(chunk) < 100:
            break
        page += 1
    return items


def _pr_files(repo: str, number: int, token: str) -> list[str]:
    return [f["filename"] for f in _paginate(f"{API}/repos/{repo}/pulls/{number}/files", token)]


def _checks(repo: str, sha: str, token: str) -> dict[str, str]:
    runs = _paginate(f"{API}/repos/{repo}/commits/{sha}/check-runs", token, key="check_runs")
    out: dict[str, str] = {}
    for run in runs:
        name = str(run.get("name"))
        conclusion = run.get("conclusion") or run.get("status") or "unknown"
        # keep the most recent run per name (API returns newest first)
        out.setdefault(name, str(conclusion))
    return out


def _base_changed_since_merge_base(
    repo: str, head: str, base_branch: str, token: str
) -> tuple[list[str], int, int, bool]:
    """Files the base changed since the merge base.

    Returns ``(files, commits_unique_to_head, commits_only_in_base, truncated)``,
    all **relative to ``base_branch``** — which for a stacked PR is another
    feature branch, not ``main`` (see :func:`_main_relative_counts`).

    The compare direction is ``<base_branch>...<head>`` so the API's
    ``ahead_by`` / ``behind_by`` mean what their names say: commits the PR has
    that the base does not, and commits the base has that the PR does not.

    Defect fixed 2026-10-16: the first version passed ``<head>...<base_branch>``
    and published the two numbers under each other's names, so a ledger row for
    #218 read "26 behind" even though ``main`` is an ancestor of its head (26 is
    the count of *unique* commits).  The historical packages keep the old field
    names; this run and every run after it use the corrected ones.
    """
    basehead = urllib.parse.quote(f"{base_branch}...{head}", safe="")
    payload = _get(f"{API}/repos/{repo}/compare/{basehead}", token)
    assert isinstance(payload, dict)
    files = [f["filename"] for f in payload.get("files", []) if isinstance(f, dict)]
    # ``total_commits`` follows the same direction as ahead_by: commits the head
    # has that the base does not.
    total = int(payload.get("total_commits", 0))
    truncated = len(files) >= 300
    return (
        files,
        int(payload.get("ahead_by", 0)),
        int(payload.get("behind_by", 0)),
        truncated or total > 250,
    )


def _main_relative_counts(
    repo: str,
    head: str,
    base_ref: str,
    default_branch: str,
    token: str,
    base_ahead: int,
    base_behind: int,
    base_files: int,
) -> dict[str, int]:
    """Counts against ``default_branch``, never against a stacked base.

    Defect fixed 2026-10-16 (second, distinct from the direction defect above):
    the compare runs against the PR's **own** base branch, and every row then
    published those numbers under main-relative names.  For a stacked PR that is
    not a rounding error — #213 (base = #210's branch) read ``unique_commits=12``
    and ``commits_behind_main=19`` while its main-relative truth was
    ``26 / 6``.  A ledger built on that would order merges wrongly, so when the
    base is not the default branch a second compare against the default branch
    supplies the main-relative triple.
    """

    if base_ref == default_branch:
        return {
            "unique_commits_vs_main": base_ahead,
            "commits_behind_main": base_behind,
            "changed_files_vs_main": base_files,
        }
    try:
        main_files, ahead, behind, _ = _base_changed_since_merge_base(
            repo, head, default_branch, token
        )
    except urllib.error.HTTPError as exc:
        print(f"{head[:8]}: main compare failed ({exc.code})", file=sys.stderr)
        return {
            "unique_commits_vs_main": -1,
            "commits_behind_main": -1,
            "changed_files_vs_main": -1,
        }
    return {
        "unique_commits_vs_main": ahead,
        "commits_behind_main": behind,
        "changed_files_vs_main": len(main_files),
    }


def classify(
    pr: dict[str, Any], checks: dict[str, str], overlap_reference: int, default_branch: str
) -> str:
    if pr.get("draft"):
        return "DEFERRED"
    if (pr.get("base") or {}).get("ref") != default_branch:
        return "BLOCKED_BY_GOVERNANCE"
    failing = {
        n: c
        for n, c in checks.items()
        if c in {"failure", "timed_out", "action_required", "startup_failure"}
    }
    if failing:
        return "BLOCKED_BY_FAILURE"
    state = pr.get("mergeable_state")
    if state in {"dirty", "behind"} or pr.get("mergeable") is False:
        return "REBASE_REQUIRED"
    if state == "clean" and overlap_reference == 0:
        return "MERGE_ELIGIBLE"
    return "REVIEW_REQUIRED"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--reference", type=int, default=218, help="reference PR for overlap")
    parser.add_argument("--overrides", type=Path, default=None)
    parser.add_argument("--out", type=Path, required=True, help="output path without extension")
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token or "/" not in args.repo:
        print("GITHUB_TOKEN and --repo owner/name are required", file=sys.stderr)
        return 2

    repo_info = _get(f"{API}/repos/{args.repo}", token)
    assert isinstance(repo_info, dict)
    default_branch = str(repo_info["default_branch"])
    main_sha = str(
        _get(f"{API}/repos/{args.repo}/branches/{default_branch}", token)["commit"]["sha"]
    )
    overrides: dict[str, dict[str, str]] = {}
    if args.overrides and args.overrides.exists():
        overrides = json.loads(args.overrides.read_text(encoding="utf-8"))

    open_prs = _paginate(f"{API}/repos/{args.repo}/pulls?state=open", token)
    reference_files = set(_pr_files(args.repo, args.reference, token))
    rows: list[dict[str, object]] = []
    for summary in sorted(open_prs, key=lambda p: int(p["number"])):
        number = int(summary["number"])
        pr = _get(f"{API}/repos/{args.repo}/pulls/{number}", token)
        assert isinstance(pr, dict)
        head_sha = str(pr["head"]["sha"])
        base_ref = str(pr["base"]["ref"])
        files = _pr_files(args.repo, number, token)
        checks = _checks(args.repo, head_sha, token)
        try:
            base_changed, ahead, behind, truncated = _base_changed_since_merge_base(
                args.repo, head_sha, base_ref, token
            )
        except urllib.error.HTTPError as exc:
            base_changed, ahead, behind, truncated = [], -1, -1, True
            print(f"#{number}: compare failed ({exc.code})", file=sys.stderr)
        both_sides = sorted(set(files) & set(base_changed))
        overlap_ref = sorted(set(files) & reference_files) if number != args.reference else []
        board_only = both_sides == [BOARD_FILE]
        touches_board = BOARD_FILE in files
        auto_state = classify(pr, checks, len(overlap_ref), default_branch)
        if number == args.reference:
            auto_state = "REFERENCE"
        override = overrides.get(str(number), {})
        main_relative = _main_relative_counts(
            args.repo,
            head_sha,
            base_ref,
            default_branch,
            token,
            ahead,
            behind,
            len(files),
        )
        rows.append(
            {
                "number": number,
                "title": pr.get("title", ""),
                "author": (pr.get("user") or {}).get("login", ""),
                "head_ref": pr["head"]["ref"],
                "head_sha": head_sha,
                "base_ref": base_ref,
                "base_sha_at_census": main_sha if base_ref == default_branch else "",
                "compare_base_ref": base_ref,
                "draft": bool(pr.get("draft")),
                "mergeable": pr.get("mergeable"),
                "mergeable_state": pr.get("mergeable_state"),
                "review_decision": "",
                "changed_files": len(files),
                "files": files,
                "checks_on_head": checks,
                "failing_checks": [
                    n
                    for n, c in checks.items()
                    if c in {"failure", "timed_out", "action_required", "startup_failure"}
                ],
                "overlap_with_reference": overlap_ref,
                "base_changed_since_merge_base": len(base_changed),
                "base_changed_truncated": truncated,
                "both_sides_changed": both_sides,
                "touches_board": touches_board,
                "dirty_only_because_of_board": board_only and pr.get("mergeable_state") == "dirty",
                "board_in_both_sides": BOARD_FILE in both_sides,
                # base-relative pair: the compare above ran against ``base_ref``,
                # which for a stacked PR is another feature branch
                "unique_commits_vs_base": ahead,
                "base_commits_not_in_head": behind,
                # main-relative triple: what integration decisions are made from
                **main_relative,
                "auto_state": auto_state,
                "override_state": override.get("state", ""),
                "override_reason": override.get("reason", ""),
                "updated_at": pr.get("updated_at"),
            }
        )
        print(
            print(
                f"#{number} {auto_state} state={pr.get('mergeable_state')} files={len(files)} "
                f"both={len(both_sides)} board_only={board_only}"
            )
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    summary_counts: dict[str, int] = {}
    for row in rows:
        key = str(row["override_state"] or row["auto_state"])
        summary_counts[key] = summary_counts.get(key, 0) + 1
    payload = {
        "repo": args.repo,
        "default_branch": default_branch,
        "default_branch_sha": main_sha,
        "reference_pr": args.reference,
        "census_time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "open_prs": len(rows),
        "states": dict(sorted(summary_counts.items())),
        "dirty_only_because_of_board": sum(1 for r in rows if r["dirty_only_because_of_board"]),
        "board_in_both_sides": sum(1 for r in rows if r["board_in_both_sides"]),
        "touching_board": sum(1 for r in rows if r["touches_board"]),
        "rows": rows,
    }
    args.out.with_suffix(".json").write_text(
        json.dumps(payload, indent=1, sort_keys=True) + "\n", "utf-8"
    )
    columns = [
        "number",
        "title",
        "author",
        "head_ref",
        "head_sha",
        "base_ref",
        "draft",
        "mergeable_state",
        "changed_files",
        "failing_checks",
        "overlap_with_reference",
        "both_sides_changed",
        "touches_board",
        "dirty_only_because_of_board",
        "board_in_both_sides",
        "compare_base_ref",
        "unique_commits_vs_base",
        "base_commits_not_in_head",
        "unique_commits_vs_main",
        "commits_behind_main",
        "changed_files_vs_main",
        "auto_state",
        "override_state",
        "override_reason",
    ]
    with args.out.with_suffix(".csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=columns, lineterminator="\n", extrasaction="ignore"
        )
        writer.writeheader()
        for row in rows:
            flat = dict(row)
            for key in ("failing_checks", "overlap_with_reference", "both_sides_changed"):
                flat[key] = ";".join(str(v) for v in list(row[key]))  # type: ignore[call-overload]
            writer.writerow(flat)
    print(json.dumps({k: v for k, v in payload.items() if k != "rows"}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
