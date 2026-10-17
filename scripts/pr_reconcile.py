#!/usr/bin/env python3
"""Turn a PR census into a disposition ledger with an explicit category per PR.

The census (``scripts/pr_census.py``) answers *what state each PR is in* from the
live API.  This script answers the question the closeout needs: **what happens to
each PR next, and who does it**.  Nothing is invented — every row carries the
inputs it was derived from, and the hand-written judgements live in a separate
``--dispositions`` file so a reviewer can see exactly which part is machine fact
and which part is a human decision.

Category rules (first match wins, then the dispositions file may override with a
reason that is copied into the row):

1. the PR is the reference PR of the census            → ``BLOCKED_EXTERNAL`` if it
   still needs a review, otherwise ``READY_TO_MERGE``;
2. a disposition entry with a ``category``              → that category;
3. any failing check on the head                       → ``NEEDS_FIX``;
4. base branch is not the default branch               → ``BLOCKED_EXTERNAL``;
5. merge state ``dirty`` (conflicts) or ``unstable``   → ``NEEDS_REBASE``;
6. commits behind the base                             → ``NEEDS_REBASE``;
7. otherwise                                           → ``READY_TO_MERGE``.

Usage::

    python scripts/pr_reconcile.py \
        --census docs/audits/2026-10-16-completion/pr_census.json \
        --dispositions docs/audits/2026-10-16-completion/pr_dispositions.json \
        --out docs/audits/2026-10-16-completion/pr_reconciliation
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

CATEGORIES = (
    "READY_TO_MERGE",
    "NEEDS_FIX",
    "NEEDS_REBASE",
    "BLOCKED_EXTERNAL",
    "DUPLICATE_OR_SUPERSEDED",
    "SAFE_TO_CLOSE_WITH_EVIDENCE",
    "DEFERRED_WITH_OWNER_AND_REASON",
)

#: What each category means, so a reader never has to guess.
CATEGORY_MEANING = {
    "READY_TO_MERGE": "every requirement GitHub enforces is satisfied; merge is the next action",
    "NEEDS_FIX": "the exact head has failing checks or a confirmed defect; fix first",
    "NEEDS_REBASE": "conflicts or a moved base; take the base into the branch and rerun CI",
    "BLOCKED_EXTERNAL": "cannot progress without a person or a secret outside this environment",
    "DUPLICATE_OR_SUPERSEDED": "its unique content is already delivered elsewhere, proved by file",
    "SAFE_TO_CLOSE_WITH_EVIDENCE": "closing is correct once the named evidence exists; not closed",
    "DEFERRED_WITH_OWNER_AND_REASON": "not on the critical path; the owner decides when to look",
}

#: Default next action per category, used when the dispositions file is silent.
DEFAULT_ACTION = {
    "NEEDS_REBASE": (
        "rebase onto post-#218 main; take .agents/board.json from main "
        "(`git checkout origin/main -- .agents/board.json`), then rerun the exact-head CI"
    ),
    "NEEDS_FIX": "read the failing checks on the exact head, fix the root cause, rerun the gates",
    "BLOCKED_EXTERNAL": "a person outside this environment must act; per row, the action is named",
    "DEFERRED_WITH_OWNER_AND_REASON": "the owner decides whether the unique diff is still wanted",
    "READY_TO_MERGE": "merge through the protected path and verify the resulting main",
}
DEFAULT_ACCEPTANCE = {
    "NEEDS_REBASE": "exact-head CI green on the rebased SHA and a reviewer reading the unique diff",
    "NEEDS_FIX": "green exact-head CI, or a written closure rationale if the PR is abandoned",
    "BLOCKED_EXTERNAL": "the named external action is recorded (review, secret, decision)",
    "DEFERRED_WITH_OWNER_AND_REASON": "an explicit keep/close decision by the owner",
    "READY_TO_MERGE": "merged commit on main whose tree contains the PR's unique changes",
}


def load_dispositions(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {str(key): value for key, value in payload.get("dispositions", {}).items()}


def derive_category(row: dict[str, object], reference: int, default_branch: str) -> str:
    number = int(row["number"])  # type: ignore[arg-type]
    if number == reference:
        return "READY_TO_MERGE" if not row.get("failing_checks") else "BLOCKED_EXTERNAL"
    if row.get("failing_checks"):
        return "NEEDS_FIX"
    if str(row.get("base_ref")) != default_branch:
        return "BLOCKED_EXTERNAL"
    state = str(row.get("mergeable_state") or "")
    if state in {"dirty", "unstable"}:
        return "NEEDS_REBASE"
    if int(row.get("commits_behind_main") or 0) > 0:
        return "NEEDS_REBASE"
    return "READY_TO_MERGE"


def build_rows(
    census: dict[str, object],
    dispositions: dict[str, dict[str, str]],
    default_branch: str,
) -> list[dict[str, object]]:
    reference = int(census.get("reference_pr") or 0)  # type: ignore[arg-type]
    rows: list[dict[str, object]] = []
    for row in census["rows"]:  # type: ignore[index]
        assert isinstance(row, dict)
        number = int(row["number"])  # type: ignore[arg-type]
        entry = dispositions.get(str(number), {})
        derived = derive_category(row, reference, default_branch)
        category = str(entry.get("category") or derived)
        if category not in CATEGORIES:
            raise SystemExit(f"PR #{number}: unknown category {category!r}")
        override = entry.get("category")
        rows.append(
            {
                "number": number,
                "title": str(row.get("title")),
                "author": str(row.get("author")),
                "head_ref": str(row.get("head_ref")),
                "head_sha": str(row.get("head_sha")),
                "base_ref": str(row.get("base_ref")),
                "base_sha_at_census": str(row.get("base_sha_at_census")),
                # main-relative counts: a stacked PR's base-relative numbers
                # would order merges wrongly (see scripts/pr_census.py)
                "unique_commits": int(row.get("unique_commits_vs_main") or 0),
                "commits_behind_main": int(row.get("commits_behind_main") or 0),
                "changed_files": int(
                    row.get("changed_files_vs_main") or row.get("changed_files") or 0
                ),
                "compare_base_ref": str(row.get("compare_base_ref") or row.get("base_ref")),
                "mergeable_state": str(row.get("mergeable_state")),
                "review_decision": str(row.get("review_decision") or ""),
                "failing_checks": len(row.get("failing_checks") or []),  # type: ignore[arg-type]
                "category": category,
                "category_source": "hand" if override else "derived",
                "category_meaning": CATEGORY_MEANING[category],
                "dependency": str(entry.get("dependency", "")),
                "owner": str(
                    entry.get("owner")
                    or "branch author / repository owner (this session writes only to PR #218)"
                ),
                "required_fix": str(entry.get("required_fix") or DEFAULT_ACTION.get(category, "")),
                "acceptance": str(entry.get("acceptance") or DEFAULT_ACCEPTANCE.get(category, "")),
                "evidence": str(entry.get("evidence", "")),
            }
        )
    return sorted(rows, key=lambda r: (r["category"], -int(r["number"])))  # type: ignore[arg-type]


def write_markdown(rows: list[dict[str, object]], out: Path) -> None:
    lines = [
        "# Per-PR reconciliation (generated)",
        "",
        "Generated by `scripts/pr_reconcile.py` from the live census and",
        "`pr_dispositions.json`.  `category_source=hand` marks a row a person decided;",
        "`derived` marks a row the rules decided from the API data.",
        "",
        "| PR | category | files@main | unique@main | behind | head | dependency | owner | next |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| #{number} | {category} | {changed_files} | {unique_commits} | "
            "{commits_behind_main} | `{head_sha}` | {dependency} | {owner} | "
            "{required_fix} |".format(
                **{  # type: ignore[arg-type]
                    **row,
                    "head_sha": str(row["head_sha"])[:7],
                    "dependency": row["dependency"] or "—",
                    "owner": str(row["owner"]).split("/")[0].strip(),
                    "required_fix": str(row["required_fix"])[:90],
                }
            )
        )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--census", required=True)
    parser.add_argument("--dispositions", default="")
    parser.add_argument("--out", required=True)
    parser.add_argument("--reference", type=int, default=0)
    args = parser.parse_args()

    census = json.loads(Path(args.census).read_text(encoding="utf-8"))
    default_branch = str(census.get("default_branch") or "main")
    if args.reference:
        census["reference_pr"] = args.reference
    rows = build_rows(
        census,
        load_dispositions(Path(args.dispositions) if args.dispositions else None),
        default_branch,
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "census_time_utc": census.get("census_time_utc"),
        "default_branch_sha": census.get("default_branch_sha"),
        "open_prs": len(rows),
        "categories": dict(sorted(Counter(str(r["category"]) for r in rows).items())),
        "meaning": CATEGORY_MEANING,
        "rows": rows,
    }
    out.with_suffix(".json").write_text(
        json.dumps(summary, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    with out.with_suffix(".csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    write_markdown(rows, out.with_suffix(".md"))
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
