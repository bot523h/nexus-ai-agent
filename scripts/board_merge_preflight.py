#!/usr/bin/env python3
"""Prove, per open PR, that the frozen board archive is its only merge conflict.

Audit A-04 acceptance ("zero code PRs dirty solely because of the board") is
measured, not asserted: for every PR given (or every PR the census marked
``dirty_only_because_of_board``) this script

1. fetches the PR head commit (read-only — no branch is checked out or moved),
2. runs ``git merge-tree --write-tree`` of ``origin/main`` and the head, and
   records the conflicted paths,
3. builds a *local, unpublished* commit on top of the head whose only change is
   restoring ``.agents/board.json`` to ``origin/main``'s blob (the one-line
   rebase step ADR 0012 prescribes), and
4. runs the same merge-tree again.

A PR passes when step 2 conflicts only on ``.agents/board.json`` and step 4 has
no conflicts.  Stdlib + git only; needs full history of ``origin/main``
(``git fetch --unshallow origin main`` on a shallow clone).

    python scripts/board_merge_preflight.py --census docs/audits/<pkg>/pr_census.json
    python scripts/board_merge_preflight.py --pr 97 --pr 198 --out proof.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ".agents/board.json"


def _git(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False, env=env, timeout=600
    )


def _conflicts(main: str, commit: str) -> tuple[int, list[str]]:
    """Return (exit code, conflicted paths) of a merge-tree dry run."""
    result = _git("merge-tree", "--write-tree", "--name-only", main, commit)
    if result.returncode == 0:
        return 0, []
    if result.returncode != 1:
        raise SystemExit(f"git merge-tree failed for {commit[:7]}: {result.stderr.strip()}")
    paths: list[str] = []
    for line in result.stdout.splitlines()[1:]:  # first line is the tree oid
        if not line.strip():
            break
        paths.append(line.strip())
    return 1, paths


def _restored_commit(head: str, main_blob: str) -> str:
    with tempfile.TemporaryDirectory() as td:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(td, "index"))
        for cmd in (
            ("read-tree", head),
            ("update-index", "--add", "--cacheinfo", f"100644,{main_blob},{ARCHIVE}"),
        ):
            res = _git(*cmd, env=env)
            if res.returncode != 0:
                raise SystemExit(f"git {cmd[0]} failed: {res.stderr.strip()}")
        tree = _git("write-tree", env=env).stdout.strip()
    identity = ["-c", "user.name=board-preflight", "-c", "user.email=preflight@nexus.invalid"]
    res = _git(*identity, "commit-tree", tree, "-p", head, "-m", "preflight: restore board archive")
    if res.returncode != 0:
        raise SystemExit(f"git commit-tree failed: {res.stderr.strip()}")
    return res.stdout.strip()


def _head_sha(pr: int, census: dict[int, dict[str, Any]]) -> str:
    row = census.get(pr)
    if row and row.get("head_sha"):
        return str(row["head_sha"])
    res = subprocess.run(
        ["gh", "api", f"repos/{{owner}}/{{repo}}/pulls/{pr}", "--jq", ".head.sha"],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        timeout=120,
    )
    if res.returncode != 0 or not res.stdout.strip():
        raise SystemExit(f"cannot resolve head of #{pr}: {res.stderr.strip() or 'no output'}")
    return res.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--census", default="", help="pr_census.json (selects board-only PRs)")
    parser.add_argument("--pr", type=int, action="append", default=[], help="PR number")
    parser.add_argument("--out", default="", help="write the JSON proof here")
    args = parser.parse_args()

    census: dict[int, dict[str, Any]] = {}
    if args.census:
        data = json.loads(Path(args.census).read_text(encoding="utf-8"))
        census = {int(r["number"]): r for r in data["rows"]}
    targets = sorted(
        set(args.pr) | {n for n, r in census.items() if r.get("dirty_only_because_of_board")}
    )
    if not targets:
        print("nothing to check (no --pr and no board-only-dirty rows in the census)")
        return 0
    if _git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        print("UNVERIFIABLE: shallow clone — run `git fetch --unshallow origin main` first")
        return 2
    main_sha = _git("rev-parse", "origin/main").stdout.strip()
    main_blob = _git("rev-parse", f"{main_sha}:{ARCHIVE}").stdout.strip()
    if not main_sha or not main_blob:
        print("UNVERIFIABLE: origin/main or its board archive is not resolvable")
        return 2

    results: list[dict[str, Any]] = []
    for pr in targets:
        head = _head_sha(pr, census)
        if _git("cat-file", "-e", f"{head}^{{commit}}").returncode != 0:
            fetched = _git("fetch", "-q", "origin", head)
            if fetched.returncode != 0:
                results.append({"pr": pr, "head": head, "verdict": "UNVERIFIABLE"})
                print(f"#{pr} {head[:7]} UNVERIFIABLE: {fetched.stderr.strip().splitlines()[-1:]}")
                continue
        rc_before, before = _conflicts(main_sha, head)
        rc_after, after = _conflicts(main_sha, _restored_commit(head, main_blob))
        board_only = rc_before == 1 and before == [ARCHIVE]
        verdict = (
            "CLEAN_AFTER_ARCHIVE_RESTORE"
            if board_only and rc_after == 0
            else ("ALREADY_CLEAN" if rc_before == 0 else "REAL_CONFLICTS")
        )
        results.append(
            {
                "pr": pr,
                "head": head,
                "conflicts_before": before,
                "conflicts_after": after,
                "verdict": verdict,
            }
        )
        print(f"#{pr} {head[:7]} before={before or '-'} after={after or '-'} → {verdict}")

    clean = sum(1 for r in results if r["verdict"] == "CLEAN_AFTER_ARCHIVE_RESTORE")
    remaining = [r["pr"] for r in results if r["verdict"] == "REAL_CONFLICTS"]
    print(
        f"RESULT: {clean}/{len(results)} PRs are dirty solely because of the board archive and "
        f"merge cleanly once it is restored; real conflicts remain in {remaining or 'none'}"
    )
    if args.out:
        payload = {"main": main_sha, "frozen_blob": main_blob, "results": results}
        Path(args.out).write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    return 0 if not remaining and all(r["verdict"] != "UNVERIFIABLE" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
