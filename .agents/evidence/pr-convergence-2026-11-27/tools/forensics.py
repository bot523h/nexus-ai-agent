#!/usr/bin/env python3
"""Live forensic inventory for the 2026-11-27 NEXUS PR backlog convergence.

Reads GitHub (REST + GraphQL) and local git, and writes one JSON record per open
PR: identity, exact SHAs, CI/review state, changed files, and the git evidence
needed to classify it (merge-base, ahead/behind, per-file blob equality against
main, merge-tree conflicts, whitespace errors).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN = os.environ["GITHUB_PERSONAL_ACCESS_TOKEN"]
REPO = "bot523h/nexus-ai-agent"
MAIN = "main"
OUT = Path(__file__).resolve().parents[1]
API = f"https://api.github.com/repos/{REPO}"
OWNER, NAME = REPO.split("/", 1)


def api(path: str, params: dict[str, str] | None = None) -> object:
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(6):
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "nexus-convergence",
            },
        )
        try:
            with urllib.request.urlopen(request) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429) and attempt < 5:
                time.sleep(2**attempt)
                continue
            raise
    raise RuntimeError("unreachable")


def graphql(query: str, variables: dict[str, object]) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request(
        "https://api.github.com/graphql",
        data=body,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "nexus-convergence",
        },
    )
    with urllib.request.urlopen(request) as response:
        payload = json.load(response)
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"])[:500])
    return payload["data"]


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check)


def sh(*args: str) -> str:
    return git(*args).stdout.strip()


THREADS = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) { totalCount nodes { isResolved } }
      reviews(first: 100) { totalCount nodes { state author { login } } }
    }
  }
}
"""


def main() -> int:
    main_sha = str(api("/commits/main")["sha"])
    local_main = sh("rev-parse", MAIN)
    if local_main != main_sha:
        raise SystemExit(
            f"local {MAIN} {local_main[:12]} != GitHub main {main_sha[:12]}: "
            f"run `git fetch origin main && git branch -f main origin/main` first"
        )
    prs: list[dict] = []
    page = 1
    while True:
        batch = api("/pulls", {"state": "open", "per_page": "100", "page": str(page)})
        assert isinstance(batch, list)
        prs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    print(f"open PRs: {len(prs)}; main {main_sha[:12]}")

    numbers = [int(pr["number"]) for pr in prs]
    refspecs = [f"+refs/pull/{n}/head:refs/remotes/pr/{n}" for n in numbers]
    print("fetching PR heads ...")
    git("fetch", "--no-tags", "origin", *refspecs)
    print("fetch done")

    records: list[dict] = []
    for pr in prs:
        number = int(pr["number"])
        ref = f"refs/remotes/pr/{number}"
        try:
            gql = graphql(THREADS, {"owner": OWNER, "name": NAME, "number": number})["repository"][
                "pullRequest"
            ]
            threads = gql.get("reviewThreads") or {}
            unresolved = sum(
                1 for thread in (threads.get("nodes") or []) if not thread.get("isResolved")
            )
            threads_total = threads.get("totalCount") or 0
            reviews = [
                {"state": review.get("state"), "author": (review.get("author") or {}).get("login")}
                for review in ((gql.get("reviews") or {}).get("nodes") or [])
            ]
        except Exception as exc:  # never hide an unreadable review state
            unresolved = -1
            threads_total = -1
            reviews = []
            print(f"  !! #{number} review state unreadable: {exc}", file=sys.stderr)

        merge_base = sh("merge-base", MAIN, ref)
        left, right = sh("rev-list", "--left-right", "--count", f"{MAIN}...{ref}").split()
        ahead, behind = int(right), int(left)

        changed: list[dict] = []
        for line in sh("diff", "--name-status", "--find-renames", merge_base, ref).splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status, path = parts[0], parts[-1]
            head_blob = git("rev-parse", f"{ref}:{path}", check=False).stdout.strip()
            main_blob = git("rev-parse", f"{MAIN}:{path}", check=False).stdout.strip()
            changed.append(
                {
                    "path": path,
                    "status": status[0],
                    "head_blob": head_blob,
                    "main_blob": main_blob,
                    "on_main_identical": bool(head_blob) and head_blob == main_blob,
                    "on_main": bool(main_blob),
                }
            )

        conflict_proc = git("merge-tree", "--write-tree", MAIN, ref, check=False)
        conflict_lines = [
            line for line in (conflict_proc.stdout or "").splitlines() if "CONFLICT" in line
        ]
        whitespace = git("diff", "--check", merge_base, ref, check=False)

        additions = deletions = 0
        for line in sh("diff", "--numstat", merge_base, ref).splitlines():
            cols = line.split("\t")
            if len(cols) >= 2 and cols[0].isdigit() and cols[1].isdigit():
                additions += int(cols[0])
                deletions += int(cols[1])

        records.append(
            {
                "number": number,
                "title": pr["title"],
                "author": (pr.get("user") or {}).get("login"),
                "draft": bool(pr["draft"]),
                "base_ref": pr["base"]["ref"],
                "base_sha": pr["base"]["sha"],
                "head_ref": pr["head"]["ref"],
                "head_sha": pr["head"]["sha"],
                "created_at": pr["created_at"],
                "updated_at": pr["updated_at"],
                "commits": pr.get("commits") or ahead,
                "changed_files": len(changed),
                "additions": additions,
                "deletions": deletions,
                "mergeable": pr.get("mergeable"),
                "merge_state": pr.get("mergeable_state"),
                "labels": [label["name"] for label in pr.get("labels") or []],
                "linked_refs": sorted(
                    {
                        int(x)
                        for x in re.findall(r"#(\d+)", (pr.get("body") or "") + " " + pr["title"])
                    }
                ),
                "review_comments": pr.get("review_comments"),
                "unresolved_threads": unresolved,
                "threads_total": threads_total,
                "reviews": reviews,
                "merge_base": merge_base,
                "ahead": ahead,
                "behind": behind,
                "files": changed,
                "merge_tree_conflicts": len(conflict_lines),
                "whitespace_errors": bool((whitespace.stdout or "").strip()),
            }
        )
        identical = sum(1 for entry in changed if entry["on_main_identical"])
        print(
            f"  #{number:<4} ahead={ahead:<4} behind={behind:<5} files={len(changed):<4} "
            f"on_main={identical}/{len(changed)} conflicts={len(conflict_lines)}"
        )

    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo": REPO,
        "main_sha": main_sha,
        "open_total": len(prs),
        "records": sorted(records, key=lambda r: r["number"]),
    }
    target = OUT / "PR_FORENSICS.json"
    target.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
