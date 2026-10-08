#!/usr/bin/env python3
"""Release lineage: VERSION → commit → CI → tag → GitHub Release → CHANGELOG.

Every link is machine-checked against the live repository state; nothing is
assumed.  A missing link is an **OPEN** item (printed, written to the job
summary and to the JSON artifact — never hidden), a **contradiction** is a red
build:

RED (exit 1)
    * the version lockstep is broken (VERSION ≠ pyproject ≠ newest released
      CHANGELOG heading);
    * the current version's tag exists but is not reachable from HEAD (a tag
      on a SHA outside the release history);
    * the current version's tag exists but has no GitHub Release;
    * the working tree is dirty (generated files must be committed, not left
      lying around — unreproducible reports start here).

OPEN (exit 0, loudly reported)
    * no tag for the current version yet (the release simply has not been cut);
    * the newest tag overall is not reachable from HEAD (pre-existing state,
      e.g. a dangling tag from a discarded branch — recorded for the owner);
    * the newest GitHub Release is older than the current version;
    * a Release carries no artifacts.

The ``--github-summary`` option appends a Markdown table to
``$GITHUB_STEP_SUMMARY`` so the evidence is on the run page, and ``--out``
writes the JSON artifact bound to the source ``--sha``.

Standard library only; git and (when a token is available) the ``gh`` CLI are
invoked as subprocesses.

Persian note: زنجیرهٔ release به‌صورت ماشینی بررسی می‌شود؛ نبودِ حلقه = OPEN،
تناقض = قرمز.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = REPO_ROOT / "VERSION"
PYPROJECT = REPO_ROOT / "pyproject.toml"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_RELEASED_HEADING = re.compile(r"^##\s*\[(\d+\.\d+\.\d+)\]", re.MULTILINE)
_PROJECT_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)

_GH_FIELDS = "tagName,targetCommitish,isDraft,isPrerelease,publishedAt,assets,url"


# --------------------------------------------------------------------------- #
# declarations
# --------------------------------------------------------------------------- #
def read_declared_version() -> str:
    text = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not _SEMVER.match(text):
        raise SystemExit(f"VERSION is not a semantic version: {text!r}")
    return text


def read_pyproject_version() -> str:
    match = _PROJECT_VERSION.search(PYPROJECT.read_text(encoding="utf-8"))
    if match is None:
        raise SystemExit("pyproject.toml has no [project] version")
    return match.group(1)


def read_changelog_version() -> str | None:
    match = _RELEASED_HEADING.search(CHANGELOG.read_text(encoding="utf-8"))
    return match.group(1) if match else None


# --------------------------------------------------------------------------- #
# git helpers
# --------------------------------------------------------------------------- #
def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *arguments],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def git_tags() -> list[str]:
    return [line for line in _git("tag", "-l").splitlines() if line.strip()]


def tag_commit(tag: str) -> str:
    return _git("rev-list", "-1", tag)


def tag_reachable_from_head(tag: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "merge-base", "--is-ancestor", tag, "HEAD"],
        capture_output=True,
        timeout=60,
    )
    return completed.returncode == 0


def head_sha() -> str:
    return _git("rev-parse", "HEAD")


def dirty_tree() -> list[str]:
    return [line for line in _git("status", "--porcelain").splitlines() if line.strip()]


def _semver_sort_key(tag: str) -> tuple[int, ...]:
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", tag)
    if match is None:
        return (-1, -1, -1)
    return tuple(int(part) for part in match.groups())


# --------------------------------------------------------------------------- #
# GitHub release truth (gh CLI; absent token → recorded as unverified)
# --------------------------------------------------------------------------- #
def _gh_release(tag: str | None) -> dict[str, object] | None:
    command = ["gh", "release", "view"]
    if tag is not None:
        command.append(tag)
    command += ["--json", _GH_FIELDS]
    completed = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    if completed.returncode != 0:
        return None
    data = json.loads(completed.stdout)
    data["assets"] = [
        {"name": asset.get("name"), "size": asset.get("size")} for asset in data.get("assets", [])
    ]
    return data


# --------------------------------------------------------------------------- #
# the lineage check
# --------------------------------------------------------------------------- #
def build_lineage(*, sha: str, verify_github: bool = True) -> dict[str, object]:
    version = read_declared_version()
    pyproject_version = read_pyproject_version()
    changelog_version = read_changelog_version()
    current_tag = f"v{version}"

    red: list[str] = []
    open_items: list[str] = []

    # 1. lockstep (self-contained re-check; the fast rail reports it earlier)
    if not (version == pyproject_version == changelog_version):
        red.append(
            f"version lockstep broken: VERSION={version} pyproject={pyproject_version} "
            f"CHANGELOG={changelog_version}"
        )

    # 2. working tree (generated files must be committed)
    dirty = dirty_tree()
    if dirty:
        red.append(f"working tree dirty ({len(dirty)} entries): {dirty[:5]}")

    # 3. the current version's tag
    tags = git_tags()
    tag_exists = current_tag in tags
    tag_sha = tag_commit(current_tag) if tag_exists else ""
    tag_on_history = tag_reachable_from_head(current_tag) if tag_exists else False
    if not tag_exists:
        open_items.append(
            f"no tag {current_tag} for the current version — the release has not been cut"
        )
    elif not tag_on_history:
        red.append(
            f"tag {current_tag} ({tag_sha[:12]}) is NOT reachable from HEAD — a release "
            "tag must live on the release history"
        )

    # 4. the newest tag overall (catches dangling tags from discarded branches)
    version_tags = [t for t in tags if _semver_sort_key(t) != (-1, -1, -1)]
    newest_tag = max(version_tags, key=_semver_sort_key) if version_tags else None
    newest_tag_reachable = tag_reachable_from_head(newest_tag) if newest_tag else None
    if newest_tag and newest_tag != current_tag and not newest_tag_reachable:
        open_items.append(
            f"newest tag {newest_tag} ({tag_commit(newest_tag)[:12]}) is not reachable from "
            "HEAD — a dangling tag from outside the release history"
        )

    # 5. GitHub release truth
    release: dict[str, object] | None = None
    latest_release: dict[str, object] | None = None
    if verify_github:
        latest_release = _gh_release(None)
        if tag_exists:
            release = _gh_release(current_tag)
            if release is None:
                red.append(
                    f"tag {current_tag} exists but has no GitHub Release — cut the "
                    "release or delete the tag"
                )
            elif not list(release.get("assets", [])):
                open_items.append(f"GitHub Release {current_tag} carries no artifacts")
        if latest_release is not None:
            latest_tag_name = str(latest_release.get("tagName", ""))
            if _semver_sort_key(latest_tag_name) < _semver_sort_key(current_tag):
                open_items.append(
                    f"newest GitHub Release {latest_tag_name} is older than the current "
                    f"version {version} — releases lag the repository"
                )
    else:
        open_items.append("GitHub Release truth not verified (gh/GH_TOKEN unavailable)")

    status = "RED" if red else ("OPEN" if open_items else "GREEN")
    return {
        "schema": 1,
        "status": status,
        "version": version,
        "pyproject_version": pyproject_version,
        "changelog_version": changelog_version,
        "source_commit": sha,
        "head_commit": head_sha(),
        "working_tree_clean": not dirty,
        "current_tag": current_tag,
        "tag_exists": tag_exists,
        "tag_commit": tag_sha or None,
        "tag_reachable_from_head": tag_on_history if tag_exists else None,
        "newest_tag": newest_tag,
        "newest_tag_reachable_from_head": newest_tag_reachable,
        "latest_github_release": latest_release,
        "github_release_for_current_version": release,
        "red": red,
        "open": open_items,
    }


_SUMMARY_TEMPLATE = """## Release lineage

