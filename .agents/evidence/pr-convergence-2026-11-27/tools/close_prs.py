#!/usr/bin/env python3
"""Close every non-merged PR with its documented terminal disposition.

Reads ``PR_CONVERGENCE_DISPOSITION_MATRIX.json``, posts one substantive comment
per PR (exact SHAs, delta, conflicts, preservation anchor, resume condition),
then closes the PR. Writes ``CLOSURE_RESULT.json`` so the final report can prove
what was closed and where the evidence lives.

Two safety properties make re-runs safe:

* **compare-and-swap** — :func:`verify_live_pr_head_matches_snapshot` re-reads
  the PR immediately before acting and refuses (``StalePRSnapshotError``) when
  the live state is not ``open`` or ``head.sha`` no longer equals the SHA the
  matrix was computed against. A disposition is a statement about a *specific*
  head; acting on a different head would close work nobody audited.
* **idempotency** — :func:`has_existing_closure_comment` detects the stable
  ``CLOSURE_MARKER`` so a retry never posts a duplicate termination comment.

Usage::

    python close_prs.py --dry-run
    python close_prs.py --only 135
    python close_prs.py            # all non-merged dispositions
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

TOKEN = os.environ["GITHUB_PERSONAL_ACCESS_TOKEN"]
REPO = "bot523h/nexus-ai-agent"
ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "PR_CONVERGENCE_DISPOSITION_MATRIX.json"
RESULT = ROOT / "CLOSURE_RESULT.json"

#: Stable, machine-detectable marker. Never change it without a migration.
CLOSURE_MARKER = "<!-- nexus-convergence:2026-11-27:closure -->"
SKIP_DISPOSITIONS = ("MERGED", "MERGE_CANDIDATE", "BLOCKED_ACTIVE_OWNER")


class StalePRSnapshotError(RuntimeError):
    """The live PR no longer matches the head the disposition was computed on."""


def verify_live_pr_head_matches_snapshot(
    pr_number: int, expected_head_sha: str, live_pr_payload: Mapping[str, Any]
) -> bool:
    """Fail closed unless the live PR is open at exactly *expected_head_sha*.

    Returns ``True`` when the compare-and-swap precondition holds and raises
    :class:`StalePRSnapshotError` otherwise, so a caller can never comment on
    or close a head it did not audit.
    """
    if not expected_head_sha:
        raise StalePRSnapshotError(f"#{pr_number}: the snapshot carries no head SHA")
    state = live_pr_payload.get("state")
    if state != "open":
        raise StalePRSnapshotError(f"#{pr_number}: live state is {state!r}, not 'open'")
    head = live_pr_payload.get("head") or {}
    live_sha = head.get("sha") if isinstance(head, Mapping) else None
    if live_sha != expected_head_sha:
        raise StalePRSnapshotError(
            f"#{pr_number}: live head {live_sha!r} != audited head {expected_head_sha!r}"
        )
    return True


def has_existing_closure_comment(
    comments: Sequence[Mapping[str, Any]], marker: str = CLOSURE_MARKER
) -> bool:
    """True when a previous run already posted the termination comment."""
    for entry in comments:
        body = entry.get("body")
        if isinstance(body, str) and marker in body:
            return True
    return False


def api(path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "nexus-convergence",
        },
        method=method,
    )
    for attempt in range(6):
        try:
            with urllib.request.urlopen(request) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429) and attempt < 5:
                time.sleep(2**attempt)
                continue
            raise
    raise RuntimeError("unreachable")


def comment(row: Mapping[str, Any]) -> str:
    disposition = row["disposition"]
    head = (
        "This comment was created by an AI agent (OpenHands) on behalf of the user.\n\n"
        f"**Closed by the 2026-11-27 PR backlog convergence** "
        f"(`NEXUS-PR-BACKLOG-CONVERGENCE-2026-11-27`).\n\n"
        f"| | |\n|---|---|\n"
        f"| Disposition | `{disposition}` |\n"
        f"| Head | `{row['head_sha']}` |\n"
        f"| Main at closure | `{row['main_sha']}` |\n"
        f"| Delta vs main | {row['ahead']} commits ahead, {row['behind']} behind, "
        f"{row['changed_files']} files, +{row['additions']}/-{row['deletions']} |\n"
        f"| Merge-tree conflicts | {row['merge_tree_conflicts']} |\n"
        f"| Unresolved review threads | {row['unresolved_threads']} |\n"
        f"| Added identifiers already on main | {row['symbols_on_main']}/{row['added_symbols']} |\n"
    )
    body = [head, f"\n**Why `{disposition}`**\n\n{row['rationale']}\n"]
    if row.get("evidence"):
        body.append(
            "\n**Evidence**\n\n" + "\n".join(f"- `{item}`" for item in row["evidence"]) + "\n"
        )
    if disposition == "SUPERSEDED_AND_CLOSED":
        body.append(
            "\nThe capability this PR introduced already exists on canonical `main`, so "
            "replaying the branch would create a duplicate implementation. The branch is "
            "**kept** for history and this PR stays reopenable; no code is deleted.\n"
        )
    elif disposition == "EXTRACTED_AND_CLOSED":
        body.append(
            "\nThe unique artefact this PR carried was extracted into the convergence branch "
            "(`.agents/evidence/pr-convergence-2026-11-27/`), so the work is preserved on "
            "main-bound history. The branch is **kept** and this PR stays reopenable.\n"
        )
    else:
        body.append(
            f"\n**Preservation anchor**\n\n"
            f"- Branch `{row['head_ref']}` is kept and this PR is reopenable.\n"
            f"- Durable board task `{row['board_task']}` (queued) is the resume anchor.\n"
            f"- Convergence PR: #369 (merges the disposition matrix and board anchors).\n"
            f"- Convergence evidence: "
            f"`.agents/evidence/pr-convergence-2026-11-27/PR_CONVERGENCE_DISPOSITION_MATRIX.json`.\n"
            f"\n**Resume condition**\n\n{row['resume_condition']}\n"
        )
    body.append(
        "\nThis is a deliberate backlog retirement, not a judgement that the work is worthless: "
        "the delta is preserved and the resume condition is explicit.\n"
        f"\n{CLOSURE_MARKER}\n"
    )
    return "".join(body)


def _live_pr(number: int) -> Mapping[str, Any]:
    payload = api(f"/pulls/{number}")
    return payload if isinstance(payload, Mapping) else {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only", type=int, action="append")
    args = parser.parse_args()

    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))["records"]
    existing = json.loads(RESULT.read_text(encoding="utf-8")) if RESULT.exists() else []
    done = {row["number"] for row in existing}

    for row in matrix:
        number = row["number"]
        if row["disposition"] in SKIP_DISPOSITIONS:
            continue
        if args.only and number not in args.only:
            continue
        if number in done:
            continue
        text = comment(row)
        if args.dry_run:
            print(f"--- #{number} {row['disposition']} ---")
            print(text)
            continue
        live = _live_pr(number)
        try:
            verify_live_pr_head_matches_snapshot(number, str(row["head_sha"]), live)
        except StalePRSnapshotError as exc:
            if (live.get("state") == "closed") and has_existing_closure_comment(
                api(f"/issues/{number}/comments?per_page=100")
            ):
                print(f"skip #{number}: already closed with the closure marker ({exc})")
                continue
            print(f"REFUSING #{number}: {exc}")
            return 1
        comments = api(f"/issues/{number}/comments?per_page=100")
        if has_existing_closure_comment(comments):
            print(f"skip #{number}: closure comment already present (idempotent)")
            continue
        posted = api(f"/issues/{number}/comments", "POST", {"body": text})
        api(f"/pulls/{number}", "PATCH", {"state": "closed"})
        existing.append(
            {
                "number": number,
                "disposition": row["disposition"],
                "comment_url": posted.get("html_url"),
                "closed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
        RESULT.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"closed #{number} ({row['disposition']}) -> {posted.get('html_url')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
