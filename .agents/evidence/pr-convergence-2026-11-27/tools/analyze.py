#!/usr/bin/env python3
"""Turn PR_FORENSICS.json into evidence for dispositions.

For every open PR it measures, against the live main tree:

* how much of its changed-file set already exists on main (identical blobs),
* how many of the identifiers it *adds* already exist on main (symbol coverage),
* whether an equivalent commit subject already landed on main,
* which other open PRs touch the same files (overlap graph input).

Nothing here decides a disposition; it only produces the numbers a human or the
convergence owner can cite.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORENSICS = ROOT / "PR_FORENSICS.json"
OUT = ROOT / "PR_ANALYSIS.json"
MAIN = "main"

DEF_RE = re.compile(r"^\+\s*(?:async\s+)?def\s+([A-Za-z_]\w*)")
CLASS_RE = re.compile(r"^\+\s*class\s+([A-Za-z_]\w*)")
CONST_RE = re.compile(r"^\+\s*([A-Z][A-Z0-9_]{3,})\s*[:=]")
SKIP = {
    "main",
    "run",
    "setUp",
    "tearDown",
    "test",
    "tests",
}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False).stdout


def main_symbols() -> set[str]:
    text = git(
        "grep",
        "-h",
        "-o",
        "-E",
        r"(def|class) [A-Za-z_][A-Za-z0-9_]*",
        MAIN,
        "--",
        "*.py",
    )
    symbols: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            symbols.add(parts[1])
    return symbols


def main_subjects() -> list[str]:
    return [line.split(" ", 1)[1] for line in git("log", "--format=%H %s", MAIN, "-n", "4000").splitlines() if " " in line]


def main() -> int:
    data = json.loads(FORENSICS.read_text(encoding="utf-8"))
    symbols = main_symbols()
    subjects = main_subjects()
    print(f"main symbols: {len(symbols)}; main commit subjects: {len(subjects)}")

    records = []
    for pr in data["records"]:
        number = pr["number"]
        ref = f"refs/remotes/pr/{number}"
        diff = git("diff", "--unified=0", pr["merge_base"], ref)
        added: set[str] = set()
        for line in diff.splitlines():
            if not line.startswith("+"):
                continue
            for regex in (DEF_RE, CLASS_RE, CONST_RE):
                match = regex.match(line)
                if match:
                    name = match.group(1)
                    if name not in SKIP and not name.startswith("__"):
                        added.add(name)
        present = sorted(name for name in added if name in symbols)
        missing = sorted(name for name in added if name not in symbols)
        coverage = (len(present) / len(added)) if added else None

        pr_subjects = [
            line.split(" ", 1)[1]
            for line in git("log", "--format=%H %s", f"{pr['merge_base']}..{ref}").splitlines()
            if " " in line
        ]
        landed_subjects = [s for s in pr_subjects if s in subjects]

        files = [entry["path"] for entry in pr["files"]]
        identical = sum(1 for entry in pr["files"] if entry["on_main_identical"])
        on_main = sum(1 for entry in pr["files"] if entry["on_main"])

        records.append(
            {
                "number": number,
                "title": pr["title"],
                "head_sha": pr["head_sha"],
                "ahead": pr["ahead"],
                "behind": pr["behind"],
                "changed_files": len(files),
                "files_on_main": on_main,
                "files_identical_on_main": identical,
                "conflicts": pr["merge_tree_conflicts"],
                "added_symbols": len(added),
                "symbols_on_main": len(present),
                "symbol_coverage": coverage,
                "missing_symbols": missing[:40],
                "commits": len(pr_subjects),
                "commit_subjects_landed_on_main": len(landed_subjects),
                "draft": pr["draft"],
                "unresolved_threads": pr["unresolved_threads"],
                "reviews": pr["reviews"],
                "linked_refs": pr["linked_refs"],
                "files": files,
            }
        )
        cov = "n/a" if coverage is None else f"{coverage:.0%}"
        print(
            f"  #{number:<4} symbols={len(present):>3}/{len(added):<3} ({cov:>4}) "
            f"files_on_main={on_main}/{len(files)} identical={identical} "
            f"subjects_on_main={len(landed_subjects)}/{len(pr_subjects)}"
        )

    overlap = []
    for i, left in enumerate(records):
        left_files = set(left["files"])
        for right in records[i + 1 :]:
            right_files = set(right["files"])
            union = left_files | right_files
            if not union:
                continue
            jaccard = len(left_files & right_files) / len(union)
            if jaccard >= 0.2:
                overlap.append(
                    {
                        "a": left["number"],
                        "b": right["number"],
                        "jaccard": round(jaccard, 3),
                        "shared_files": sorted(left_files & right_files)[:20],
                    }
                )

    payload = {
        "main_sha": data["main_sha"],
        "open_total": data["open_total"],
        "records": records,
        "overlap": sorted(overlap, key=lambda edge: (-edge["jaccard"], edge["a"], edge["b"])),
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