| Link | State | Evidence |
|---|---|---|
| Lockstep `VERSION`==`pyproject`==CHANGELOG | {lockstep} | `{version}` / `{pp}` / `{cl}` |
| Tag `{current_tag}` | {tag_state} | {tag_evidence} |
| Newest tag `{newest_tag}` | {newest_state} | {newest_evidence} |
| GitHub Release (latest) | {release_state} | {release_evidence} |
| Working tree | {tree_state} | {tree_evidence} |

**Verdict: {status}** — {verdict_note}

{detail}
"""


def render_summary(report: dict[str, object]) -> str:
    def _yes(condition: bool, ok: str = "OK", bad: str = "BROKEN") -> str:
        return ok if condition else bad

    version = str(report["version"])
    current_tag = str(report["current_tag"])
    newest_tag = str(report.get("newest_tag") or "—")
    latest = report.get("latest_github_release") or {}
    latest_name = str(latest.get("tagName", "—")) if latest else "unverified"

    if not report["tag_exists"]:
        tag_state, tag_evidence = (
            "MISSING",
            "no tag for the current version (OPEN: release not cut)",
        )
    else:
        tag_state = _yes(bool(report["tag_reachable_from_head"]), "ON HISTORY", "OFF HISTORY")
        tag_evidence = f"commit `{str(report.get('tag_commit') or '?')[:12]}`"
    newest_state = _yes(bool(report.get("newest_tag_reachable_from_head")), "REACHABLE", "DANGLING")
    release_state = "PRESENT" if latest else "UNVERIFIED"
    release_evidence = (
        f"`{latest_name}`, assets: {len(latest.get('assets', []))}"
        if latest
        else "gh/GH_TOKEN unavailable in this environment"
    )
    detail = ""
    if report["red"]:
        detail += (
            "**RED (release is broken, not merely pending):**\n"
            + "\n".join(f"- {item}" for item in report["red"])
            + "\n"
        )
    if report["open"]:
        detail += "**OPEN items:**\n" + "\n".join(f"- {item}" for item in report["open"])
    return _SUMMARY_TEMPLATE.format(
        lockstep=_yes(version == report["pyproject_version"] == report["changelog_version"]),
        version=version,
        pp=report["pyproject_version"],
        cl=report["changelog_version"],
        current_tag=current_tag,
        tag_state=tag_state,
        tag_evidence=tag_evidence,
        newest_tag=newest_tag,
        newest_state=newest_state,
        newest_evidence="newest tag overall (not required to equal the current version tag)",
        release_state=release_state,
        release_evidence=release_evidence,
        tree_state=_yes(bool(report["working_tree_clean"]), "CLEAN", "DIRTY"),
        tree_evidence=(
            f"HEAD `{str(report['head_commit'])[:12]}` vs checked-out "
            f"`{str(report['source_commit'])[:12]}`"
        ),
        status=report["status"],
        verdict_note={
            "GREEN": "all links verified",
            "RED": "red items must be fixed",
            "OPEN": "links missing, recorded as OPEN",
        }[str(report["status"])],
        detail=detail,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sha", required=True, help="the commit this report describes")
    parser.add_argument("--out", help="write the JSON artifact here")
    parser.add_argument("--github-summary", help="append the Markdown summary to this file")
    parser.add_argument(
        "--no-verify-github",
        action="store_true",
        help="skip gh CLI release checks (offline/local runs)",
    )
    args = parser.parse_args(argv)

    report = build_lineage(sha=args.sha, verify_github=not args.no_verify_github)

    print(json.dumps(report, indent=2, sort_keys=False))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    if args.github_summary:
        with Path(args.github_summary).open("a", encoding="utf-8") as handle:
            handle.write(render_summary(report))
        print(f"appended summary to {args.github_summary}", file=sys.stderr)

    if report["red"]:
        print("RELEASE LINEAGE RED:", file=sys.stderr)
        for item in report["red"]:
            print(f"  - {item}", file=sys.stderr)
        return 1
    if report["open"]:
        print("RELEASE LINEAGE OPEN (not blocking, recorded):", file=sys.stderr)
        for item in report["open"]:
            print(f"  - {item}", file=sys.stderr)
    else:
        print("RELEASE LINEAGE GREEN", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
